"""Custody-tx auto-recovery: transient-error reclassification + receipt-poll
liveness loop.

These cover the difference between a deterministic on-chain revert (operator-only
AWAITING_CLEAR) and a transport/server blip (stays runnable so the next loop
re-tries), plus the wall-clock receipt-poll deadline that replaces the old fixed
retry cap. Driven by calling `_process_next_for_chain` / `_handle_receipt_retry`
directly so each invariant is asserted deterministically.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import (
    BadFunctionCallOutput,
    ContractLogicError,
    TimeExhausted,
    TransactionNotFound,
    Web3RPCError,
)

from src.config.bridge import SAPPHIRE_RELEASE_GAS_LIMIT
from src.services.custody_tx_clear_events import (
    _DEFERRED_STALL_RETRY_THRESHOLD,
    ClearAction,
    CustodyTxClearWatcher,
    _expected_applied_hash,
    _pending_key,
)
from src.services.custody_tx_executor import (
    _GAS_BUDGET_EXCEEDED_SELECTOR,
    CustodyTxExecutor,
    CustodyTxKind,
    CustodyTxRequest,
    CustodyTxStatus,
    PreflightDecision,
    PreflightOutcome,
    is_transient_rpc_error,
)
from src.services.custody_tx_proof import _same_address, verify_mark_success

CUSTODY_ADDRESS = "0x" + "ab" * 20
CONTRACT_ADDRESS = "0x" + "cd" * 20
ROUTE_ADDRESS = "0x" + "12" * 20
TO_ADDRESS = "0x" + "34" * 20
SAPPHIRE = 23295
BASE = 84532
AMOUNT = 10**18
WITHDRAWAL_INDEX = 7


def _tx_hash_for(chain_id: int, nonce: int) -> str:
    return "0x" + f"{chain_id:08x}{nonce:056x}"


def _make_receipt(status: int = 1, block_number: int = 100) -> Dict[str, Any]:
    return {
        "status": status,
        "blockNumber": block_number,
        "transactionHash": HexBytes("0x" + "ee" * 32),
        "gasUsed": 21000,
        "effectiveGasPrice": 20_000_000_000,
    }


class _FakeChainEth:
    def __init__(self) -> None:
        self.send_raw_transaction = AsyncMock()
        self.get_transaction_receipt = AsyncMock()
        self.get_transaction_count = AsyncMock(return_value=0)
        self.get_balance = AsyncMock(return_value=10**20)
        # SAPPHIRE_RELEASE MarkSuccessWithHash proof + recovery signer check
        # both read the vouched/recovered tx by hash.
        self.get_transaction = AsyncMock()


class _FakeChainWeb3:
    def __init__(self) -> None:
        self.eth = _FakeChainEth()


@pytest.fixture
def state_dir(tmp_path) -> str:
    return str(tmp_path)


@pytest.fixture
def web3s() -> Dict[int, _FakeChainWeb3]:
    return {SAPPHIRE: _FakeChainWeb3(), BASE: _FakeChainWeb3()}


@pytest.fixture
def accounting(web3s: Dict[int, _FakeChainWeb3]):
    svc = SimpleNamespace()
    svc.contract_address = CONTRACT_ADDRESS
    svc.settings = SimpleNamespace(
        min_withdrawal_gas_balance=10**13,
        sapphire_chain_id=SAPPHIRE,
        chain_rpc_urls={SAPPHIRE: "http://sapphire", BASE: "http://base"},
    )
    svc.get_custody_address = AsyncMock(return_value=CUSTODY_ADDRESS)

    async def _get_chain_web3(cid: int):
        return web3s[cid]

    svc._get_chain_web3 = _get_chain_web3
    svc._get_reader_contract = MagicMock(return_value=None)
    svc.resolve_bridge_withdrawal = AsyncMock(return_value=b"\xab" * 100)
    svc.generate_bridge_burn_transfer = AsyncMock(return_value=b"\xcd" * 100)
    svc.sign_nonce_burn = AsyncMock(return_value=b"\xbe" * 110)
    svc.get_clear_applied_hash = AsyncMock(return_value=b"\x00" * 32)
    return svc


def _make_executor(state_dir: str, accounting, **kwargs) -> CustodyTxExecutor:
    return CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.0,
        receipt_timeout_seconds=0,
        **kwargs,
    )


async def _seed_onchain_nonce(executor: CustodyTxExecutor, chain_id: int, nonce: int) -> None:
    eth = (await executor._accounting._get_chain_web3(chain_id)).eth
    current = eth.get_transaction_count.return_value
    eth.get_transaction_count = AsyncMock(return_value=max(current, nonce))


async def _enqueue_sapphire_release(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    withdrawal_index: int = WITHDRAWAL_INDEX,
) -> str:
    await _seed_onchain_nonce(executor, SAPPHIRE, nonce)
    return await executor.enqueue(
        CustodyTxRequest(
            chain_id=SAPPHIRE,
            evm_nonce=nonce,
            kind=CustodyTxKind.SAPPHIRE_RELEASE,
            id=str(withdrawal_index),
            signed_tx=b"",
            route_address=None,
            max_gas_cost=10**15,
            withdrawal_index=withdrawal_index,
            to_address=TO_ADDRESS,
            amount=AMOUNT,
        )
    )


def _rpc_error(code: int, message: str, data: Any = None) -> Web3RPCError:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return Web3RPCError(message, rpc_response={"jsonrpc": "2.0", "id": 1, "error": err})


# --- 1: provider-shape wrapped reverts escalate; transport blips retry ---


@pytest.mark.parametrize(
    "exc",
    [
        # Alchemy-ish: revert code 3 + selector-bearing data.
        _rpc_error(3, "execution reverted", "0xabcdef12"),
        # Sapphire-gateway-ish: -32000 + "execution reverted: X" + selector data.
        _rpc_error(-32000, "execution reverted: X", _GAS_BUDGET_EXCEEDED_SELECTOR.to_0x_hex()),
        ContractLogicError("execution reverted"),
        BadFunctionCallOutput("could not decode"),
    ],
)
def test_deterministic_failures_are_not_transient(exc):
    assert is_transient_rpc_error(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        TimeExhausted("receipt never came"),
        asyncio.TimeoutError(),
        _rpc_error(-32005, "rate limit exceeded"),
        aiohttp.ServerDisconnectedError("server closed the connection"),
    ],
)
def test_transport_and_ratelimit_errors_are_transient(exc):
    assert is_transient_rpc_error(exc) is True


# --- 2: preflight RPC blip → QUEUED; wrapped revert → AWAITING_CLEAR ---


@pytest.mark.asyncio
async def test_preflight_transient_rpc_blip_stays_queued(state_dir, accounting, web3s):
    """A transient transport error from the SAPPHIRE_RELEASE re-sign round-trips
    to QUEUED (runnable) instead of escalating to AWAITING_CLEAR."""
    accounting.resolve_bridge_withdrawal = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("provider dropped the connection")
    )
    ex = _make_executor(state_dir, accounting)
    await _enqueue_sapphire_release(ex, nonce=210)
    await ex._process_next_for_chain(SAPPHIRE)

    rec = ex.get_record(SAPPHIRE, 210)
    assert rec.status == CustodyTxStatus.QUEUED
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_preflight_wrapped_revert_escalates_awaiting_clear(state_dir, accounting, web3s):
    """A Web3RPCError carrying a real revert (code 3 + selector data) is NOT
    transient → AWAITING_CLEAR; no broadcast."""
    accounting.resolve_bridge_withdrawal = AsyncMock(
        side_effect=_rpc_error(3, "execution reverted", "0xdeadbeef")
    )
    ex = _make_executor(state_dir, accounting)
    await _enqueue_sapphire_release(ex, nonce=211)
    await ex._process_next_for_chain(SAPPHIRE)

    rec = ex.get_record(SAPPHIRE, 211)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 0


# --- 3: receipt-poll → SUCCESS after delayed mining ---


@pytest.mark.asyncio
async def test_receipt_poll_succeeds_after_delayed_mining(state_dir, accounting, web3s):
    """First probe finds the nonce unadvanced (record flips back to runnable);
    once the nonce advances and the receipt is status==1, reconcile promotes to
    SUCCESS. Large deadline so it never escalates."""
    eth = web3s[BASE].eth
    tx_hash = _tx_hash_for(BASE, 320)
    eth.send_raw_transaction.side_effect = [HexBytes(tx_hash)]
    # First poll/probe: nonce unadvanced and receipt absent.
    eth.get_transaction_count = AsyncMock(return_value=320)

    async def _no_receipt_yet(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    eth.get_transaction_receipt.side_effect = _no_receipt_yet

    ex = _make_executor(
        state_dir,
        accounting,
        receipt_probe_interval=1,
        receipt_stuck_deadline_seconds=3600.0,
    )
    await _seed_onchain_nonce(ex, BASE, 320)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=320,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="delayed",
            signed_tx=bytes.fromhex("de0140beef"),
        )
    )
    # First pass: broadcast, receipt times out, probe sees nonce==320 (unadvanced),
    # under deadline → flip back to QUEUED.
    await ex._process_next_for_chain(BASE)
    rec = ex.get_record(BASE, 320)
    assert rec.status == CustodyTxStatus.QUEUED
    assert rec.stuck_since is not None

    # Now the tx mines: nonce advances and the receipt resolves status==1.
    eth.get_transaction_count = AsyncMock(return_value=321)

    async def _mined_receipt(_h: Any) -> Dict[str, Any]:
        return _make_receipt(status=1, block_number=1000)

    eth.get_transaction_receipt.side_effect = _mined_receipt
    # Re-broadcast hits "already known"; reconcile-by-sender-nonce promotes it.
    eth.send_raw_transaction.side_effect = [Exception("already known")]
    await ex._process_next_for_chain(BASE)

    rec_final = ex.get_record(BASE, 320)
    assert rec_final.status == CustodyTxStatus.SUCCESS


# --- 4: never-mineable under-cap → wall-clock deadline → AWAITING_CLEAR ---


@pytest.mark.asyncio
async def test_unmineable_tx_escalates_past_wallclock_deadline(state_dir, accounting, web3s):
    """A BROADCAST record whose nonce never advances escalates to AWAITING_CLEAR
    once the wall-clock deadline elapses, carrying the deadline error string."""
    eth = web3s[BASE].eth
    eth.send_raw_transaction.side_effect = [HexBytes(_tx_hash_for(BASE, 400))]
    eth.get_transaction_count = AsyncMock(return_value=400)

    async def _never_receipt(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    eth.get_transaction_receipt.side_effect = _never_receipt

    ex = _make_executor(
        state_dir,
        accounting,
        receipt_probe_interval=1,
        receipt_stuck_deadline_seconds=0.0,
    )
    await _seed_onchain_nonce(ex, BASE, 400)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=400,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="stuck-under-cap",
            signed_tx=bytes.fromhex("de0190beef"),
        )
    )
    await ex._process_next_for_chain(BASE)

    rec = ex.get_record(BASE, 400)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "deadline" in (rec.error or "")


# --- 5: SAPPHIRE_RELEASE re-sign reverts GasBudgetExceeded → AWAITING_CLEAR_GAS_CAP ---


@pytest.mark.asyncio
async def test_sapphire_release_gas_budget_exceeded_routes_to_gas_cap(state_dir, accounting, web3s):
    """resolve_bridge_withdrawal reverting GasBudgetExceeded (real selector on
    .data) escalates to AWAITING_CLEAR_GAS_CAP, distinct from a generic revert."""
    accounting.resolve_bridge_withdrawal = AsyncMock(
        side_effect=ContractLogicError(
            "execution reverted: GasBudgetExceeded()",
            data=_GAS_BUDGET_EXCEEDED_SELECTOR.to_0x_hex(),
        )
    )
    ex = _make_executor(state_dir, accounting)
    await _enqueue_sapphire_release(ex, nonce=500)
    await ex._process_next_for_chain(SAPPHIRE)

    rec = ex.get_record(SAPPHIRE, 500)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR_GAS_CAP
    assert "GasBudgetExceeded" in (rec.error or "")
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 0


# --- 6: broadcast transient rejection → QUEUED; real rejection → AWAITING_CLEAR ---


@pytest.mark.asyncio
async def test_broadcast_transient_rejection_stays_queued(state_dir, accounting, web3s):
    """send_raw_transaction raising a transient transport error keeps the record
    runnable (QUEUED) so the next pass re-broadcasts the same nonce."""
    eth = web3s[BASE].eth
    eth.send_raw_transaction.side_effect = [aiohttp.ServerDisconnectedError("dropped")]
    eth.get_transaction_count = AsyncMock(return_value=600)

    ex = _make_executor(state_dir, accounting)
    await _seed_onchain_nonce(ex, BASE, 600)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=600,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="transient-send",
            signed_tx=bytes.fromhex("de0258beef"),
        )
    )
    await ex._process_next_for_chain(BASE)

    rec = ex.get_record(BASE, 600)
    assert rec.status == CustodyTxStatus.QUEUED
    assert rec.retry_count >= 1


@pytest.mark.asyncio
async def test_broadcast_intrinsic_gas_rejection_escalates(state_dir, accounting, web3s):
    """send_raw_transaction raising 'intrinsic gas too low' is a real rejection,
    not transient → AWAITING_CLEAR."""
    eth = web3s[BASE].eth
    eth.send_raw_transaction.side_effect = [ValueError("intrinsic gas too low")]
    eth.get_transaction_count = AsyncMock(return_value=601)

    ex = _make_executor(state_dir, accounting)
    await _seed_onchain_nonce(ex, BASE, 601)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=601,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="bad-gas",
            signed_tx=bytes.fromhex("de0259beef"),
        )
    )
    await ex._process_next_for_chain(BASE)

    rec = ex.get_record(BASE, 601)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR


# --- 7: periodic self-heal promotes blocked records whose root cause cleared ---


RECOVERED_TX_HASH = "0x" + "fe" * 32
RECOVERED_BLOCK = 4242


class _FakeCall:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        return self._result


class _FakeMintedEvent:
    def __init__(self, logs: list) -> None:
        self._logs = logs

    async def get_logs(self, *args: Any, **kwargs: Any) -> list:
        return self._logs


class _FakeBridgeContract:
    """Stand-in for the bound ROFLBridge contract the duplicate-id verifier
    reads: the processed-id mapping plus the matching event log."""

    def __init__(self, *, mapping_attr: str, processed: bool, logs: list) -> None:
        self.functions = SimpleNamespace(**{mapping_attr: lambda _id: _FakeCall(processed)})
        event_name = "Minted" if mapping_attr == "mintedWithdrawalIds" else "Burned"
        self.events = SimpleNamespace(**{event_name: _FakeMintedEvent(logs)})


def _minted_log(*, to: str, amount: int) -> dict:
    return {
        "args": {"to": to, "amount": amount},
        "transactionHash": HexBytes(RECOVERED_TX_HASH),
        "blockNumber": RECOVERED_BLOCK,
    }


def _install_foreign_mint(
    executor: CustodyTxExecutor,
    web3s: Dict[int, Any],
    *,
    chain_id: int,
    nonce: int,
    event_amount: int,
    sender: str = CUSTODY_ADDRESS,
) -> None:
    """Wire the bridge cache + foreign-tx lookup so duplicate-id recovery for a
    blocked BASE_MINT finds a single matching ``Minted`` event."""
    logs = [_minted_log(to=TO_ADDRESS, amount=event_amount)]
    bridge = _FakeBridgeContract(mapping_attr="mintedWithdrawalIds", processed=True, logs=logs)
    executor._bridge_contract_cache[(chain_id, Web3.to_checksum_address(ROUTE_ADDRESS))] = bridge
    eth = web3s[chain_id].eth
    eth.get_transaction = AsyncMock(return_value={"from": sender, "nonce": nonce})


async def _seed_blocked_base_mint(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    amount: int = AMOUNT,
) -> str:
    await _seed_onchain_nonce(executor, BASE, nonce)
    key = await executor.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=nonce,
            kind=CustodyTxKind.BASE_MINT,
            id=str(WITHDRAWAL_INDEX),
            signed_tx=bytes.fromhex("de07a0beef"),
            route_address=ROUTE_ADDRESS,
            max_gas_cost=10**15,
            withdrawal_index=WITHDRAWAL_INDEX,
            to_address=TO_ADDRESS,
            amount=amount,
        )
    )
    rec = executor.get_record(BASE, nonce)
    executor._mark_status(rec, CustodyTxStatus.AWAITING_CLEAR, error="seeded block")
    return key


@pytest.mark.asyncio
async def test_self_heal_promotes_foreign_tx_race_base_mint(state_dir, accounting, web3s):
    """A BASE_MINT parked in AWAITING_CLEAR whose duplicate-id recovery now finds
    a matching foreign Minted event is promoted to SUCCESS with recovered_tx_hash
    pinned. The broadcast tx_hash is NOT clobbered (pin_tx_hash=False path)."""
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_base_mint(ex, nonce=700)
    _install_foreign_mint(ex, web3s, chain_id=BASE, nonce=700, event_amount=AMOUNT)

    promoted = await ex._self_heal_blocked(BASE)

    assert promoted is True
    rec = ex.get_record(BASE, 700)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.recovered_tx_hash == RECOVERED_TX_HASH
    assert rec.recovered_block_number == RECOVERED_BLOCK


@pytest.mark.asyncio
async def test_self_heal_leaves_mismatched_foreign_event_blocked(state_dir, accounting, web3s):
    """A foreign Minted event with a different amount is an operator
    inconsistency, not a recovery: the record stays AWAITING_CLEAR and the sweep
    reports no promotion."""
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_base_mint(ex, nonce=701, amount=AMOUNT)
    _install_foreign_mint(ex, web3s, chain_id=BASE, nonce=701, event_amount=AMOUNT + 1)

    promoted = await ex._self_heal_blocked(BASE)

    assert promoted is False
    rec = ex.get_record(BASE, 701)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert rec.recovered_tx_hash is None


@pytest.mark.asyncio
async def test_self_heal_promotes_late_mined_receipt_non_bridge(state_dir, accounting, web3s):
    """A SAPPHIRE_RELEASE parked in AWAITING_CLEAR with a tx_hash whose receipt now
    returns status==1 is reconciled to SUCCESS by the sweep."""
    eth = web3s[SAPPHIRE].eth
    eth.get_transaction_receipt = AsyncMock(return_value=_make_receipt(status=1, block_number=999))

    ex = _make_executor(state_dir, accounting)
    await _enqueue_sapphire_release(ex, nonce=800)
    rec = ex.get_record(SAPPHIRE, 800)
    rec.tx_hash = _tx_hash_for(SAPPHIRE, 800)
    ex._mark_status(rec, CustodyTxStatus.AWAITING_CLEAR, error="seeded block")

    promoted = await ex._self_heal_blocked(SAPPHIRE)

    assert promoted is True
    rec_final = ex.get_record(SAPPHIRE, 800)
    assert rec_final.status == CustodyTxStatus.SUCCESS
    assert rec_final.receipt_block_number == 999


@pytest.mark.asyncio
async def test_self_heal_keeps_reverted_receipt_blocked(state_dir, accounting, web3s):
    """A status==0 receipt is negative evidence: reconcile re-marks AWAITING_CLEAR
    and the sweep reports no promotion (a non-bridge record has no event-log
    recovery path)."""
    eth = web3s[SAPPHIRE].eth
    eth.get_transaction_receipt = AsyncMock(return_value=_make_receipt(status=0, block_number=999))

    ex = _make_executor(state_dir, accounting)
    await _enqueue_sapphire_release(ex, nonce=801)
    rec = ex.get_record(SAPPHIRE, 801)
    rec.tx_hash = _tx_hash_for(SAPPHIRE, 801)
    ex._mark_status(rec, CustodyTxStatus.AWAITING_CLEAR, error="seeded block")

    promoted = await ex._self_heal_blocked(SAPPHIRE)

    assert promoted is False
    assert ex.get_record(SAPPHIRE, 801).status == CustodyTxStatus.AWAITING_CLEAR


# --- 8: same-tick continuation is budget-bounded; no-op when nothing promotes ---


@pytest.mark.asyncio
async def test_self_heal_pass_drain_is_budget_bounded(state_dir, accounting, monkeypatch):
    """When the sweep promotes ≥1 record, the same-tick drain calls
    _process_next_for_chain exactly `same_tick_promotion_budget` times — it does
    NOT drain unboundedly."""
    budget = 3
    ex = _make_executor(state_dir, accounting, same_tick_promotion_budget=budget)

    monkeypatch.setattr(ex, "_self_heal_blocked", AsyncMock(return_value=True))
    process_spy = AsyncMock()
    monkeypatch.setattr(ex, "_process_next_for_chain", process_spy)

    await ex._run_self_heal_pass(BASE)

    assert process_spy.await_count == budget


@pytest.mark.asyncio
async def test_self_heal_pass_yields_between_drains(state_dir, accounting, monkeypatch):
    """The `asyncio.sleep(0)` between drains lets a competing task interleave, so
    one chain's backlog drain can't starve the cooperative scheduler."""
    budget = 4
    ex = _make_executor(state_dir, accounting, same_tick_promotion_budget=budget)
    monkeypatch.setattr(ex, "_self_heal_blocked", AsyncMock(return_value=True))
    monkeypatch.setattr(ex, "_process_next_for_chain", AsyncMock())

    interleaved = 0

    async def _competitor() -> None:
        nonlocal interleaved
        while True:
            interleaved += 1
            await asyncio.sleep(0)

    competitor = asyncio.create_task(_competitor())
    await ex._run_self_heal_pass(BASE)
    competitor.cancel()

    assert interleaved >= budget


@pytest.mark.asyncio
async def test_self_heal_no_promotable_records_is_noop(state_dir, accounting, web3s, monkeypatch):
    """Over a chain with only non-blocking records (SUCCESS/QUEUED/BROADCAST),
    _self_heal_blocked returns False and _run_self_heal_pass never drains."""
    eth = web3s[BASE].eth
    eth.send_raw_transaction.side_effect = [HexBytes(_tx_hash_for(BASE, 900))]
    eth.get_transaction_count = AsyncMock(return_value=900)

    async def _no_receipt_yet(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    eth.get_transaction_receipt.side_effect = _no_receipt_yet

    ex = _make_executor(
        state_dir,
        accounting,
        receipt_probe_interval=1,
        receipt_stuck_deadline_seconds=3600.0,
    )
    # SUCCESS record at nonce 900.
    await _seed_onchain_nonce(ex, BASE, 900)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=900,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="done",
            signed_tx=bytes.fromhex("de0384beef"),
        )
    )
    done = ex.get_record(BASE, 900)
    ex._mark_status(done, CustodyTxStatus.SUCCESS)
    # QUEUED record at nonce 901.
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=901,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="pending",
            signed_tx=bytes.fromhex("de0385beef"),
        )
    )

    assert await ex._self_heal_blocked(BASE) is False

    process_spy = AsyncMock()
    monkeypatch.setattr(ex, "_process_next_for_chain", process_spy)
    await ex._run_self_heal_pass(BASE)
    assert process_spy.await_count == 0


# === owner-authorized custody-tx clear: per-status x per-kind allowlist ===
#
# Most cases drive executor._apply_clear_action directly: the watcher's
# on-chain cross-check is exercised separately in the watcher section below.
# The verdict is observable via the clear.applied_total metric log line and via
# the record's resulting status; tests assert both where it disambiguates.

BURN_RAW_TX = b"\xbe" * 110
BURN_TX_HASH = "0x" + "b0" * 32


def _clear_verdict(caplog, action: ClearAction) -> str:
    """Pull the verdict label off the clear.applied_total metric log line."""
    for record in reversed(caplog.records):
        msg = record.getMessage()
        if "custody_tx.clear.applied_total" in msg and f"action={action.name}" in msg:
            for tok in msg.split():
                if tok.startswith("verdict="):
                    return tok.split("=", 1)[1]
    raise AssertionError(f"no clear.applied_total metric for action={action.name}")


async def _seed_blocked_sapphire_release(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    status: CustodyTxStatus = CustodyTxStatus.AWAITING_CLEAR,
    has_broadcast: bool = False,
) -> None:
    """Seed a SAPPHIRE_RELEASE record parked at a blocking status."""
    await _enqueue_sapphire_release(executor, nonce=nonce)
    rec = executor.get_record(SAPPHIRE, nonce)
    if has_broadcast:
        rec.broadcast_hashes.append(_tx_hash_for(SAPPHIRE, nonce))
        rec.tx_hash = _tx_hash_for(SAPPHIRE, nonce)
    executor._mark_status(rec, status, error="seeded block")


def _burned_log(*, amount: int) -> dict:
    return {
        "args": {"amount": amount},
        "transactionHash": HexBytes(RECOVERED_TX_HASH),
        "blockNumber": RECOVERED_BLOCK,
    }


def _install_foreign_burn(
    executor: CustodyTxExecutor,
    web3s: Dict[int, Any],
    *,
    chain_id: int,
    nonce: int,
    event_amount: int,
    sender: str = CUSTODY_ADDRESS,
) -> None:
    """Wire the bridge cache + foreign-tx lookup so duplicate-id recovery for a
    blocked XROSE_BURN finds a single matching ``Burned`` event."""
    logs = [_burned_log(amount=event_amount)]
    bridge = _FakeBridgeContract(mapping_attr="burnedDepositIds", processed=True, logs=logs)
    executor._bridge_contract_cache[(chain_id, Web3.to_checksum_address(ROUTE_ADDRESS))] = bridge
    eth = web3s[chain_id].eth
    eth.get_transaction = AsyncMock(return_value={"from": sender, "nonce": nonce})


async def _seed_blocked_xrose_burn(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    amount: int = AMOUNT,
    has_broadcast: bool = True,
) -> str:
    """Seed an XROSE_BURN record (32-byte hex id) parked in AWAITING_CLEAR."""
    deposit_id = "0x" + f"{nonce:064x}"
    await _seed_onchain_nonce(executor, BASE, nonce)
    key = await executor.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=nonce,
            kind=CustodyTxKind.XROSE_BURN,
            id=deposit_id,
            signed_tx=bytes.fromhex("de07b0beef"),
            route_address=ROUTE_ADDRESS,
            amount=amount,
        )
    )
    rec = executor.get_record(BASE, nonce)
    if has_broadcast:
        rec.broadcast_hashes.append(_tx_hash_for(BASE, nonce))
    executor._mark_status(rec, CustodyTxStatus.AWAITING_CLEAR, error="seeded block")
    return key


def _sapphire_release_vouched_tx(
    record,
    *,
    value_override: int | None = None,
) -> dict:
    """A vouched tx whose shape matches a SAPPHIRE_RELEASE record."""
    value = (
        value_override
        if value_override is not None
        else int(record.amount) - int(record.max_gas_cost)
    )
    return {
        "to": record.to_address,
        "from": record.evm_sender,
        "value": value,
        "input": "0x",
        "gas": SAPPHIRE_RELEASE_GAS_LIMIT,
        "nonce": record.evm_nonce,
    }


# --- Requeue: AWAITING_CLEAR(broadcast) -> QUEUED; GAS_CAP refused ---


@pytest.mark.asyncio
async def test_clear_requeue_awaiting_clear_resets_to_queued(state_dir, accounting):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1000, has_broadcast=True)
    rec = ex.get_record(SAPPHIRE, 1000)
    rec.retry_count = 5
    rec.stuck_since = 123.0
    ex._save_record(rec)

    await ex._apply_clear_action(SAPPHIRE, 1000, ClearAction.REQUEUE, "0x" + "00" * 32)

    out = ex.get_record(SAPPHIRE, 1000)
    assert out.status == CustodyTxStatus.QUEUED
    assert out.error is None
    assert out.retry_count == 0
    assert out.stuck_since is None


@pytest.mark.asyncio
async def test_clear_requeue_refused_on_gas_cap(state_dir, accounting, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(
        ex, nonce=1001, status=CustodyTxStatus.AWAITING_CLEAR_GAS_CAP, has_broadcast=True
    )

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(SAPPHIRE, 1001, ClearAction.REQUEUE, "0x" + "00" * 32)

    assert ex.get_record(SAPPHIRE, 1001).status == CustodyTxStatus.AWAITING_CLEAR_GAS_CAP
    assert _clear_verdict(caplog, ClearAction.REQUEUE) == "refused_status"


# --- Abandon: broadcast -> FAILED_FINAL; never-broadcast precondition gate ---


@pytest.mark.asyncio
async def test_clear_abandon_accepted_when_broadcast(state_dir, accounting):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1010, has_broadcast=True)

    await ex._apply_clear_action(SAPPHIRE, 1010, ClearAction.ABANDON, "0x" + "00" * 32)

    assert ex.get_record(SAPPHIRE, 1010).status == CustodyTxStatus.FAILED_FINAL


@pytest.mark.asyncio
async def test_clear_abandon_deferred_never_broadcast_nonce_not_advanced(
    state_dir, accounting, web3s
):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1011, has_broadcast=False)
    # On-chain nonce has NOT advanced past the record's nonce.
    web3s[SAPPHIRE].eth.get_transaction_count = AsyncMock(return_value=1011)

    verdict = await ex._apply_clear_action(SAPPHIRE, 1011, ClearAction.ABANDON, "0x" + "00" * 32)

    # Abandoning now would wedge the nonce floor; the clear is parked, not dropped.
    assert verdict == "deferred"
    assert ex.get_record(SAPPHIRE, 1011).status == CustodyTxStatus.AWAITING_CLEAR


@pytest.mark.asyncio
async def test_clear_abandon_accepted_never_broadcast_but_nonce_advanced(
    state_dir, accounting, web3s
):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1012, has_broadcast=False)
    # On-chain nonce already advanced past this slot: a foreign tx burned it.
    web3s[SAPPHIRE].eth.get_transaction_count = AsyncMock(return_value=1013)

    await ex._apply_clear_action(SAPPHIRE, 1012, ClearAction.ABANDON, "0x" + "00" * 32)

    assert ex.get_record(SAPPHIRE, 1012).status == CustodyTxStatus.FAILED_FINAL


# --- MarkSuccessWithHash: per-kind proof gate ---


@pytest.mark.asyncio
async def test_clear_mark_success_sapphire_release_shape_match(state_dir, accounting, web3s):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1100, has_broadcast=True)
    rec = ex.get_record(SAPPHIRE, 1100)
    vouched = "0x" + "a1" * 32
    web3s[SAPPHIRE].eth.get_transaction = AsyncMock(return_value=_sapphire_release_vouched_tx(rec))
    web3s[SAPPHIRE].eth.get_transaction_receipt = AsyncMock(
        return_value=_make_receipt(status=1, block_number=777)
    )

    await ex._apply_clear_action(SAPPHIRE, 1100, ClearAction.MARK_SUCCESS_WITH_HASH, vouched)

    out = ex.get_record(SAPPHIRE, 1100)
    assert out.status == CustodyTxStatus.SUCCESS
    assert HexBytes(out.recovered_tx_hash) == HexBytes(vouched)


@pytest.mark.asyncio
async def test_clear_mark_success_sapphire_release_shape_mismatch(
    state_dir, accounting, web3s, caplog
):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1101, has_broadcast=True)
    rec = ex.get_record(SAPPHIRE, 1101)
    vouched = "0x" + "a2" * 32
    web3s[SAPPHIRE].eth.get_transaction = AsyncMock(
        return_value=_sapphire_release_vouched_tx(rec, value_override=1)
    )
    web3s[SAPPHIRE].eth.get_transaction_receipt = AsyncMock(
        return_value=_make_receipt(status=1, block_number=777)
    )

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(SAPPHIRE, 1101, ClearAction.MARK_SUCCESS_WITH_HASH, vouched)

    assert ex.get_record(SAPPHIRE, 1101).status == CustodyTxStatus.AWAITING_CLEAR
    assert _clear_verdict(caplog, ClearAction.MARK_SUCCESS_WITH_HASH) == "refused_proof"


@pytest.mark.asyncio
async def test_clear_mark_success_base_mint_event_match(state_dir, accounting, web3s):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_base_mint(ex, nonce=1110)
    rec = ex.get_record(BASE, 1110)
    rec.broadcast_hashes.append(_tx_hash_for(BASE, 1110))
    ex._save_record(rec)
    _install_foreign_mint(ex, web3s, chain_id=BASE, nonce=1110, event_amount=AMOUNT)

    await ex._apply_clear_action(BASE, 1110, ClearAction.MARK_SUCCESS_WITH_HASH, RECOVERED_TX_HASH)

    assert ex.get_record(BASE, 1110).status == CustodyTxStatus.SUCCESS


@pytest.mark.asyncio
async def test_clear_mark_success_base_mint_vouched_mismatch(state_dir, accounting, web3s, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_base_mint(ex, nonce=1111)
    rec = ex.get_record(BASE, 1111)
    rec.broadcast_hashes.append(_tx_hash_for(BASE, 1111))
    ex._save_record(rec)
    _install_foreign_mint(ex, web3s, chain_id=BASE, nonce=1111, event_amount=AMOUNT)

    with caplog.at_level("INFO"):
        # Event recovered tx is RECOVERED_TX_HASH; vouch a different hash.
        await ex._apply_clear_action(
            BASE, 1111, ClearAction.MARK_SUCCESS_WITH_HASH, "0x" + "cc" * 32
        )

    assert ex.get_record(BASE, 1111).status == CustodyTxStatus.AWAITING_CLEAR
    assert _clear_verdict(caplog, ClearAction.MARK_SUCCESS_WITH_HASH) == "refused_proof"


@pytest.mark.asyncio
async def test_clear_mark_success_xrose_burn_event_match(state_dir, accounting, web3s):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_xrose_burn(ex, nonce=1120)
    _install_foreign_burn(ex, web3s, chain_id=BASE, nonce=1120, event_amount=AMOUNT)

    await ex._apply_clear_action(BASE, 1120, ClearAction.MARK_SUCCESS_WITH_HASH, RECOVERED_TX_HASH)

    assert ex.get_record(BASE, 1120).status == CustodyTxStatus.SUCCESS


@pytest.mark.asyncio
async def test_clear_mark_success_xrose_burn_vouched_mismatch(state_dir, accounting, web3s, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_xrose_burn(ex, nonce=1121)
    _install_foreign_burn(ex, web3s, chain_id=BASE, nonce=1121, event_amount=AMOUNT)

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(
            BASE, 1121, ClearAction.MARK_SUCCESS_WITH_HASH, "0x" + "dd" * 32
        )

    assert ex.get_record(BASE, 1121).status == CustodyTxStatus.AWAITING_CLEAR
    assert _clear_verdict(caplog, ClearAction.MARK_SUCCESS_WITH_HASH) == "refused_proof"


@pytest.mark.asyncio
async def test_clear_mark_success_normal_withdrawal_refused(state_dir, accounting, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_onchain_nonce(ex, BASE, 1130)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=1130,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="nw",
            signed_tx=bytes.fromhex("de0460beef"),
        )
    )
    rec = ex.get_record(BASE, 1130)
    rec.broadcast_hashes.append(_tx_hash_for(BASE, 1130))
    ex._mark_status(rec, CustodyTxStatus.AWAITING_CLEAR, error="seeded block")

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(
            BASE, 1130, ClearAction.MARK_SUCCESS_WITH_HASH, "0x" + "11" * 32
        )

    assert ex.get_record(BASE, 1130).status == CustodyTxStatus.AWAITING_CLEAR
    assert _clear_verdict(caplog, ClearAction.MARK_SUCCESS_WITH_HASH) == "refused_proof"


@pytest.mark.asyncio
async def test_clear_mark_success_refused_when_never_broadcast(state_dir, accounting, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1140, has_broadcast=False)

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(
            SAPPHIRE, 1140, ClearAction.MARK_SUCCESS_WITH_HASH, "0x" + "11" * 32
        )

    assert ex.get_record(SAPPHIRE, 1140).status == CustodyTxStatus.AWAITING_CLEAR
    assert _clear_verdict(caplog, ClearAction.MARK_SUCCESS_WITH_HASH) == "refused_status"


# --- BurnNonce: round-trip, drain, and refusals ---


@pytest.mark.asyncio
async def test_clear_burn_nonce_starts_burn(state_dir, accounting, web3s):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1200, has_broadcast=False)
    accounting.sign_nonce_burn = AsyncMock(return_value=BURN_RAW_TX)
    web3s[SAPPHIRE].eth.send_raw_transaction = AsyncMock(return_value=HexBytes(BURN_TX_HASH))

    await ex._apply_clear_action(SAPPHIRE, 1200, ClearAction.BURN_NONCE, "0x" + "00" * 32)

    accounting.sign_nonce_burn.assert_awaited_once_with(SAPPHIRE, 1200)
    assert web3s[SAPPHIRE].eth.send_raw_transaction.await_count == 1
    out = ex.get_record(SAPPHIRE, 1200)
    assert out.status == CustodyTxStatus.BURNING_NONCE
    assert HexBytes(out.burn_nonce_tx_hash) == HexBytes(BURN_TX_HASH)
    assert out.status not in {CustodyTxStatus.SUCCESS, CustodyTxStatus.FAILED_FINAL}


@pytest.mark.asyncio
async def test_clear_burn_nonce_roundtrip_drains_downstream(state_dir, accounting, web3s):
    """Burn at nonce N mines -> FAILED_FINAL; the same _process_next_for_chain
    pass then advances the downstream runnable record at N+1."""
    ex = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    await _seed_blocked_sapphire_release(ex, nonce=1210, has_broadcast=False)
    # Downstream normal withdrawal at N+1, runnable.
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=SAPPHIRE,
            evm_nonce=1211,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="downstream",
            signed_tx=bytes.fromhex("de0461beef"),
        )
    )

    accounting.sign_nonce_burn = AsyncMock(return_value=BURN_RAW_TX)
    downstream_hash = _tx_hash_for(SAPPHIRE, 1211)
    downstream_hash_raw = bytes.fromhex("de0461beef")
    eth = web3s[SAPPHIRE].eth
    eth.send_raw_transaction = AsyncMock(
        side_effect=[HexBytes(BURN_TX_HASH), HexBytes(downstream_hash)]
    )
    # On-chain next nonce permits the downstream broadcast.
    eth.get_transaction_count = AsyncMock(return_value=1211)

    async def _receipt(tx_hash: Any) -> Dict[str, Any]:
        h = HexBytes(tx_hash).to_0x_hex()
        if h == HexBytes(BURN_TX_HASH).to_0x_hex():
            return _make_receipt(status=1, block_number=900)
        raise TransactionNotFound(h)

    eth.get_transaction_receipt.side_effect = _receipt

    await ex._apply_clear_action(SAPPHIRE, 1210, ClearAction.BURN_NONCE, "0x" + "00" * 32)
    assert ex.get_record(SAPPHIRE, 1210).status == CustodyTxStatus.BURNING_NONCE

    await ex._process_next_for_chain(SAPPHIRE)

    # Burn mined and abandoned the slot; the same pass continued to the
    # downstream record and broadcast it (drain-this-pass).
    assert ex.get_record(SAPPHIRE, 1210).status == CustodyTxStatus.FAILED_FINAL
    assert eth.send_raw_transaction.await_args_list[-1].args[0] == downstream_hash_raw


@pytest.mark.asyncio
async def test_clear_burn_nonce_refused_when_broadcast(state_dir, accounting, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1220, has_broadcast=True)

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(SAPPHIRE, 1220, ClearAction.BURN_NONCE, "0x" + "00" * 32)

    assert ex.get_record(SAPPHIRE, 1220).status == CustodyTxStatus.AWAITING_CLEAR
    assert _clear_verdict(caplog, ClearAction.BURN_NONCE) == "refused_status"
    accounting.sign_nonce_burn.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_burn_nonce_refused_on_gas_cap(state_dir, accounting, caplog):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(
        ex, nonce=1221, status=CustodyTxStatus.AWAITING_CLEAR_GAS_CAP, has_broadcast=False
    )

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(SAPPHIRE, 1221, ClearAction.BURN_NONCE, "0x" + "00" * 32)

    assert ex.get_record(SAPPHIRE, 1221).status == CustodyTxStatus.AWAITING_CLEAR_GAS_CAP
    assert _clear_verdict(caplog, ClearAction.BURN_NONCE) == "refused_status"


# --- BURNING_NONCE chain-loop routing + restart durability ---


@pytest.mark.asyncio
async def test_burning_nonce_never_routed_to_broadcast(state_dir, accounting, web3s, monkeypatch):
    """A BURNING_NONCE record whose burn is not yet mined must drive the burn
    reconcile path and return — never fall through to _broadcast_record, and
    never advance downstream."""
    ex = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    await _seed_blocked_sapphire_release(ex, nonce=1300, has_broadcast=False)
    await ex.enqueue(
        CustodyTxRequest(
            chain_id=SAPPHIRE,
            evm_nonce=1301,
            kind=CustodyTxKind.NORMAL_WITHDRAWAL,
            id="downstream-blocked",
            signed_tx=bytes.fromhex("de0462beef"),
        )
    )
    accounting.sign_nonce_burn = AsyncMock(return_value=BURN_RAW_TX)
    eth = web3s[SAPPHIRE].eth
    eth.send_raw_transaction = AsyncMock(return_value=HexBytes(BURN_TX_HASH))
    await ex._apply_clear_action(SAPPHIRE, 1300, ClearAction.BURN_NONCE, "0x" + "00" * 32)

    # Burn not mined yet.
    async def _no_receipt(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    eth.get_transaction_receipt.side_effect = _no_receipt

    async def _boom(_rec: Any) -> None:
        raise AssertionError("_broadcast_record must never run for a BURNING_NONCE slot")

    monkeypatch.setattr(ex, "_broadcast_record", _boom)

    await ex._process_next_for_chain(SAPPHIRE)

    assert ex.get_record(SAPPHIRE, 1300).status == CustodyTxStatus.BURNING_NONCE
    # Downstream stays blocked behind the in-flight burn.
    assert ex.get_record(SAPPHIRE, 1301).status == CustodyTxStatus.QUEUED


@pytest.mark.asyncio
async def test_burning_nonce_survives_restart_and_is_redriven(state_dir, accounting, web3s):
    """A BURNING_NONCE record with burn_nonce_tx_hash persisted survives a fresh
    executor over the same state_dir and is re-driven to FAILED_FINAL."""
    ex = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    await _seed_blocked_sapphire_release(ex, nonce=1310, has_broadcast=False)
    accounting.sign_nonce_burn = AsyncMock(return_value=BURN_RAW_TX)
    web3s[SAPPHIRE].eth.send_raw_transaction = AsyncMock(return_value=HexBytes(BURN_TX_HASH))
    await ex._apply_clear_action(SAPPHIRE, 1310, ClearAction.BURN_NONCE, "0x" + "00" * 32)
    assert ex.get_record(SAPPHIRE, 1310).status == CustodyTxStatus.BURNING_NONCE

    # Fresh executor over the same state dir; burn now mines.
    ex2 = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    reloaded = ex2.get_record(SAPPHIRE, 1310)
    assert reloaded.status == CustodyTxStatus.BURNING_NONCE
    assert HexBytes(reloaded.burn_nonce_tx_hash) == HexBytes(BURN_TX_HASH)

    web3s[SAPPHIRE].eth.get_transaction_receipt = AsyncMock(
        return_value=_make_receipt(status=1, block_number=950)
    )
    await ex2._process_next_for_chain(SAPPHIRE)

    assert ex2.get_record(SAPPHIRE, 1310).status == CustodyTxStatus.FAILED_FINAL


# --- compare-and-swap: clear on a non-blocking record is a no-op ---


@pytest.mark.asyncio
async def test_clear_compare_and_swap_refuses_non_blocking(state_dir, accounting, caplog):
    """A clear arriving for a record whose reloaded status is no longer blocking
    (e.g. a concurrent self-heal already promoted it) must refuse and not mutate."""
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1400, has_broadcast=True)
    # Simulate the TOCTOU: on disk the record is already SUCCESS.
    rec = ex.get_record(SAPPHIRE, 1400)
    rec.status = CustodyTxStatus.SUCCESS
    ex._save_record(rec)

    with caplog.at_level("INFO"):
        await ex._apply_clear_action(SAPPHIRE, 1400, ClearAction.REQUEUE, "0x" + "00" * 32)

    assert ex.get_record(SAPPHIRE, 1400).status == CustodyTxStatus.SUCCESS
    assert _clear_verdict(caplog, ClearAction.REQUEUE) == "refused_status"


# === clear watcher: on-chain cross-check before applying ===


def _cleared_event(
    *,
    chain_id: int,
    nonce: int,
    action: ClearAction,
    vouched: bytes,
    tx_hash: str = "0x" + "ab" * 32,
    log_index: int = 0,
) -> dict:
    return {
        "transactionHash": HexBytes(tx_hash),
        "logIndex": log_index,
        "args": {
            "chainId": chain_id,
            "nonce": nonce,
            "action": int(action),
            "vouchedTxHash": HexBytes(vouched),
        },
    }


def _make_watcher(accounting, state_dir: str):
    executor = SimpleNamespace(_apply_clear_action=AsyncMock())
    watcher = CustodyTxClearWatcher(
        accounting,
        executor,
        state_dir=Path(state_dir),
        sapphire_chain_id=SAPPHIRE,
    )
    return watcher, executor


@pytest.mark.asyncio
async def test_clear_watcher_contract_disagreement(state_dir, accounting, caplog):
    """clearAppliedHash != recomputed expected -> apply NOT called, CRITICAL +
    disagreement metric emitted, and the event is consumed (marked seen)."""
    watcher, executor = _make_watcher(accounting, state_dir)
    vouched = b"\x00" * 32
    accounting.get_clear_applied_hash = AsyncMock(return_value=b"\xff" * 32)
    event = _cleared_event(chain_id=SAPPHIRE, nonce=10, action=ClearAction.REQUEUE, vouched=vouched)

    with caplog.at_level("INFO"):
        held = await watcher._process_event(event, None)

    executor._apply_clear_action.assert_not_awaited()
    assert any("contract_disagreement_total" in r.getMessage() for r in caplog.records)
    # Permanent disagreement fails closed: consumed, cursor not held back.
    assert held is None
    assert (HexBytes(event["transactionHash"]).to_0x_hex(), event["logIndex"]) in watcher._seen


@pytest.mark.asyncio
async def test_clear_watcher_match_applies(state_dir, accounting):
    """clearAppliedHash == recomputed expected -> apply called once with decoded
    (chain_id, nonce, ClearAction, vouched hex)."""
    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.return_value = "applied"
    vouched = bytes(HexBytes("0x" + "77" * 32))
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.ABANDON), vouched)
    )
    event = _cleared_event(chain_id=SAPPHIRE, nonce=20, action=ClearAction.ABANDON, vouched=vouched)

    await watcher._process_event(event, None)

    executor._apply_clear_action.assert_awaited_once_with(
        SAPPHIRE, 20, ClearAction.ABANDON, HexBytes(vouched).to_0x_hex()
    )


@pytest.mark.asyncio
async def test_clear_watcher_seen_set_dedup(state_dir, accounting):
    """The same event processed twice applies only once: a terminal apply records
    it in the seen-set, so the second pass short-circuits."""
    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.return_value = "applied"
    vouched = b"\x00" * 32
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.REQUEUE), vouched)
    )
    event = _cleared_event(chain_id=SAPPHIRE, nonce=30, action=ClearAction.REQUEUE, vouched=vouched)

    await watcher._process_event(event, None)
    await watcher._process_event(event, None)

    assert executor._apply_clear_action.await_count == 1


@pytest.mark.asyncio
async def test_clear_watcher_disabled_on_pre_upgrade_contract(state_dir, accounting, caplog):
    """start() probing clearAppliedHash and getting BadFunctionCallOutput (pre-
    upgrade contract) disables the watcher: no task, _running stays False, and a
    watcher_disabled metric/WARNING is emitted."""
    watcher, _executor = _make_watcher(accounting, state_dir)
    accounting.get_clear_applied_hash = AsyncMock(
        side_effect=BadFunctionCallOutput("no such selector")
    )

    with caplog.at_level("INFO"):
        await watcher.start()

    assert watcher._running is False
    assert watcher._task is None
    assert any("watcher_disabled" in r.getMessage() for r in caplog.records)


# === deferred clears are durable: parked and retried until terminal ===


@pytest.mark.asyncio
async def test_deferred_clear_parks_then_graduates_on_retry(state_dir, accounting):
    """A clear whose first apply defers is parked in _pending; once the apply
    turns terminal, the next _retry_pending graduates it into _seen."""
    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.side_effect = ["deferred", "applied"]
    vouched = b"\x00" * 32
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.BURN_NONCE), vouched)
    )
    event = _cleared_event(
        chain_id=SAPPHIRE, nonce=40, action=ClearAction.BURN_NONCE, vouched=vouched
    )

    await watcher._process_event(event, None)
    assert watcher._pending  # deferred → parked
    assert (HexBytes(event["transactionHash"]).to_0x_hex(), event["logIndex"]) not in watcher._seen

    await watcher._retry_pending()

    assert not watcher._pending  # terminal → graduated out
    assert (HexBytes(event["transactionHash"]).to_0x_hex(), event["logIndex"]) in watcher._seen
    assert executor._apply_clear_action.await_count == 2


@pytest.mark.asyncio
async def test_deferred_clear_retried_without_log_rescan(state_dir, accounting):
    """A parked clear is re-driven by _retry_pending alone, with no log scan or
    event re-delivery — retry does not depend on the discovery cursor/window."""
    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.return_value = "deferred"
    vouched = b"\x00" * 32
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.ABANDON), vouched)
    )
    event = _cleared_event(chain_id=SAPPHIRE, nonce=41, action=ClearAction.ABANDON, vouched=vouched)

    await watcher._process_event(event, None)
    assert watcher._pending
    first_count = executor._apply_clear_action.await_count

    await watcher._retry_pending()

    # Re-driven from the pending set, not via the log cursor; still parked.
    assert executor._apply_clear_action.await_count == first_count + 1
    assert watcher._pending


@pytest.mark.asyncio
async def test_perpetually_deferred_clear_emits_stalled_critical(state_dir, accounting, caplog):
    """A clear that defers forever (e.g. Abandon on a never-broadcast, never-
    advancing nonce) counts consecutive deferrals and emits a gated CRITICAL +
    deferred_stalled metric on the Nth retry. Observability only: the entry stays
    parked and is never consumed."""
    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.return_value = "deferred"
    vouched = b"\x00" * 32
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.ABANDON), vouched)
    )
    event = _cleared_event(chain_id=SAPPHIRE, nonce=42, action=ClearAction.ABANDON, vouched=vouched)
    await watcher._process_event(event, None)  # park at retries=0

    with caplog.at_level("INFO"):
        for _ in range(_DEFERRED_STALL_RETRY_THRESHOLD):
            await watcher._retry_pending()

    entry = watcher._pending[_pending_key(SAPPHIRE, 42)]
    assert entry["retries"] == _DEFERRED_STALL_RETRY_THRESHOLD
    crit = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("operator action required" in r.getMessage() for r in crit)
    assert any("custody_tx.clear.deferred_stalled" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_deferred_clear_no_stalled_critical_before_threshold(state_dir, accounting, caplog):
    """Below the threshold, a deferred clear escalates nothing — the CRITICAL is
    gated, not per-pass."""
    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.return_value = "deferred"
    vouched = b"\x00" * 32
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.ABANDON), vouched)
    )
    event = _cleared_event(chain_id=SAPPHIRE, nonce=43, action=ClearAction.ABANDON, vouched=vouched)
    await watcher._process_event(event, None)

    with caplog.at_level("INFO"):
        for _ in range(_DEFERRED_STALL_RETRY_THRESHOLD - 1):
            await watcher._retry_pending()

    assert not any("deferred_stalled" in r.getMessage() for r in caplog.records)


# === executor: _apply_clear_action returns the verdict the watcher acts on ===


@pytest.mark.asyncio
async def test_apply_clear_action_returns_applied(state_dir, accounting):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1500, has_broadcast=True)

    verdict = await ex._apply_clear_action(SAPPHIRE, 1500, ClearAction.REQUEUE, "0x" + "00" * 32)

    assert verdict == "applied"
    assert ex.get_record(SAPPHIRE, 1500).status == CustodyTxStatus.QUEUED


@pytest.mark.asyncio
async def test_apply_clear_action_returns_refused_status(state_dir, accounting):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(
        ex, nonce=1501, status=CustodyTxStatus.AWAITING_CLEAR_GAS_CAP, has_broadcast=True
    )

    verdict = await ex._apply_clear_action(SAPPHIRE, 1501, ClearAction.REQUEUE, "0x" + "00" * 32)

    assert verdict == "refused_status"
    assert ex.get_record(SAPPHIRE, 1501).status == CustodyTxStatus.AWAITING_CLEAR_GAS_CAP


@pytest.mark.asyncio
async def test_apply_clear_action_returns_deferred(state_dir, accounting, web3s):
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1502, has_broadcast=False)
    web3s[SAPPHIRE].eth.get_transaction_count = AsyncMock(return_value=1502)

    verdict = await ex._apply_clear_action(SAPPHIRE, 1502, ClearAction.ABANDON, "0x" + "00" * 32)

    assert verdict == "deferred"
    assert ex.get_record(SAPPHIRE, 1502).status == CustodyTxStatus.AWAITING_CLEAR


# === BurnNonce broadcast failure defers (record stays blocking, not dropped) ===


@pytest.mark.asyncio
async def test_burn_nonce_broadcast_failure_defers_then_succeeds(state_dir, accounting, web3s):
    """sign_nonce_burn ok but send_raw_transaction raising defers — the record
    stays AWAITING_CLEAR (not BURNING_NONCE) so the same clear re-drives the burn.
    A later attempt whose broadcast succeeds applies."""
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1600, has_broadcast=False)
    accounting.sign_nonce_burn = AsyncMock(return_value=BURN_RAW_TX)
    eth = web3s[SAPPHIRE].eth
    eth.send_raw_transaction = AsyncMock(side_effect=aiohttp.ServerDisconnectedError("dropped"))

    verdict = await ex._apply_clear_action(SAPPHIRE, 1600, ClearAction.BURN_NONCE, "0x" + "00" * 32)

    assert verdict == "deferred"
    assert ex.get_record(SAPPHIRE, 1600).status == CustodyTxStatus.AWAITING_CLEAR

    # Broadcast now succeeds: the re-driven clear applies and flips to BURNING_NONCE.
    eth.send_raw_transaction = AsyncMock(return_value=HexBytes(BURN_TX_HASH))
    verdict2 = await ex._apply_clear_action(
        SAPPHIRE, 1600, ClearAction.BURN_NONCE, "0x" + "00" * 32
    )

    assert verdict2 == "applied"
    out = ex.get_record(SAPPHIRE, 1600)
    assert out.status == CustodyTxStatus.BURNING_NONCE
    assert HexBytes(out.burn_nonce_tx_hash) == HexBytes(BURN_TX_HASH)


# === self-heal honors the per-slot clear lock ===


@pytest.mark.asyncio
async def test_self_heal_blocks_on_held_clear_lock(state_dir, accounting, web3s):
    """While an owner clear holds the per-slot lock, a concurrent self-heal pass
    cannot reload/mutate the record; it proceeds only after the lock releases."""
    eth = web3s[SAPPHIRE].eth
    eth.get_transaction_receipt = AsyncMock(return_value=_make_receipt(status=1, block_number=999))

    ex = _make_executor(state_dir, accounting)
    await _enqueue_sapphire_release(ex, nonce=1700)
    rec = ex.get_record(SAPPHIRE, 1700)
    rec.tx_hash = _tx_hash_for(SAPPHIRE, 1700)
    ex._mark_status(rec, CustodyTxStatus.AWAITING_CLEAR, error="seeded block")

    lock = ex._clear_lock(SAPPHIRE, 1700)
    await lock.acquire()
    try:
        heal = asyncio.create_task(ex._self_heal_blocked(SAPPHIRE))
        # Give the task room to run up to the lock and block there.
        await asyncio.sleep(0)
        assert not heal.done()
        assert ex.get_record(SAPPHIRE, 1700).status == CustodyTxStatus.AWAITING_CLEAR
    finally:
        lock.release()

    promoted = await heal
    assert promoted is True
    assert ex.get_record(SAPPHIRE, 1700).status == CustodyTxStatus.SUCCESS


@pytest.mark.asyncio
async def test_self_heal_skips_record_no_longer_blocking(state_dir, accounting, monkeypatch):
    """Self-heal reloads fresh under the lock: a record flipped out of
    BLOCKING_STATUSES on disk (e.g. BURNING_NONCE) is skipped — it is never
    promoted to SUCCESS even if duplicate-id recovery would report a match."""
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_base_mint(ex, nonce=1701)
    rec = ex.get_record(BASE, 1701)
    rec.status = CustodyTxStatus.BURNING_NONCE
    rec.burn_nonce_tx_hash = BURN_TX_HASH
    ex._save_record(rec)

    monkeypatch.setattr(
        ex,
        "_attempt_duplicate_id_recovery",
        AsyncMock(
            return_value=PreflightDecision(
                outcome=PreflightOutcome.MARK_RECOVERED,
                recovered_tx_hash=RECOVERED_TX_HASH,
                recovered_block_number=RECOVERED_BLOCK,
            )
        ),
    )

    promoted = await ex._self_heal_blocked(BASE)

    assert promoted is False
    assert ex.get_record(BASE, 1701).status == CustodyTxStatus.BURNING_NONCE


# === BURNING_NONCE liveness: re-broadcast past the deadline; mined-revert terminal ===


@pytest.mark.asyncio
async def test_burn_rebroadcasts_past_deadline(state_dir, accounting, web3s, caplog):
    """A BURNING_NONCE record stuck past the wall-clock deadline whose burn is not
    yet mined re-broadcasts the burn (owner gas bump absorbed) and stays
    BURNING_NONCE, with a CRITICAL escalation."""
    ex = _make_executor(
        state_dir, accounting, receipt_probe_interval=1, receipt_stuck_deadline_seconds=0.0
    )
    await _seed_blocked_sapphire_release(ex, nonce=1800, has_broadcast=False)
    rec = ex.get_record(SAPPHIRE, 1800)
    rec.status = CustodyTxStatus.BURNING_NONCE
    rec.burn_nonce_tx_hash = BURN_TX_HASH
    rec.stuck_since = 1.0  # far in the past → past the (zero) deadline
    rec.retry_count = 0
    ex._save_record(rec)

    accounting.sign_nonce_burn = AsyncMock(return_value=BURN_RAW_TX)
    eth = web3s[SAPPHIRE].eth
    eth.send_raw_transaction = AsyncMock(return_value=HexBytes(BURN_TX_HASH))

    async def _no_receipt(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    eth.get_transaction_receipt.side_effect = _no_receipt

    with caplog.at_level("CRITICAL"):
        await ex._reconcile_burn_nonce(rec)

    accounting.sign_nonce_burn.assert_awaited()
    assert eth.send_raw_transaction.await_count == 1
    assert ex.get_record(SAPPHIRE, 1800).status == CustodyTxStatus.BURNING_NONCE
    assert any("operator must raise gas" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_burn_mined_with_revert_status_is_terminal(state_dir, accounting, web3s, caplog):
    """A burn that mines with status!=1 still consumed its nonce: reconcile marks
    FAILED_FINAL (not stuck in BURNING_NONCE) and records a CRITICAL."""
    ex = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    await _seed_blocked_sapphire_release(ex, nonce=1810, has_broadcast=False)
    rec = ex.get_record(SAPPHIRE, 1810)
    rec.status = CustodyTxStatus.BURNING_NONCE
    rec.burn_nonce_tx_hash = BURN_TX_HASH
    rec.stuck_since = 1.0
    ex._save_record(rec)

    web3s[SAPPHIRE].eth.get_transaction_receipt = AsyncMock(
        return_value=_make_receipt(status=0, block_number=950)
    )

    with caplog.at_level("CRITICAL"):
        await ex._reconcile_burn_nonce(rec)

    assert ex.get_record(SAPPHIRE, 1810).status == CustodyTxStatus.FAILED_FINAL
    assert any("anomalous receipt" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_burn_failed_final_when_nonce_advanced_but_tracked_hash_unmined(
    state_dir, accounting, web3s, caplog
):
    """A re-broadcast (new gas → new hash) overwrote burn_nonce_tx_hash, but an
    earlier sibling hash mined: the tracked latest hash has no receipt while the
    on-chain nonce has advanced past the slot. The slot is burned regardless →
    FAILED_FINAL, CRITICAL for forensics."""
    ex = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    await _seed_blocked_sapphire_release(ex, nonce=1820, has_broadcast=False)
    rec = ex.get_record(SAPPHIRE, 1820)
    rec.status = CustodyTxStatus.BURNING_NONCE
    rec.burn_nonce_tx_hash = BURN_TX_HASH
    rec.stuck_since = 1.0
    ex._save_record(rec)

    eth = web3s[SAPPHIRE].eth

    async def _no_receipt(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    eth.get_transaction_receipt.side_effect = _no_receipt
    # On-chain nonce advanced past the slot (a sibling burn hash mined).
    eth.get_transaction_count = AsyncMock(return_value=1821)

    with caplog.at_level("CRITICAL"):
        await ex._reconcile_burn_nonce(rec)

    assert ex.get_record(SAPPHIRE, 1820).status == CustodyTxStatus.FAILED_FINAL
    assert any("sibling burn hash" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_burn_receipt_rpc_outage_bumps_retry_and_persists(state_dir, accounting, web3s):
    """A non-TransactionNotFound receipt-lookup exception must not freeze the
    deadline / re-broadcast cadence: retry_count is bumped and stuck_since set
    BEFORE the lookup, and the record is persisted in the except branch so the
    wall-clock deadline still fires."""
    ex = _make_executor(state_dir, accounting, receipt_probe_interval=1)
    await _seed_blocked_sapphire_release(ex, nonce=1830, has_broadcast=False)
    rec = ex.get_record(SAPPHIRE, 1830)
    rec.status = CustodyTxStatus.BURNING_NONCE
    rec.burn_nonce_tx_hash = BURN_TX_HASH
    rec.stuck_since = None
    rec.retry_count = 0
    ex._save_record(rec)

    eth = web3s[SAPPHIRE].eth
    eth.get_transaction_receipt = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("receipt RPC down")
    )

    await ex._reconcile_burn_nonce(rec)

    out = ex.get_record(SAPPHIRE, 1830)
    assert out.status == CustodyTxStatus.BURNING_NONCE
    assert out.retry_count == 1
    assert out.stuck_since is not None


# === proof helpers: malformed records refuse (never raise) ===


@pytest.mark.asyncio
async def test_verify_mark_success_none_field_refuses(state_dir, accounting, web3s):
    """A SAPPHIRE_RELEASE record missing a shape-check field (max_gas_cost) is
    refused, not raised — the clear is consumable rather than crashing the pass."""
    ex = _make_executor(state_dir, accounting)
    await _seed_blocked_sapphire_release(ex, nonce=1900, has_broadcast=True)
    rec = ex.get_record(SAPPHIRE, 1900)
    rec.max_gas_cost = None
    w3 = web3s[SAPPHIRE]

    result, _reason = await verify_mark_success(ex, rec, w3, "0x" + "a1" * 32)

    assert result == "refused"


def test_same_address_none_pair_is_false():
    assert _same_address(None, None) is False
    assert _same_address("0xABCD", "0xabcd") is True


# === watcher batch isolation + age-based seen-set prune ===


class _FakeClearEvents:
    def __init__(self, events: list) -> None:
        self._events = events

    async def get_logs(self, *, from_block: int, to_block: int, **_: Any) -> list:
        return [e for e in self._events if from_block <= int(e["blockNumber"]) <= to_block]


class _FakeReaderContract:
    def __init__(self, events: list) -> None:
        self.events = SimpleNamespace(CustodyTxCleared=_FakeClearEvents(events))


class _FakeReaderEth:
    def __init__(self, head: int) -> None:
        self._head = head

    @property
    def block_number(self):
        async def _coro() -> int:
            return self._head

        return _coro()


class _FakeReaderWeb3:
    def __init__(self, head: int) -> None:
        self.eth = _FakeReaderEth(head)


@pytest.mark.asyncio
async def test_scan_isolates_poison_event_and_holds_cursor(state_dir, accounting, caplog):
    """One event whose processing raises does not abort the batch: a healthy
    sibling is still applied and the cursor is held back past the poison block."""
    head = 1000
    poison = {
        "transactionHash": HexBytes("0x" + "01" * 32),
        "logIndex": 0,
        "blockNumber": 980,
        # An out-of-range action int raises inside _process_event before any
        # try/except, exercising the _scan_once per-event isolation.
        "args": {
            "chainId": SAPPHIRE,
            "nonce": 50,
            "action": 99,
            "vouchedTxHash": HexBytes(b"\x00" * 32),
        },
    }
    vouched = b"\x00" * 32
    healthy = _cleared_event(
        chain_id=SAPPHIRE,
        nonce=51,
        action=ClearAction.REQUEUE,
        vouched=vouched,
        tx_hash="0x" + "02" * 32,
        log_index=0,
    )
    healthy["blockNumber"] = 985

    watcher, executor = _make_watcher(accounting, state_dir)
    executor._apply_clear_action.return_value = "applied"
    accounting.reader_w3 = _FakeReaderWeb3(head)
    accounting._get_reader_contract = MagicMock(return_value=_FakeReaderContract([poison, healthy]))
    accounting.get_clear_applied_hash = AsyncMock(
        return_value=_expected_applied_hash(int(ClearAction.REQUEUE), vouched)
    )

    with caplog.at_level("INFO"):
        await watcher._scan_once()

    # Healthy sibling applied despite the poison event raising.
    executor._apply_clear_action.assert_awaited_once_with(
        SAPPHIRE, 51, ClearAction.REQUEUE, HexBytes(vouched).to_0x_hex()
    )
    # Cursor held back past the unprocessed poison block (980 - 1).
    assert watcher._cursor == poison["blockNumber"] - 1


def test_prune_seen_drops_by_block_age(state_dir, accounting):
    watcher, _executor = _make_watcher(accounting, state_dir)
    watcher._cursor = 1000
    cutoff = watcher._cursor - watcher._overlap_blocks
    old_key = ("0x" + "aa" * 32, 0)
    recent_key = ("0x" + "bb" * 32, 1)
    watcher._seen = {old_key: cutoff - 1, recent_key: cutoff}

    watcher._prune_seen()

    assert old_key not in watcher._seen
    assert recent_key in watcher._seen


def test_prune_seen_retains_entry_within_held_back_cursor_window(state_dir, accounting):
    """Cursor held back below safe_head: an entry whose block sits inside the
    next pass's rescan floor (cursor - overlap) must survive pruning, or it would
    be re-discovered and re-applied. Anchoring on safe_head would wrongly drop it."""
    watcher, _executor = _make_watcher(accounting, state_dir)
    overlap = watcher._overlap_blocks
    safe_head = 1000
    watcher._cursor = safe_head - 200  # held back well below the confirmed head
    rescan_floor = watcher._cursor - overlap
    # Block sits above the cursor's rescan floor but below safe_head - overlap.
    in_window_key = ("0x" + "cc" * 32, 0)
    watcher._seen = {in_window_key: rescan_floor}

    watcher._prune_seen()

    assert in_window_key in watcher._seen
