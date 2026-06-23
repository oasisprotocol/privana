"""Preflight + surplus tests for the BridgeAsset withdrawal path.

Covers two surfaces:
1. `CustodyTxExecutor` kind-routed preflight for `BASE_MINT` (eth_call to
   ROFLBridge.mint from custody EOA) and `SAPPHIRE_RELEASE` (fresh sign via
   `resolve_bridge_withdrawal(index)` — contract reads gas from
   `gasPrices[Sapphire]` and enforces the user's `maxGasCost` cap), plus the
   receipt-side surplus accumulator.
2. `WithdrawalProcessor` routing the bridge-record signing call through
   `resolve_bridge_withdrawal(...)` instead of the legacy
   `resolveWithdrawal(...)` that reverts `UnsupportedTokenType` for
   `TokenType.BridgeAsset` on-chain.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from eth_abi import encode
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError

from src.services.custody_tx_executor import (
    CustodyTxExecutor,
    CustodyTxKind,
    CustodyTxRecord,
    CustodyTxRequest,
    CustodyTxStatus,
)
from src.services.withdrawal_processor import WithdrawalProcessor

SAPPHIRE = 23295
BASE = 84532
CUSTODY_ADDRESS = Web3.to_checksum_address("0x" + "ab" * 20)
CONTRACT_ADDRESS = Web3.to_checksum_address("0x" + "cd" * 20)
ROUTE_ADDRESS = Web3.to_checksum_address("0x" + "12" * 20)
TO_ADDRESS = Web3.to_checksum_address("0x" + "34" * 20)
AMOUNT = 10**18
MAX_GAS_COST = 10**15  # 0.001 ROSE
WITHDRAWAL_INDEX = 7

# Revert errors used by the Base preflight tests. The executor's classifier
# matches on the 4-byte selector from `ContractLogicError.data`, so the test
# fixtures supply full ``(message, data)`` pairs as a real RPC node would.
# keccak256("EnforcedPause()")[:4] = 0xd93c0665
# keccak256("IXERC20_NotHighEnoughLimits()")[:4] = 0x0b6842aa
ROFL_PAUSED = ContractLogicError("execution reverted: EnforcedPause()", data="0xd93c0665")
MINT_LIMIT_EXCEEDED = ContractLogicError(
    "execution reverted: IXERC20_NotHighEnoughLimits()", data="0x0b6842aa"
)
# Unrecognized selector → AWAITING_CLEAR path.
UNEXPECTED_REVERT = ContractLogicError("execution reverted: SomethingElse()", data="0xdeadbeef")
# keccak256("AlreadyProcessed()")[:4] = 0x57eee766
ALREADY_PROCESSED = ContractLogicError("execution reverted: AlreadyProcessed()", data="0x57eee766")


class _AwaitableValue:
    """Re-awaitable wrapper for async web3 properties (gas_price)."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        if False:
            yield
        return self._value


class _FakeMintCall:
    # ``eth`` is held by reference so the test can flip ``_mint_raises`` /
    # ``_burn_raises`` between iterations and the next ``.call()`` picks the
    # new value up — the executor caches the bound contract instance, so a
    # raises field captured at construction-time would freeze the test state.
    def __init__(self, recorder, eth, method, args, kwargs):
        self._recorder = recorder
        self._eth = eth
        self._method = method
        self._args = args
        self._kwargs = kwargs

    async def call(self, tx_params=None):
        self._recorder["last_method"] = self._method
        self._recorder["last_args"] = self._args
        self._recorder["last_kwargs"] = self._kwargs
        self._recorder["last_tx_params"] = tx_params
        raises = self._eth._mint_raises if self._method == "mint" else self._eth._burn_raises
        if raises is not None:
            raise raises
        return None


class _FakeReadCall:
    """Async .call() wrapper for view-function mocks (mintedWithdrawalIds etc)."""

    def __init__(self, value):
        self._value = value

    async def call(self, tx_params=None):
        return self._value


class _FakeEventType:
    """Mocks `contract.events.<Name>` so duplicate-id recovery can read past logs.

    `get_logs` is async and applies `argument_filters` against each
    pre-scripted entry's `args` dict — same shape as real web3.py 7.x async.
    """

    def __init__(self, entries):
        self._entries = entries

    async def get_logs(self, *, from_block=None, to_block=None, argument_filters=None):
        filters = argument_filters or {}
        return [e for e in self._entries if all(e["args"].get(k) == v for k, v in filters.items())]


class _FakeEvents:
    """Lazy property accessors so the test can mutate `_minted_events` /
    `_burned_events` between iterations and the next `get_logs` sees
    the new state — same reason `_FakeMintCall` reads `_mint_raises` from
    `eth` at call-time instead of capturing it at construction-time."""

    def __init__(self, eth):
        self._eth = eth

    @property
    def Minted(self):
        return _FakeEventType(self._eth._minted_events)

    @property
    def Burned(self):
        return _FakeEventType(self._eth._burned_events)


class _FakeFunctions:
    def __init__(self, recorder, eth):
        self._recorder = recorder
        self._eth = eth

    def mint(self, *args, **kwargs):
        return _FakeMintCall(self._recorder, self._eth, "mint", args, kwargs)

    def burn(self, *args, **kwargs):
        return _FakeMintCall(self._recorder, self._eth, "burn", args, kwargs)

    def mintedWithdrawalIds(self, withdrawal_id):
        key = bytes(withdrawal_id).hex()
        return _FakeReadCall(self._eth._minted_withdrawal_ids.get(key, False))

    def burnedDepositIds(self, deposit_id):
        key = bytes(deposit_id).hex()
        return _FakeReadCall(self._eth._burned_deposit_ids.get(key, False))


class _FakeContract:
    def __init__(self, recorder, eth, address):
        recorder["last_contract_address"] = address
        self.functions = _FakeFunctions(recorder, eth)
        self.events = _FakeEvents(eth)


class _FakeChainEth:
    def __init__(self) -> None:
        self.send_raw_transaction = AsyncMock()
        self.get_transaction_receipt = AsyncMock()
        # Pre-broadcast nonce floor probe (`_process_next_for_chain`). Default 0
        # blocks every enqueued record at nonce 100+ as a future tx; `_enqueue_*`
        # helpers raise it to the broadcast nonce so the runnable record is
        # treated as ready-to-mine. Tests that drive a record to BLOCKED for an
        # unrelated reason override this with a realistic-but-not-future value.
        self.get_transaction_count = AsyncMock(return_value=0)
        self.get_balance = AsyncMock(return_value=10**20)
        # Test-scripted state read by preflight via eth.contract(...).functions.mint
        self._mint_raises = None
        self._burn_raises = None
        self._gas_price_wei = 20_000_000_000  # 20 gwei
        self._minted_withdrawal_ids: Dict[str, bool] = {}
        self._burned_deposit_ids: Dict[str, bool] = {}
        self._minted_events: list = []
        self._burned_events: list = []
        # tx hash → {"from": ..., "nonce": ...}. Recovery's signer+nonce
        # cross-check resolves the matched event's transactionHash through this.
        self._transactions: Dict[str, Dict[str, Any]] = {}
        # Populated by _FakeMintCall.call so tests can assert eth_call params.
        self.preflight_recorder: Dict[str, Any] = {}

    async def get_transaction(self, tx_hash):
        key = HexBytes(tx_hash).to_0x_hex().lower()
        return self._transactions.get(key)

    @property
    def gas_price(self):
        return _AwaitableValue(self._gas_price_wei)

    def contract(self, address=None, abi=None):
        return _FakeContract(self.preflight_recorder, self, address)


class _FakeChainWeb3:
    def __init__(self) -> None:
        self.eth = _FakeChainEth()


def _make_receipt(
    *,
    status: int = 1,
    block: int = 100,
    gas_used: int = 21000,
    effective_gas_price: int = 20_000_000_000,
) -> Dict[str, Any]:
    return {
        "status": status,
        "blockNumber": block,
        "transactionHash": HexBytes("0x" + "ee" * 32),
        "gasUsed": gas_used,
        "effectiveGasPrice": effective_gas_price,
    }


@pytest.fixture
def state_dir(tmp_path) -> str:
    return str(tmp_path)


@pytest.fixture
def web3s() -> Dict[int, _FakeChainWeb3]:
    return {SAPPHIRE: _FakeChainWeb3(), BASE: _FakeChainWeb3()}


@pytest.fixture
def accounting(web3s):
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
    # BASE_MINT/XROSE_BURN re-sign per attempt off these calls; the fresh bytes
    # are what actually get broadcast.
    svc.generate_bridge_burn_transfer = AsyncMock(return_value=b"\xbe" * 100)
    return svc


@pytest.fixture
def executor(state_dir: str, accounting) -> CustodyTxExecutor:
    return CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )


async def _seed_onchain_nonce(executor: CustodyTxExecutor, chain_id: int, nonce: int) -> None:
    """Raise the fake chain's `get_transaction_count` to at least `nonce`.

    The pre-broadcast floor blocks a runnable record when its `evm_nonce`
    exceeds on-chain `latest` count. The realistic precondition for the
    next-to-mine record at nonce N is that `latest` count == N. Keyed per
    enqueue and monotonic so multi-record tests land at `max(nonces)`,
    mirroring `_script_broadcast_success` in test_custody_tx_executor.py.
    """
    eth = (await executor._accounting._get_chain_web3(chain_id)).eth
    current = eth.get_transaction_count.return_value
    eth.get_transaction_count = AsyncMock(return_value=max(current, nonce))


async def _enqueue_base_mint(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    withdrawal_index: int = WITHDRAWAL_INDEX,
    signed_tx: bytes | None = None,
) -> str:
    await _seed_onchain_nonce(executor, BASE, nonce)
    if signed_tx is None:
        signed_tx = bytes.fromhex("de" * 100)
    return await executor.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=nonce,
            kind=CustodyTxKind.BASE_MINT,
            id=str(withdrawal_index),
            signed_tx=signed_tx,
            route_address=ROUTE_ADDRESS,
            max_gas_cost=0,
            withdrawal_index=withdrawal_index,
            to_address=TO_ADDRESS,
            amount=AMOUNT,
        )
    )


async def _enqueue_sapphire_release(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    withdrawal_index: int = WITHDRAWAL_INDEX,
    max_gas_cost: int = MAX_GAS_COST,
) -> str:
    await _seed_onchain_nonce(executor, SAPPHIRE, nonce)
    return await executor.enqueue(
        CustodyTxRequest(
            chain_id=SAPPHIRE,
            evm_nonce=nonce,
            kind=CustodyTxKind.SAPPHIRE_RELEASE,
            id=str(withdrawal_index),
            signed_tx=b"",  # executor regenerates per attempt
            route_address=None,
            max_gas_cost=max_gas_cost,
            withdrawal_index=withdrawal_index,
            to_address=TO_ADDRESS,
            amount=AMOUNT,
        )
    )


async def _enqueue_xrose_burn(
    executor: CustodyTxExecutor,
    *,
    nonce: int,
    deposit_id_hex: str = "0x" + "dd" * 32,
    amount: int = AMOUNT,
    signed_tx: bytes | None = None,
) -> str:
    await _seed_onchain_nonce(executor, BASE, nonce)
    if signed_tx is None:
        signed_tx = bytes.fromhex("cd" * 100)
    return await executor.enqueue(
        CustodyTxRequest(
            chain_id=BASE,
            evm_nonce=nonce,
            kind=CustodyTxKind.XROSE_BURN,
            id=deposit_id_hex,
            signed_tx=signed_tx,
            route_address=ROUTE_ADDRESS,
            amount=amount,
        )
    )


def _script_receipt(web3: _FakeChainWeb3, receipt: Dict[str, Any]) -> None:
    tx_hash = HexBytes("0x" + "ee" * 32)
    web3.eth.send_raw_transaction.side_effect = [tx_hash]

    async def _r(_h):
        return receipt

    web3.eth.get_transaction_receipt.side_effect = _r


def _script_recovered_tx(
    web3: _FakeChainWeb3,
    *,
    tx_hash: HexBytes,
    from_address: str = CUSTODY_ADDRESS,
    nonce: int = 0,
) -> None:
    web3.eth._transactions[tx_hash.to_0x_hex().lower()] = {
        "from": from_address,
        "nonce": nonce,
    }


# --- 1-4: Base mint preflight ---


@pytest.mark.asyncio
async def test_base_mint_preflight_paused_stays_queued_for_retry(executor, web3s):
    """ROFLBridge paused → record stays QUEUED with descriptive error.
    Subsequent loop iteration with pause cleared → broadcasts; error cleared."""
    web3s[BASE].eth._mint_raises = ROFL_PAUSED
    await _enqueue_base_mint(executor, nonce=100)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 100)
    assert rec.status == CustodyTxStatus.QUEUED
    assert "paused" in (rec.error or "").lower()
    assert web3s[BASE].eth.send_raw_transaction.call_count == 0

    # Next iteration with pause cleared → broadcast
    web3s[BASE].eth._mint_raises = None
    _script_receipt(web3s[BASE], _make_receipt(status=1))
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 100)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.error is None
    assert web3s[BASE].eth.send_raw_transaction.call_count == 1


@pytest.mark.asyncio
async def test_base_mint_preflight_mint_limit_exhausted_stays_queued_for_retry(executor, web3s):
    """XRose mint-limit exhausted → record stays QUEUED with descriptive error."""
    web3s[BASE].eth._mint_raises = MINT_LIMIT_EXCEEDED
    await _enqueue_base_mint(executor, nonce=101)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 101)
    assert rec.status == CustodyTxStatus.QUEUED
    assert "limit" in (rec.error or "").lower()
    assert web3s[BASE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_base_mint_preflight_passes_and_broadcasts(executor, web3s, accounting):
    """Clean eth_call → re-sign via resolve_bridge_withdrawal, then broadcast
    the fresh bytes (NOT the gas-frozen sign-time tx). The re-sign must not
    change the record's EVM nonce — only gas — since the destination-tx nonce
    is frozen on-chain in the stored txIdentifier."""
    fresh_signed = b"\xab" * 100  # accounting fixture's resolve_bridge_withdrawal return
    _script_receipt(web3s[BASE], _make_receipt(status=1))
    await _enqueue_base_mint(executor, nonce=102, withdrawal_index=42)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 102)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert web3s[BASE].eth.send_raw_transaction.call_count == 1
    # Re-signed off the withdrawal index, and the fresh bytes were broadcast.
    accounting.resolve_bridge_withdrawal.assert_awaited_once_with(42)
    assert web3s[BASE].eth.send_raw_transaction.call_args[0][0] == fresh_signed
    # Nonce is untouched by the re-sign.
    assert rec.evm_nonce == 102


@pytest.mark.asyncio
async def test_base_mint_preflight_unexpected_revert_marks_awaiting_clear(executor, web3s):
    """Non-pause / non-limit revert → AWAITING_CLEAR (terminal). Operator
    investigates; the chain loop halts at this nonce."""
    web3s[BASE].eth._mint_raises = UNEXPECTED_REVERT
    await _enqueue_base_mint(executor, nonce=103)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 103)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert web3s[BASE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_base_mint_preflight_calls_with_custody_from_address(executor, web3s):
    """eth_call must run with from=custody_eoa against record.route_address
    with (to, amount, withdrawal_id) — paused/limit checks differ per caller
    and per recipient, so a missing `from` or wrong address would silently
    misclassify."""
    _script_receipt(web3s[BASE], _make_receipt(status=1))
    await _enqueue_base_mint(executor, nonce=110)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 110)
    assert rec.status == CustodyTxStatus.SUCCESS

    recorder = web3s[BASE].eth.preflight_recorder
    assert recorder["last_method"] == "mint"
    assert recorder["last_contract_address"] == ROUTE_ADDRESS
    assert recorder["last_tx_params"] == {"from": CUSTODY_ADDRESS}
    mint_args = recorder["last_args"]
    assert mint_args[0] == TO_ADDRESS
    assert mint_args[1] == AMOUNT
    # withdrawal_id = keccak256(abi.encode(accountingProxy, sapphireChainId, idx))
    expected_withdrawal_id = Web3.keccak(
        encode(
            ["address", "uint256", "uint256"],
            [CONTRACT_ADDRESS, SAPPHIRE, WITHDRAWAL_INDEX],
        )
    )
    assert bytes(mint_args[2]) == bytes(expected_withdrawal_id)


@pytest.mark.asyncio
async def test_xrose_burn_preflight_calls_with_custody_from_address(executor, web3s):
    """xROSE burn preflight mirrors the Base mint guarantees:
    eth_call uses from=custody_eoa, route_address as the contract, and
    (amount, depositId-bytes32) as the args."""
    _script_receipt(web3s[BASE], _make_receipt(status=1))
    deposit_id_hex = "0x" + "ee" * 32
    await _enqueue_xrose_burn(executor, nonce=120, deposit_id_hex=deposit_id_hex)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 120)
    assert rec.status == CustodyTxStatus.SUCCESS

    recorder = web3s[BASE].eth.preflight_recorder
    assert recorder["last_method"] == "burn"
    assert recorder["last_contract_address"] == ROUTE_ADDRESS
    assert recorder["last_tx_params"] == {"from": CUSTODY_ADDRESS}
    burn_args = recorder["last_args"]
    assert burn_args[0] == AMOUNT
    assert bytes(burn_args[1]) == bytes(HexBytes(deposit_id_hex))


# --- 5-7: Sapphire release preflight ---


@pytest.mark.asyncio
async def test_sapphire_release_preflight_contract_revert_marks_awaiting_clear(
    executor, web3s, accounting
):
    """A generic (non-GasBudgetExceeded) ContractLogicError from
    resolve_bridge_withdrawal → plain AWAITING_CLEAR; no broadcast; record does
    not auto-retry. (GasBudgetExceeded routes to AWAITING_CLEAR_GAS_CAP — see
    test_custody_tx_recovery.py.)"""
    accounting.resolve_bridge_withdrawal = AsyncMock(side_effect=UNEXPECTED_REVERT)

    await _enqueue_sapphire_release(executor, nonce=800)
    await executor._process_next_for_chain(SAPPHIRE)

    rec = executor.get_record(SAPPHIRE, 800)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "reverted" in (rec.error or "").lower()
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 0
    assert accounting.resolve_bridge_withdrawal.call_count == 1


@pytest.mark.asyncio
async def test_sapphire_release_preflight_rpc_failure_retries_later(executor, web3s, accounting):
    """A transient transport error from resolve_bridge_withdrawal (RPC outage) →
    RETRY_LATER with target_status=QUEUED; record stays runnable next loop."""
    accounting.resolve_bridge_withdrawal = AsyncMock(
        side_effect=aiohttp.ServerDisconnectedError("provider dropped the connection")
    )

    await _enqueue_sapphire_release(executor, nonce=810)
    await executor._process_next_for_chain(SAPPHIRE)

    rec = executor.get_record(SAPPHIRE, 810)
    assert rec.status == CustodyTxStatus.QUEUED
    assert "rpc failure" in (rec.error or "").lower()
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_sapphire_release_awaiting_clear_blocks_downstream_nonces(
    executor, web3s, accounting
):
    """Record N stuck AWAITING_CLEAR → record N+1 stays QUEUED, never broadcasts.
    The blocking status preserves the 'block later Sapphire nonces' guarantee
    because the chain loop returns at the stuck record without advancing."""
    accounting.resolve_bridge_withdrawal = AsyncMock(
        side_effect=ContractLogicError("execution reverted: GasBudgetExceeded()", data="0x12345678")
    )

    await _enqueue_sapphire_release(executor, nonce=900, withdrawal_index=900)
    await _enqueue_sapphire_release(executor, nonce=901, withdrawal_index=901)

    for _ in range(3):
        await executor._process_next_for_chain(SAPPHIRE)

    rec_900 = executor.get_record(SAPPHIRE, 900)
    rec_901 = executor.get_record(SAPPHIRE, 901)
    assert rec_900.status == CustodyTxStatus.AWAITING_CLEAR
    assert rec_901.status == CustodyTxStatus.QUEUED
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_sapphire_release_preflight_passes_and_fresh_signs(executor, web3s, accounting):
    """resolve_bridge_withdrawal called with the withdrawal index only;
    broadcast uses the freshly-signed bytes, NOT the b"" placeholder."""
    fresh_signed = b"\xab\xcd" * 50
    accounting.resolve_bridge_withdrawal = AsyncMock(return_value=fresh_signed)

    _script_receipt(
        web3s[SAPPHIRE],
        _make_receipt(status=1, gas_used=21000, effective_gas_price=20_000_000_000),
    )
    await _enqueue_sapphire_release(executor, nonce=950, withdrawal_index=42)
    await executor._process_next_for_chain(SAPPHIRE)

    # Fresh bytes were broadcast
    assert web3s[SAPPHIRE].eth.send_raw_transaction.call_count == 1
    broadcast_args = web3s[SAPPHIRE].eth.send_raw_transaction.call_args
    assert broadcast_args[0][0] == fresh_signed

    # resolve_bridge_withdrawal called with index 42 only — the contract reads
    # gas from gasPrices[Sapphire] on-chain.
    accounting.resolve_bridge_withdrawal.assert_called_once_with(42)


# --- 8-10: Surplus accounting ---


@pytest.mark.asyncio
async def test_sapphire_release_success_persists_surplus_delta(executor, web3s, accounting):
    """SAPPHIRE_RELEASE SUCCESS → surplus_delta = max_gas_cost - gas_used*effective_gas_price."""
    web3s[SAPPHIRE].eth._gas_price_wei = 20_000_000_000

    # max_gas_cost=1e15, gas_used=21000, effective_gas_price=40e9 → actual=8.4e14 → delta=1.6e14
    _script_receipt(
        web3s[SAPPHIRE],
        _make_receipt(status=1, gas_used=21000, effective_gas_price=40_000_000_000),
    )
    await _enqueue_sapphire_release(executor, nonce=1000, max_gas_cost=10**15)
    await executor._process_next_for_chain(SAPPHIRE)

    rec = executor.get_record(SAPPHIRE, 1000)
    assert rec.status == CustodyTxStatus.SUCCESS
    expected_actual = 21000 * 40_000_000_000
    assert rec.gas_used == 21000
    assert rec.effective_gas_price == 40_000_000_000
    assert rec.surplus_delta == (10**15 - expected_actual)


@pytest.mark.asyncio
async def test_sapphire_release_actual_gas_over_cap_marks_awaiting_clear_gas_cap(
    executor, web3s, accounting
):
    """Receipt actual cost > max_gas_cost → AWAITING_CLEAR_GAS_CAP (terminal).

    BridgeLib's request-time check forbids this; if observed in a receipt it
    means a contract invariant has broken — fail closed."""
    web3s[SAPPHIRE].eth._gas_price_wei = 20_000_000_000

    # max_gas_cost=1e15. gas_used=21000 * effective_gas_price=60e9 = 1.26e15 > 1e15
    _script_receipt(
        web3s[SAPPHIRE],
        _make_receipt(status=1, gas_used=21000, effective_gas_price=60_000_000_000),
    )
    await _enqueue_sapphire_release(executor, nonce=1100, max_gas_cost=10**15)
    await executor._process_next_for_chain(SAPPHIRE)

    rec = executor.get_record(SAPPHIRE, 1100)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR_GAS_CAP
    assert rec.surplus_delta is None


@pytest.mark.asyncio
async def test_surplus_reconstruction_after_restart(state_dir, accounting):
    """Write 3 SAPPHIRE_RELEASE SUCCESS records with surplus_deltas to disk.
    Build a fresh executor pointing at the same state_dir → sapphire_release_surplus()
    sums to the pre-restart total without re-reading the chain."""
    deltas = [100, 200, 50]
    for i, delta in enumerate(deltas):
        rec = CustodyTxRecord(
            chain_id=SAPPHIRE,
            accounting_contract_address=CONTRACT_ADDRESS,
            evm_sender=CUSTODY_ADDRESS,
            evm_nonce=2000 + i,
            kind=CustodyTxKind.SAPPHIRE_RELEASE,
            id=str(i),
            signed_tx_hex="0x" + "ab" * 50,
            status=CustodyTxStatus.SUCCESS,
            max_gas_cost=10**15,
            withdrawal_index=i,
            to_address=TO_ADDRESS,
            amount=10**18,
            gas_used=21000,
            effective_gas_price=20_000_000_000,
            surplus_delta=delta,
        )
        path = Path(state_dir) / f"custody_tx_{SAPPHIRE}_{2000 + i}.json"
        path.write_text(json.dumps(rec.to_dict(), indent=2))

    fresh = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    assert fresh.sapphire_release_surplus() == sum(deltas)


# --- Canonical failure-mode drills (pytest -k drill) ---


@pytest.mark.asyncio
async def test_drill_paused_bridge_preflight_blocks_broadcast(executor, web3s):
    """Drill: ROFLBridge paused → preflight rejects, no raw tx broadcast."""
    web3s[BASE].eth._mint_raises = ROFL_PAUSED
    await _enqueue_base_mint(executor, nonce=200)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 200)
    assert rec.status == CustodyTxStatus.QUEUED
    assert rec.tx_hash is None
    assert rec.broadcast_hashes == []
    assert web3s[BASE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_drill_mint_limit_exhausted_preflight_blocks_broadcast(executor, web3s):
    """Drill: mintingCurrentLimitOf(ROFLBridge) == 0 → preflight rejects,
    no broadcast, and no record promoted to SUCCESS on this chain. The
    record's status IS the dedup cursor, so the SUCCESS-absence check is
    load-bearing alongside the broadcast-count assertion."""
    web3s[BASE].eth._mint_raises = MINT_LIMIT_EXCEEDED
    await _enqueue_base_mint(executor, nonce=201)
    await _enqueue_base_mint(executor, nonce=202)
    await executor._process_next_for_chain(BASE)
    await executor._process_next_for_chain(BASE)

    assert web3s[BASE].eth.send_raw_transaction.call_count == 0
    records = executor.get_records_for_chain(BASE)
    assert {r.evm_nonce for r in records} == {201, 202}
    assert all(r.status == CustodyTxStatus.QUEUED for r in records)
    assert all(r.tx_hash is None for r in records)


@pytest.mark.asyncio
async def test_drill_successful_mint_promotes_record_to_success_exactly_once(executor, web3s):
    """Drill: receipt status==1 → record promoted to SUCCESS exactly once.
    A second poll is a no-op (terminal-status records are not re-broadcast),
    pinning the exactly-once invariant across the dedup boundary."""
    _script_receipt(web3s[BASE], _make_receipt(status=1, block=500))
    await _enqueue_base_mint(executor, nonce=202)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 202)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.receipt_status == 1
    assert rec.tx_hash == HexBytes("0x" + "ee" * 32).to_0x_hex()
    assert rec.recovered_tx_hash is None
    assert rec.recovered_block_number is None
    records = executor.get_records_for_chain(BASE)
    assert len(records) == 1
    assert records[0].evm_nonce == 202

    await executor._process_next_for_chain(BASE)
    assert web3s[BASE].eth.send_raw_transaction.call_count == 1
    assert executor.get_record(BASE, 202).status == CustodyTxStatus.SUCCESS


# --- 11-12: Withdrawal processor routes bridge records to resolve_bridge_withdrawal ---


@pytest.fixture
def mock_accounting_service():
    """Minimal AccountingContractService mock for withdrawal-processor tests."""
    service = MagicMock()
    service.get_all_pending_withdrawals = AsyncMock(
        return_value={"pending": [], "current_block": 100}
    )
    service.resolve_withdrawal = AsyncMock(return_value=MagicMock(status="submitted"))
    service.submit_resolve_bridge_withdrawal = AsyncMock(return_value=MagicMock(status="submitted"))
    service.resolve_bridge_withdrawal = AsyncMock(return_value=b"\x77" * 64)
    mock_contract = MagicMock()
    mock_contract.functions.withdrawals.return_value.call = AsyncMock()
    mock_contract.functions.resolveWithdrawal.return_value.call = AsyncMock(
        return_value=b"\x00" * 64
    )
    mock_contract.functions.withdrawalCount.return_value.call = AsyncMock()
    service._get_reader_contract = MagicMock(return_value=mock_contract)
    service._get_token_context = AsyncMock()
    service._mock_contract = mock_contract  # expose for assertions
    return service


@pytest.fixture
def mock_custody_executor():
    ex = MagicMock()
    ex.get_record = MagicMock(return_value=None)
    ex.enqueue = AsyncMock(return_value="dummy-key")
    return ex


@pytest.fixture
def processor(mock_accounting_service, mock_custody_executor):
    with patch("src.services.withdrawal_processor.load_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            withdrawal_poll_interval=1,
            withdrawal_resolution_timeout=1,
            sapphire_rpc_url="https://example.invalid",
            accounting_contract_address=CONTRACT_ADDRESS,
            chain_rpc_urls={SAPPHIRE: "https://s.invalid", BASE: "https://b.invalid"},
            sapphire_chain_id=SAPPHIRE,
        )
        with patch(
            "src.services.withdrawal_processor.AccountingContractService",
            return_value=mock_accounting_service,
        ):
            with patch("src.services.withdrawal_processor.AsyncWeb3") as mock_async_web3:
                mock_w3 = MagicMock()
                mc = MagicMock()
                mc.functions.evmAddress.return_value.call = AsyncMock(return_value=CUSTODY_ADDRESS)
                mc.functions.nonces.return_value.call = AsyncMock(return_value=0)
                mock_w3.eth.contract.return_value = mc
                mock_async_web3.return_value = mock_w3

                proc = WithdrawalProcessor(custody_executor=mock_custody_executor)
                proc.accounting_service = mock_accounting_service
                proc._contract = mc
                return proc


@pytest.mark.asyncio
async def test_withdrawal_processor_routes_base_to_resolve_bridge_withdrawal(
    processor, mock_accounting_service, mock_custody_executor
):
    """Base bridge withdrawal in live path → calls resolve_bridge_withdrawal(idx),
    not resolveWithdrawal(idx). Guards the latent UnsupportedTokenType bug."""
    tx_identifier = encode(
        ["uint256", "uint64", "address", "uint256"],
        [BASE, 5, ROUTE_ADDRESS, 0],
    )
    mock_accounting_service._mock_contract.functions.withdrawals.return_value.call.return_value = (
        Web3.to_checksum_address("0x" + "11" * 20),  # user
        TO_ADDRESS,
        AMOUNT,
        100,
        b"\x00" * 32,  # tokenId
        True,  # resolved
        tx_identifier,
    )
    withdrawal = {
        "index": 0,
        "chain_id": BASE,
        "is_bridge_asset": True,
        "dest_tx_nonce": 5,
        "route_address": ROUTE_ADDRESS,
        "max_gas_cost": 0,
    }

    ok = await processor._resolve_and_broadcast(withdrawal)
    assert ok is True

    mock_accounting_service.resolve_bridge_withdrawal.assert_called_once_with(0)
    mock_accounting_service._mock_contract.functions.resolveWithdrawal.return_value.call.assert_not_called()

    mock_custody_executor.enqueue.assert_called_once()
    req = mock_custody_executor.enqueue.call_args[0][0]
    assert req.chain_id == BASE
    assert req.evm_nonce == 5
    assert req.kind == CustodyTxKind.BASE_MINT


@pytest.mark.asyncio
async def test_withdrawal_processor_resolves_unresolved_bridge_asset_via_bridge_path(
    processor, mock_accounting_service, mock_custody_executor
):
    """Unresolved BridgeAsset withdrawal must call submit_resolve_bridge_withdrawal
    to flip ``resolved=true`` — ``resolve_withdrawal`` reverts ``UnsupportedTokenType``
    on-chain for BridgeAsset tokens, so the processor must never reach for it."""
    tx_identifier = encode(
        ["uint256", "uint64", "address", "uint256"],
        [BASE, 7, ROUTE_ADDRESS, 0],
    )
    unresolved = (
        Web3.to_checksum_address("0x" + "33" * 20),
        TO_ADDRESS,
        AMOUNT,
        100,
        b"\x00" * 32,
        False,
        tx_identifier,
    )
    resolved = (*unresolved[:5], True, unresolved[6])
    # First read sees resolved=False (triggers the resolve write); the polling
    # loop after the write sees resolved=True; Step 3 reads it again.
    mock_accounting_service._mock_contract.functions.withdrawals.return_value.call.side_effect = [
        unresolved,
        resolved,
        resolved,
    ]
    withdrawal = {
        "index": 0,
        "chain_id": BASE,
        "is_bridge_asset": True,
        "dest_tx_nonce": 7,
        "route_address": ROUTE_ADDRESS,
        "max_gas_cost": 0,
    }

    ok = await processor._resolve_and_broadcast(withdrawal)
    assert ok is True

    mock_accounting_service.submit_resolve_bridge_withdrawal.assert_called_once_with(0)
    mock_accounting_service.resolve_withdrawal.assert_not_called()
    mock_accounting_service.resolve_bridge_withdrawal.assert_called_once_with(0)

    mock_custody_executor.enqueue.assert_called_once()
    req = mock_custody_executor.enqueue.call_args[0][0]
    assert req.chain_id == BASE
    assert req.evm_nonce == 7
    assert req.kind == CustodyTxKind.BASE_MINT


@pytest.mark.asyncio
async def test_withdrawal_processor_skips_signing_for_sapphire_bridge_enqueue(
    processor, mock_accounting_service, mock_custody_executor
):
    """Sapphire bridge withdrawal in live path → enqueue with signed_tx=b"" and
    withdrawal_index+max_gas_cost. Executor regenerates per attempt with live gas."""
    tx_identifier = encode(
        ["uint256", "uint64", "address", "uint256"],
        [SAPPHIRE, 3, "0x" + "00" * 20, MAX_GAS_COST],
    )
    mock_accounting_service._mock_contract.functions.withdrawals.return_value.call.return_value = (
        Web3.to_checksum_address("0x" + "22" * 20),
        TO_ADDRESS,
        AMOUNT,
        100,
        b"\x00" * 32,
        True,
        tx_identifier,
    )
    withdrawal = {
        "index": 0,
        "chain_id": SAPPHIRE,
        "is_bridge_asset": True,
        "dest_tx_nonce": 3,
        "route_address": "0x" + "00" * 20,
        "max_gas_cost": MAX_GAS_COST,
    }

    ok = await processor._resolve_and_broadcast(withdrawal)
    assert ok is True

    # Sapphire path does NOT pre-sign; executor regenerates per attempt.
    mock_accounting_service.resolve_bridge_withdrawal.assert_not_called()
    mock_accounting_service._mock_contract.functions.resolveWithdrawal.return_value.call.assert_not_called()

    mock_custody_executor.enqueue.assert_called_once()
    req = mock_custody_executor.enqueue.call_args[0][0]
    assert req.chain_id == SAPPHIRE
    assert req.evm_nonce == 3
    assert req.kind == CustodyTxKind.SAPPHIRE_RELEASE
    assert req.signed_tx == b""
    assert req.withdrawal_index == 0
    assert req.max_gas_cost == MAX_GAS_COST


# --- Regression: executor catch-up must route bridge records through
#     `resolve_bridge_withdrawal`, not the dispatcher's `resolveWithdrawal`
#     (which reverts UnsupportedTokenType for TokenType.BridgeAsset). And the
#     reconstructed record must carry the preflight inputs so the next loop
#     iteration's dispatcher has the gates it needs.


def _make_catchup_contract(withdrawals: list[tuple[int, tuple]]):
    """Build a minimal contract mock for `_catchup_withdrawal_queue`.

    `withdrawals` is a list of (index, withdrawals(idx) tuple) entries.
    `withdrawalCount()` returns max(index)+1; `resolveWithdrawal` raises so any
    accidental call surfaces as a regression rather than a silent fallthrough.
    """
    count = max((idx for idx, _ in withdrawals), default=-1) + 1
    entries = {idx: tup for idx, tup in withdrawals}

    contract = MagicMock()
    contract.functions.withdrawalCount.return_value.call = AsyncMock(return_value=count)

    def _withdrawals(idx):
        async def _call():
            return entries[idx]

        wrapped = MagicMock()
        wrapped.call = _call
        return wrapped

    contract.functions.withdrawals.side_effect = _withdrawals
    contract.functions.resolveWithdrawal.return_value.call = AsyncMock(
        side_effect=AssertionError("resolveWithdrawal must not be called for BridgeAsset")
    )
    return contract


@pytest.mark.asyncio
async def test_catchup_base_bridge_record_uses_resolve_bridge_withdrawal(executor, accounting):
    """Base bridge withdrawal at restart → catch-up enqueues via
    `resolve_bridge_withdrawal(idx)` and the record carries the full
    preflight-input set (route, max_gas_cost, withdrawal_index, to, amount).
    """
    idx = 3
    tx_identifier = encode(
        ["uint256", "uint64", "address", "uint256"],
        [BASE, 11, ROUTE_ADDRESS, 0],
    )
    user = Web3.to_checksum_address("0x" + "55" * 20)
    contract = _make_catchup_contract(
        [(idx, (user, TO_ADDRESS, AMOUNT, 100, b"\x00" * 32, True, tx_identifier))]
    )
    accounting.resolve_bridge_withdrawal = AsyncMock(return_value=b"\x42" * 64)

    inserted = await executor._catchup_withdrawal_queue(contract)

    assert inserted == 1
    accounting.resolve_bridge_withdrawal.assert_called_once_with(idx)

    rec = executor.get_record(BASE, 11)
    assert rec is not None
    assert rec.kind == CustodyTxKind.BASE_MINT
    assert rec.route_address == ROUTE_ADDRESS
    assert rec.max_gas_cost == 0
    assert rec.withdrawal_index == idx
    assert rec.to_address == TO_ADDRESS
    assert rec.amount == AMOUNT
    assert rec.signed_tx_hex == "0x" + "42" * 64


@pytest.mark.asyncio
async def test_catchup_base_bridge_record_failed_final_when_nonce_burned(
    executor, accounting, web3s
):
    """If the destination chain's custody nonce has already advanced past the
    withdrawal's destTxNonce, the prior broadcast outcome is unreconstructable
    from local state. Catch-up must stamp FAILED_FINAL (non-blocking) instead
    of re-signing + re-broadcasting, which would burn another nonce and
    eventually escalate to AWAITING_CLEAR (blocking)."""
    idx = 0
    burned_nonce = 0
    tx_identifier = encode(
        ["uint256", "uint64", "address", "uint256"],
        [BASE, burned_nonce, ROUTE_ADDRESS, 0],
    )
    user = Web3.to_checksum_address("0x" + "77" * 20)
    contract = _make_catchup_contract(
        [(idx, (user, TO_ADDRESS, AMOUNT, 100, b"\x00" * 32, True, tx_identifier))]
    )
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=burned_nonce + 1)
    accounting.resolve_bridge_withdrawal = AsyncMock(return_value=b"\x42" * 64)

    inserted = await executor._catchup_withdrawal_queue(contract)

    assert inserted == 1
    accounting.resolve_bridge_withdrawal.assert_not_called()

    rec = executor.get_record(BASE, burned_nonce)
    assert rec is not None
    assert rec.status == CustodyTxStatus.FAILED_FINAL
    assert rec.signed_tx_hex == "0x"
    assert rec.error is not None and "burned" in rec.error


@pytest.mark.asyncio
async def test_catchup_sapphire_bridge_record_skips_signing(executor, accounting):
    """Sapphire bridge withdrawal at restart → catch-up enqueues with empty
    `signed_tx_hex`; the preflight will fresh-sign with live gas on next
    broadcast. `resolve_bridge_withdrawal` is NOT pre-called.
    """
    idx = 4
    tx_identifier = encode(
        ["uint256", "uint64", "address", "uint256"],
        [SAPPHIRE, 7, "0x" + "00" * 20, MAX_GAS_COST],
    )
    user = Web3.to_checksum_address("0x" + "66" * 20)
    contract = _make_catchup_contract(
        [(idx, (user, TO_ADDRESS, AMOUNT, 100, b"\x00" * 32, True, tx_identifier))]
    )
    accounting.resolve_bridge_withdrawal = AsyncMock(return_value=b"\xab" * 100)

    inserted = await executor._catchup_withdrawal_queue(contract)

    assert inserted == 1
    accounting.resolve_bridge_withdrawal.assert_not_called()

    rec = executor.get_record(SAPPHIRE, 7)
    assert rec is not None
    assert rec.kind == CustodyTxKind.SAPPHIRE_RELEASE
    assert rec.max_gas_cost == MAX_GAS_COST
    assert rec.withdrawal_index == idx
    assert rec.to_address == TO_ADDRESS
    assert rec.amount == AMOUNT
    assert rec.signed_tx_hex == "0x"


# --- Regression: reconcile uses persisted `broadcast_hashes`, not a fresh
#     `keccak(signed_tx_hex)`. A SAPPHIRE_RELEASE that fresh-signs on retry
#     would overwrite signed_tx_hex; without history, reconcile would look up
#     the new hash and miss the original broadcast that actually mined.


@pytest.mark.asyncio
async def test_reconcile_finds_original_broadcast_hash_after_fresh_sign(executor, web3s):
    """Simulated crash-and-restart: persisted signed_tx_hex points at B2 (the
    re-sign), but broadcast_hashes contains H1 from the original attempt. The
    on-chain nonce advanced and the *original* H1 mined. Reconcile must find
    it via broadcast_hashes — derived `keccak(signed_tx_hex)` would miss.
    """
    from web3.exceptions import TransactionNotFound

    rec = CustodyTxRecord(
        chain_id=SAPPHIRE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=900,
        kind=CustodyTxKind.SAPPHIRE_RELEASE,
        id="0",
        signed_tx_hex="0x" + "b2" * 100,  # B2 — the re-sign on retry
        status=CustodyTxStatus.BROADCAST,
        broadcast_hashes=["0x" + "11" * 32, "0x" + "22" * 32],  # H1, H2
        max_gas_cost=MAX_GAS_COST,
        withdrawal_index=0,
        to_address=TO_ADDRESS,
        amount=AMOUNT,
    )
    executor._save_record(rec)

    sapphire_web3 = web3s[SAPPHIRE]
    sapphire_web3.eth.get_transaction_count = AsyncMock(return_value=901)  # nonce advanced

    h1 = "0x" + "11" * 32
    h2 = "0x" + "22" * 32

    async def _get_receipt(h):
        if h == h1:
            return _make_receipt(status=1, gas_used=21000, effective_gas_price=20_000_000_000)
        raise TransactionNotFound(f"not found: {h}")

    sapphire_web3.eth.get_transaction_receipt = AsyncMock(side_effect=_get_receipt)

    resolved = await executor._reconcile_by_sender_nonce(rec)

    assert resolved is True
    updated = executor.get_record(SAPPHIRE, 900)
    assert updated.status == CustodyTxStatus.SUCCESS
    assert updated.tx_hash == h1  # original broadcast, not keccak(B2)
    assert h2 not in (updated.tx_hash or "")


# --- Regression: `_extract_revert_selector` handles `data: dict` shapes from
#     providers (Anvil, Geth forks, gateway proxies). Without this the
#     EnforcedPause selector match fails and a paused ROFLBridge gets routed
#     to AWAITING_CLEAR instead of RETRY_LATER auto-retry.


@pytest.mark.asyncio
async def test_base_mint_preflight_handles_dict_shaped_revert_data(executor, web3s):
    """ContractLogicError with `data={"data": "0xd93c0665"}` (web3.py 7.x dict
    shape per `raise_contract_logic_error_on_revert`) classifies as paused —
    record stays QUEUED with the retry error, not AWAITING_CLEAR.
    """
    await _enqueue_base_mint(executor, nonce=42)

    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ContractLogicError(
        "execution reverted: EnforcedPause()",
        data={"data": "0xd93c0665"},
    )

    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 42)
    assert rec.status == CustodyTxStatus.QUEUED
    assert rec.error == "ROFLBridge paused"
    base_web3.eth.send_raw_transaction.assert_not_called()


# --- XROSE_BURN preflight: same revert classifier as BASE_MINT (paused +
#     limit-exhausted → RETRY_LATER auto-retry, anything else → AWAITING_CLEAR).
#     Without this branch a paused-bridge or limit-exhausted burn would
#     broadcast, fail on-chain, and stall every later Base nonce in
#     AWAITING_CLEAR until the owner clears it via Accounting.clearCustodyTx.


@pytest.mark.asyncio
async def test_xrose_burn_preflight_paused_stays_queued_for_retry(executor, web3s):
    await _enqueue_xrose_burn(executor, nonce=210)

    base_web3 = web3s[BASE]
    base_web3.eth._burn_raises = ROFL_PAUSED

    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 210)
    assert rec.status == CustodyTxStatus.QUEUED
    assert rec.error == "ROFLBridge paused"
    base_web3.eth.send_raw_transaction.assert_not_called()

    # Second iteration with paused() cleared → broadcasts and the error clears.
    base_web3.eth._burn_raises = None
    _script_receipt(base_web3, _make_receipt(status=1))
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 210)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.error is None
    base_web3.eth.send_raw_transaction.assert_called_once()


@pytest.mark.asyncio
async def test_xrose_burn_preflight_limit_exhausted_stays_queued_for_retry(
    executor, web3s, accounting
):
    await _enqueue_xrose_burn(executor, nonce=211)

    base_web3 = web3s[BASE]
    base_web3.eth._burn_raises = MINT_LIMIT_EXCEEDED  # same selector for burn-limit path

    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 211)
    assert rec.status == CustodyTxStatus.QUEUED
    assert rec.error == "burn limit exhausted"
    base_web3.eth.send_raw_transaction.assert_not_called()
    # The limit-exhausted branch returns before the re-sign.
    accounting.generate_bridge_burn_transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_xrose_burn_preflight_unexpected_revert_marks_awaiting_clear(executor, web3s):
    await _enqueue_xrose_burn(executor, nonce=212)

    base_web3 = web3s[BASE]
    base_web3.eth._burn_raises = UNEXPECTED_REVERT

    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 212)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "xrose burn preflight reverted" in (rec.error or "")
    base_web3.eth.send_raw_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_xrose_burn_preflight_passes_and_broadcasts(executor, web3s, accounting):
    """Clean eth_call → re-sign via generate_bridge_burn_transfer, then
    broadcast the fresh bytes (NOT the gas-frozen sign-time tx). The re-sign
    must not change the record's EVM nonce — the burn nonce is frozen on-chain
    in BridgeBurnRequest.nonce, so only gas can differ between attempts."""
    fresh_signed = b"\xbe" * 100  # accounting fixture's generate_bridge_burn_transfer return
    deposit_id_hex = "0x" + "dd" * 32
    await _enqueue_xrose_burn(executor, nonce=213, deposit_id_hex=deposit_id_hex)

    base_web3 = web3s[BASE]
    base_web3.eth._burn_raises = None
    _script_receipt(base_web3, _make_receipt(status=1))

    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 213)
    assert rec.status == CustodyTxStatus.SUCCESS
    base_web3.eth.send_raw_transaction.assert_called_once()
    # Re-signed off the depositId, and the fresh bytes were broadcast.
    accounting.generate_bridge_burn_transfer.assert_awaited_once_with(
        bytes(HexBytes(deposit_id_hex))
    )
    assert base_web3.eth.send_raw_transaction.call_args[0][0] == fresh_signed
    # Nonce is untouched by the re-sign.
    assert rec.evm_nonce == 213


# --- Duplicate-id recovery: a withdrawalId already minted (or depositId already
#     burned) by a prior incarnation must NOT escalate to AWAITING_CLEAR. The
#     executor re-reads the contract mapping + on-chain Minted/Burned events,
#     matches params against the queued record, and marks recovered SUCCESS.


def _expected_withdrawal_id(index: int = WITHDRAWAL_INDEX) -> bytes:
    return bytes(
        Web3.keccak(
            encode(
                ["address", "uint256", "uint256"],
                [CONTRACT_ADDRESS, SAPPHIRE, index],
            )
        )
    )


@pytest.mark.asyncio
async def test_base_mint_preflight_duplicate_id_matching_minted_event_marks_recovered_success(
    executor, web3s
):
    """eth_call mint() reverts AlreadyProcessed → executor reads
    mintedWithdrawalIds[id]=True → finds matching Minted event → marks
    SUCCESS with the recovered tx hash. No broadcast happens."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    recovered_hash = HexBytes("0x" + "aa" * 32)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 99,
        }
    ]
    _script_recovered_tx(base_web3, tx_hash=recovered_hash, nonce=300)

    await _enqueue_base_mint(executor, nonce=300)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 300)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.tx_hash == recovered_hash.to_0x_hex()
    assert rec.recovered_tx_hash == recovered_hash.to_0x_hex()
    assert rec.receipt_block_number == 99
    assert rec.recovered_block_number == 99
    assert rec.receipt_status == 1
    assert rec.error is None
    assert base_web3.eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_base_mint_preflight_duplicate_id_mismatched_to_marks_awaiting_clear(executor, web3s):
    """AlreadyProcessed revert + mapping=True but Minted.args.to ≠ record.to_address
    → AWAITING_CLEAR (operator must investigate; another caller minted to a
    different recipient under the same id)."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    foreign_to = Web3.to_checksum_address("0x" + "99" * 20)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": foreign_to, "amount": AMOUNT},
            "transactionHash": HexBytes("0x" + "ab" * 32),
            "blockNumber": 99,
        }
    ]

    await _enqueue_base_mint(executor, nonce=301)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 301)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "mismatch" in (rec.error or "").lower()
    assert base_web3.eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_base_mint_preflight_duplicate_id_mismatched_amount_marks_awaiting_clear(
    executor, web3s
):
    """AlreadyProcessed revert + mapping=True but Minted.args.amount ≠ record.amount
    → AWAITING_CLEAR. Same recipient, different amount under the same id is
    still an inconsistency that needs human attention."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT + 1},
            "transactionHash": HexBytes("0x" + "ab" * 32),
            "blockNumber": 99,
        }
    ]

    await _enqueue_base_mint(executor, nonce=302)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 302)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "mismatch" in (rec.error or "").lower()


@pytest.mark.asyncio
async def test_base_mint_preflight_duplicate_id_mapping_false_marks_awaiting_clear(executor, web3s):
    """AlreadyProcessed revert but on-chain mintedWithdrawalIds[id]=False —
    contradictory state. Fail closed instead of recovering."""
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    # Mapping deliberately NOT set: defaults to False via _FakeFunctions.

    await _enqueue_base_mint(executor, nonce=303)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 303)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "mintedwithdrawalids" in (rec.error or "").lower()
    assert base_web3.eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_base_mint_preflight_duplicate_id_no_event_marks_awaiting_clear(executor, web3s):
    """AlreadyProcessed revert + mapping=True but no Minted event found for
    this id → AWAITING_CLEAR (chain state and log history disagree; investigate)."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    # No matching Minted events scripted.

    await _enqueue_base_mint(executor, nonce=304)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 304)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "no minted event" in (rec.error or "").lower()


@pytest.mark.asyncio
async def test_xrose_burn_preflight_duplicate_id_matching_burned_event_marks_recovered_success(
    executor, web3s
):
    """eth_call burn() reverts AlreadyProcessed → executor reads
    burnedDepositIds[id]=True → finds matching Burned event with amount
    matching the queued record → marks SUCCESS. Burned has no `to` field,
    so only `amount` is matched."""
    deposit_id_hex = "0x" + "fa" * 32
    deposit_id = bytes(HexBytes(deposit_id_hex))
    base_web3 = web3s[BASE]
    base_web3.eth._burn_raises = ALREADY_PROCESSED
    base_web3.eth._burned_deposit_ids[deposit_id.hex()] = True
    recovered_hash = HexBytes("0x" + "bb" * 32)
    base_web3.eth._burned_events = [
        {
            "args": {"depositId": deposit_id, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 77,
        }
    ]
    _script_recovered_tx(base_web3, tx_hash=recovered_hash, nonce=400)

    await _enqueue_xrose_burn(executor, nonce=400, deposit_id_hex=deposit_id_hex)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 400)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.tx_hash == recovered_hash.to_0x_hex()
    assert rec.recovered_tx_hash == recovered_hash.to_0x_hex()
    assert rec.receipt_block_number == 77
    assert rec.recovered_block_number == 77
    assert rec.receipt_status == 1
    assert rec.error is None
    assert base_web3.eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_base_mint_receipt_status_zero_duplicate_id_marks_recovered_success(executor, web3s):
    """Preflight passes (no eth_call revert), broadcast lands, receipt comes
    back with status=0 — a race where another caller mined the same id between
    preflight and broadcast. The executor must re-check mintedWithdrawalIds in
    _apply_receipt and mark recovered SUCCESS if the on-chain Minted event
    matches the queued record."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = None
    reverted_receipt = _make_receipt(status=0)
    _script_receipt(base_web3, reverted_receipt)
    # Foreign tx mined the id with matching params after our preflight passed.
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    recovered_hash = HexBytes("0x" + "cc" * 32)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 55,
        }
    ]
    _script_recovered_tx(base_web3, tx_hash=recovered_hash, nonce=500)

    await _enqueue_base_mint(executor, nonce=500)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 500)
    assert rec.status == CustodyTxStatus.SUCCESS
    # Broadcast forensics preserved: tx_hash is our reverted broadcast,
    # receipt_status stays 0. The recovered_* fields carry the matched event.
    assert rec.tx_hash == HexBytes("0x" + "ee" * 32).to_0x_hex()
    assert rec.receipt_status == 0
    assert rec.receipt_block_number == reverted_receipt["blockNumber"]
    assert rec.recovered_tx_hash == recovered_hash.to_0x_hex()
    assert rec.recovered_block_number == 55
    assert rec.error is None


@pytest.mark.asyncio
async def test_receipt_status_zero_recovery_awaiting_clear_propagates_rich_error(executor, web3s):
    """Receipt status=0 + recovery cross-check trips (e.g. foreign signer):
    the surfaced record.error must be the cross-check diagnostic, not the
    generic "receipt status 0". Dropping it on the floor would hide the
    foreign-mint forensics from the operator."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = None  # preflight passes
    _script_receipt(base_web3, _make_receipt(status=0))
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    recovered_hash = HexBytes("0x" + "dd" * 32)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 60,
        }
    ]
    # Recovered tx is from a foreign signer — recovery returns AWAITING_CLEAR
    # with a rich diagnostic. Without C2, this is overwritten by "receipt
    # status 0" before persisting.
    foreign_signer = Web3.to_checksum_address("0x" + "88" * 20)
    _script_recovered_tx(base_web3, tx_hash=recovered_hash, from_address=foreign_signer, nonce=700)

    await _enqueue_base_mint(executor, nonce=700)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 700)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert rec.error is not None
    assert "mismatch" in rec.error.lower()
    # Generic-error string must not have been substituted.
    assert "receipt status" not in rec.error.lower()


# --- Recovery signer+nonce cross-check.
#     Event match alone is insufficient: a rotated roflSigner (setRoflSigner on
#     ROFLBridge) could have minted under the same id, and bridge replay
#     protection is id-based, not nonce-based. Without this guard the executor
#     would flip SUCCESS while the custody EOA's nonce stays unconsumed.


@pytest.mark.asyncio
async def test_preflight_duplicate_id_signer_mismatch_marks_awaiting_clear(executor, web3s):
    """Event params match but eth_getTransactionByHash returns a different
    `from` address → AWAITING_CLEAR (a rotated/foreign signer minted the id)."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    recovered_hash = HexBytes("0x" + "a1" * 32)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 99,
        }
    ]
    foreign_signer = Web3.to_checksum_address("0x" + "77" * 20)
    _script_recovered_tx(base_web3, tx_hash=recovered_hash, from_address=foreign_signer, nonce=600)

    await _enqueue_base_mint(executor, nonce=600)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 600)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "mismatch" in (rec.error or "").lower()
    assert base_web3.eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_preflight_duplicate_id_nonce_mismatch_marks_awaiting_clear(executor, web3s):
    """Same `from` but the recovered tx's nonce ≠ our record's evm_nonce.
    Means a prior incarnation's nonce N tx mined this id under a different
    slot — promoting us would still leave our own nonce N unconsumed."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    recovered_hash = HexBytes("0x" + "a2" * 32)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 99,
        }
    ]
    _script_recovered_tx(base_web3, tx_hash=recovered_hash, nonce=999)

    await _enqueue_base_mint(executor, nonce=601)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 601)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "mismatch" in (rec.error or "").lower()
    assert base_web3.eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_preflight_duplicate_id_tx_not_found_marks_awaiting_clear(executor, web3s):
    """get_transaction returns None (RPC lost the tx, or it never existed at
    that hash) → AWAITING_CLEAR. Cannot verify provenance, so fail closed."""
    withdrawal_id = _expected_withdrawal_id()
    base_web3 = web3s[BASE]
    base_web3.eth._mint_raises = ALREADY_PROCESSED
    base_web3.eth._minted_withdrawal_ids[withdrawal_id.hex()] = True
    recovered_hash = HexBytes("0x" + "a3" * 32)
    base_web3.eth._minted_events = [
        {
            "args": {"withdrawalId": withdrawal_id, "to": TO_ADDRESS, "amount": AMOUNT},
            "transactionHash": recovered_hash,
            "blockNumber": 99,
        }
    ]
    # _transactions dict deliberately empty → get_transaction returns None.

    await _enqueue_base_mint(executor, nonce=602)
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 602)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert "not found" in (rec.error or "").lower()


# --- Processor secondary filter: an index already present in the executor
#     (any status: QUEUED, BROADCAST, AWAITING_CLEAR) must NOT be re-enqueued.
#     Combined with the BLOCKING_STATUSES halt this keeps the chain loop from
#     piling up duplicate records behind a stuck nonce.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing_status",
    [
        CustodyTxStatus.QUEUED,
        CustodyTxStatus.BROADCAST,
        CustodyTxStatus.AWAITING_CLEAR,
    ],
)
async def test_processor_skips_indices_already_in_executor(
    processor, mock_accounting_service, mock_custody_executor, existing_status
):
    """If the executor already has a record for withdrawal index 5 on a chain
    (regardless of that record's status), the processor must filter index 5
    out of the pending list so it is not re-enqueued."""
    existing_record = MagicMock(spec=["withdrawal_index", "status"])
    existing_record.withdrawal_index = 5
    existing_record.status = existing_status
    mock_custody_executor.get_records_for_chain = MagicMock(return_value=[existing_record])

    mock_accounting_service.get_all_pending_withdrawals.return_value = {
        "pending": [
            {"index": 5, "chain_id": BASE, "block_number": 50, "is_bridge_asset": True},
            {"index": 6, "chain_id": BASE, "block_number": 50, "is_bridge_asset": True},
        ],
        "current_block": 100,
    }

    by_chain = await processor._get_pending_withdrawals()

    indices = [w["index"] for w in by_chain.get(BASE, [])]
    assert 5 not in indices
    assert 6 in indices
