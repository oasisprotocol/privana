"""Tests for the shared custody-tx executor.

Tests drive the executor by calling `_process_next_for_chain(chain_id)`
directly instead of spinning up the real chain loop. This keeps the
assertions deterministic and lets each test focus on one invariant.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from hexbytes import HexBytes
from web3.exceptions import TransactionNotFound

from src.services.custody_tx_executor import (
    BLOCKING_STATUSES,
    DEFAULT_STATE_DIR,
    EXECUTOR_CHAIN_IDS,
    TERMINAL_STATUSES,
    CorruptCustodyTxRecordError,
    CustodyTxExecutor,
    CustodyTxKind,
    CustodyTxRecord,
    CustodyTxRequest,
    CustodyTxStartupError,
    CustodyTxStatus,
    get_custody_tx_executor,
    reset_custody_tx_executor,
)

CUSTODY_ADDRESS = "0x" + "ab" * 20
CONTRACT_ADDRESS = "0x" + "cd" * 20
SAPPHIRE = 23295
BASE = 84532


def _tx_hash_for(chain_id: int, nonce: int) -> str:
    """Deterministic fake tx hash keyed by (chain, nonce)."""
    return "0x" + f"{chain_id:08x}{nonce:056x}"


def _make_receipt(
    status: int = 1,
    block_number: int = 100,
    *,
    gas_used: int = 21000,
    effective_gas_price: int = 20_000_000_000,
) -> Dict[str, Any]:
    return {
        "status": status,
        "blockNumber": block_number,
        "transactionHash": HexBytes("0x" + "ee" * 32),
        "gasUsed": gas_used,
        "effectiveGasPrice": effective_gas_price,
    }


class _AwaitableValue:
    """Re-awaitable wrapper so ``await w3.eth.gas_price`` works in mocks."""

    def __init__(self, value: int) -> None:
        self._value = value

    def __await__(self):
        if False:
            yield
        return self._value


class _FakeChainEth:
    """Async-mock-shaped substitute for `AsyncWeb3.eth` with explicit scripting."""

    def __init__(self) -> None:
        self.send_raw_transaction = AsyncMock()
        self.get_transaction_receipt = AsyncMock()
        self.get_transaction_count = AsyncMock(return_value=0)
        self.get_balance = AsyncMock(return_value=10**20)
        self._gas_price_wei = 20_000_000_000

    @property
    def gas_price(self):
        return _AwaitableValue(self._gas_price_wei)


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
    return svc


@pytest.fixture
def executor(state_dir: str, accounting) -> CustodyTxExecutor:
    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    return ex


async def _enqueue(
    executor: CustodyTxExecutor,
    chain_id: int,
    nonce: int,
    kind: CustodyTxKind = CustodyTxKind.NORMAL_WITHDRAWAL,
    id: str = "1",
) -> str:
    """Helper: enqueue a request through the kind-routed preflight path.

    The kind-routed dispatcher returns ALLOW for NORMAL_WITHDRAWAL,
    XROSE_BURN, and bridge kinds with missing metadata, so this helper
    relies on the broadcast scripting to drive outcomes — preflight
    failure paths are exercised in test_rose_bridge_withdrawal.py.
    """
    return await executor.enqueue(
        CustodyTxRequest(
            chain_id=chain_id,
            evm_nonce=nonce,
            kind=kind,
            id=id,
            signed_tx=bytes.fromhex(f"de{nonce:04x}beef"),
        )
    )


def _script_broadcast_success(web3: _FakeChainWeb3, chain_id: int, nonces: List[int]) -> None:
    """Configure send_raw_transaction to return a stable tx_hash per nonce and
    get_transaction_receipt to return status=1 for each tx_hash.
    """
    hashes_by_nonce = {n: _tx_hash_for(chain_id, n) for n in nonces}
    hashes_in_order = [hashes_by_nonce[n] for n in nonces]
    web3.eth.send_raw_transaction.side_effect = [HexBytes(h) for h in hashes_in_order]
    # Keep the on-chain next nonce at the top of the batch so the pre-broadcast
    # nonce floor treats every scripted nonce as ready-to-mine, not a future tx.
    web3.eth.get_transaction_count = AsyncMock(return_value=max(nonces))

    receipts_by_hash = {
        h: _make_receipt(status=1, block_number=1000 + i) for i, h in enumerate(hashes_in_order)
    }

    async def _receipt(tx_hash: Any) -> Dict[str, Any]:
        key = HexBytes(tx_hash).to_0x_hex()
        if key not in receipts_by_hash:
            raise TransactionNotFound(key)
        return receipts_by_hash[key]

    web3.eth.get_transaction_receipt.side_effect = _receipt


@pytest.mark.asyncio
async def test_base_mint_burn_mint_in_three_consecutive_nonces(executor, web3s):
    """Base mint @ n, burn @ n+1, second mint @ n+2 — all enter the same queue
    and resolve in order, with the executor never advancing past an unresolved
    earlier nonce."""
    _script_broadcast_success(web3s[BASE], BASE, [100, 101, 102])
    await _enqueue(executor, BASE, 100, kind=CustodyTxKind.BASE_MINT, id="m0")
    await _enqueue(executor, BASE, 101, kind=CustodyTxKind.XROSE_BURN, id="b0")
    await _enqueue(executor, BASE, 102, kind=CustodyTxKind.BASE_MINT, id="m1")

    for _ in range(3):
        await executor._process_next_for_chain(BASE)

    records = executor.get_records_for_chain(BASE)
    assert [(r.evm_nonce, r.kind, r.status) for r in records] == [
        (100, CustodyTxKind.BASE_MINT, CustodyTxStatus.SUCCESS),
        (101, CustodyTxKind.XROSE_BURN, CustodyTxStatus.SUCCESS),
        (102, CustodyTxKind.BASE_MINT, CustodyTxStatus.SUCCESS),
    ]


@pytest.mark.asyncio
async def test_user_nonce_decoupled_from_dest_tx_nonce(executor, web3s):
    """Bridge withdrawals carry both a user-side EIP-712 nonce and a custody-EOA
    dest_tx_nonce. The executor only ever sees and uses the dest_tx_nonce.
    A request enqueued with evm_nonce=205 (the dest_tx_nonce) resolves at slot 205
    regardless of what the user_nonce was upstream."""
    _script_broadcast_success(web3s[BASE], BASE, [205])
    key = await _enqueue(executor, BASE, 205, kind=CustodyTxKind.BASE_MINT, id="user-nonce-5")
    await executor._process_next_for_chain(BASE)

    record = executor.get_record(BASE, 205)
    assert record is not None
    assert record.status == CustodyTxStatus.SUCCESS
    assert record.evm_nonce == 205
    assert record.id == "user-nonce-5"
    assert executor._record_key(BASE, 205) == key


@pytest.mark.asyncio
async def test_normal_withdrawal_on_either_chain_uses_executor(executor, web3s):
    _script_broadcast_success(web3s[BASE], BASE, [50])
    _script_broadcast_success(web3s[SAPPHIRE], SAPPHIRE, [42])

    await _enqueue(executor, BASE, 50, kind=CustodyTxKind.NORMAL_WITHDRAWAL, id="b-wd")
    await _enqueue(executor, SAPPHIRE, 42, kind=CustodyTxKind.NORMAL_WITHDRAWAL, id="s-wd")

    await executor._process_next_for_chain(BASE)
    await executor._process_next_for_chain(SAPPHIRE)

    assert executor.get_record(BASE, 50).status == CustodyTxStatus.SUCCESS
    assert executor.get_record(SAPPHIRE, 42).status == CustodyTxStatus.SUCCESS


@pytest.mark.asyncio
async def test_awaiting_clear_blocks_next_nonce(executor, web3s):
    """If nonce n is in AWAITING_CLEAR, the executor must NOT broadcast n+1
    even though n+1 itself is ready."""
    # send_raw_transaction fails on nonce 300 → AWAITING_CLEAR; nonce 301 must stay QUEUED.
    web3s[BASE].eth.send_raw_transaction.side_effect = Exception("rpc explode")
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=300)
    await _enqueue(executor, BASE, 300, kind=CustodyTxKind.XROSE_BURN, id="failed")
    await _enqueue(executor, BASE, 301, kind=CustodyTxKind.BASE_MINT, id="next")

    for _ in range(5):
        await executor._process_next_for_chain(BASE)

    rec_300 = executor.get_record(BASE, 300)
    rec_301 = executor.get_record(BASE, 301)
    assert rec_300.status == CustodyTxStatus.AWAITING_CLEAR
    assert rec_301.status == CustodyTxStatus.QUEUED
    # send_raw_transaction was attempted once (for 300) then 301 never tried.
    assert web3s[BASE].eth.send_raw_transaction.call_count == 1


@pytest.mark.asyncio
async def test_success_at_n_unblocks_n_plus_one(executor, web3s):
    _script_broadcast_success(web3s[BASE], BASE, [400, 401])
    await _enqueue(executor, BASE, 400, kind=CustodyTxKind.BASE_MINT, id="a")
    await _enqueue(executor, BASE, 401, kind=CustodyTxKind.BASE_MINT, id="b")

    await executor._process_next_for_chain(BASE)
    assert executor.get_record(BASE, 400).status == CustodyTxStatus.SUCCESS
    assert executor.get_record(BASE, 401).status == CustodyTxStatus.QUEUED

    await executor._process_next_for_chain(BASE)
    assert executor.get_record(BASE, 401).status == CustodyTxStatus.SUCCESS


@pytest.mark.asyncio
async def test_no_broadcast_for_next_until_previous_resolves(executor, web3s):
    """Two QUEUED records; first not-yet-processed, second present. A single
    chain-loop pass must broadcast only the lower nonce, not both."""
    _script_broadcast_success(web3s[BASE], BASE, [500])
    await _enqueue(executor, BASE, 500, kind=CustodyTxKind.BASE_MINT, id="first")
    await _enqueue(executor, BASE, 501, kind=CustodyTxKind.BASE_MINT, id="second")

    await executor._process_next_for_chain(BASE)

    assert web3s[BASE].eth.send_raw_transaction.call_count == 1
    assert executor.get_record(BASE, 500).status == CustodyTxStatus.SUCCESS
    assert executor.get_record(BASE, 501).status == CustodyTxStatus.QUEUED


@pytest.mark.asyncio
async def test_success_only_after_receipt_status_one(executor, web3s):
    """Until a receipt with status=1 is observed, the record must NOT be
    marked SUCCESS. Test by handing back a status=0 receipt — record must
    become AWAITING_CLEAR, not SUCCESS."""
    tx_hash = _tx_hash_for(BASE, 600)
    web3s[BASE].eth.send_raw_transaction.side_effect = [HexBytes(tx_hash)]
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=600)

    async def _reverted_receipt(_h: Any) -> Dict[str, Any]:
        return _make_receipt(status=0)

    web3s[BASE].eth.get_transaction_receipt.side_effect = _reverted_receipt

    await _enqueue(executor, BASE, 600, kind=CustodyTxKind.BASE_MINT, id="rev")
    await executor._process_next_for_chain(BASE)

    rec = executor.get_record(BASE, 600)
    assert rec.status == CustodyTxStatus.AWAITING_CLEAR
    assert rec.receipt_status == 0


@pytest.mark.asyncio
async def test_reverted_receipt_blocks_later_nonces(executor, web3s):
    """receipt.status == 0 at nonce n must keep nonce n+1 QUEUED indefinitely."""
    tx_hash = _tx_hash_for(BASE, 700)
    web3s[BASE].eth.send_raw_transaction.side_effect = [HexBytes(tx_hash)]
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=700)

    async def _reverted(_h: Any) -> Dict[str, Any]:
        return _make_receipt(status=0)

    web3s[BASE].eth.get_transaction_receipt.side_effect = _reverted

    await _enqueue(executor, BASE, 700, kind=CustodyTxKind.BASE_MINT)
    await _enqueue(executor, BASE, 701, kind=CustodyTxKind.BASE_MINT)

    for _ in range(5):
        await executor._process_next_for_chain(BASE)

    assert executor.get_record(BASE, 700).status == CustodyTxStatus.AWAITING_CLEAR
    assert executor.get_record(BASE, 701).status == CustodyTxStatus.QUEUED
    assert web3s[BASE].eth.send_raw_transaction.call_count == 1


# Preflight transitions (paused / mint-limit / gas-cap / actual-over-cap) now
# live in test/py/test_rose_bridge_withdrawal.py.


@pytest.mark.asyncio
async def test_restart_after_queued_keeps_nonce(state_dir, accounting, web3s):
    """A QUEUED record persisted to disk must come back at the same nonce after
    a fresh executor instance is constructed."""
    ex1 = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await _enqueue(ex1, BASE, 1100, kind=CustodyTxKind.BASE_MINT, id="durable")
    # Simulate restart — drop ex1, build ex2 pointed at the same state_dir
    ex2 = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )

    records = ex2.get_records_for_chain(BASE)
    assert len(records) == 1
    assert records[0].evm_nonce == 1100
    assert records[0].status == CustodyTxStatus.QUEUED
    assert records[0].kind == CustodyTxKind.BASE_MINT


@pytest.mark.asyncio
async def test_restart_after_broadcast_reconciles_by_tx_hash(state_dir, accounting, web3s):
    """If the writer crashed AFTER tx_hash persist but BEFORE the receipt
    arrived, reconcile_on_startup must look up the receipt by tx_hash and
    promote the record to SUCCESS when status=1."""
    tx_hash = _tx_hash_for(BASE, 1200)
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1200,
        kind=CustodyTxKind.BASE_MINT,
        id="cr",
        signed_tx_hex="0xdeadbeef",
        tx_hash=tx_hash,
        status=CustodyTxStatus.BROADCAST,
    )
    Path(state_dir, f"custody_tx_{BASE}_1200.json").write_text(json.dumps(record.to_dict()))

    async def _receipt(_h: Any) -> Dict[str, Any]:
        return _make_receipt(status=1)

    web3s[BASE].eth.get_transaction_receipt.side_effect = _receipt

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await ex.reconcile_on_startup()
    refreshed = ex.get_record(BASE, 1200)
    assert refreshed.status == CustodyTxStatus.SUCCESS
    assert refreshed.receipt_status == 1


@pytest.mark.asyncio
async def test_restart_missing_tx_hash_reconciles_by_nonce_status_1(state_dir, accounting, web3s):
    """Writer crashed BEFORE persisting tx_hash but AFTER the chain accepted
    the tx. Reconcile derives the expected tx hash from signed_tx_hex
    (keccak256), looks up the receipt, and promotes to SUCCESS only when
    that receipt reports status == 1.
    """
    from eth_utils import keccak

    signed_bytes = b"\xde\xad\xbe\xef\x01"
    expected_hash = "0x" + keccak(signed_bytes).hex()
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1300,
        kind=CustodyTxKind.BASE_MINT,
        id="orphan",
        signed_tx_hex="0x" + signed_bytes.hex(),
        tx_hash=None,
        status=CustodyTxStatus.BROADCAST,
    )
    Path(state_dir, f"custody_tx_{BASE}_1300.json").write_text(json.dumps(record.to_dict()))

    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1301)

    async def _receipt(h: Any) -> Dict[str, Any]:
        assert HexBytes(h).to_0x_hex() == expected_hash
        return _make_receipt(status=1)

    web3s[BASE].eth.get_transaction_receipt.side_effect = _receipt

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await ex.reconcile_on_startup()

    refreshed = ex.get_record(BASE, 1300)
    assert refreshed.status == CustodyTxStatus.SUCCESS
    assert refreshed.tx_hash == expected_hash
    assert refreshed.receipt_status == 1


@pytest.mark.asyncio
async def test_reconcile_by_nonce_with_reverted_receipt_marks_awaiting_clear(
    state_dir, accounting, web3s
):
    """Nonce advanced AND a receipt exists at the expected hash, but the
    receipt has status == 0. Must NOT mark SUCCESS — the tx reverted on
    chain. A naive nonce-only check would have promoted this record."""
    from eth_utils import keccak

    signed_bytes = b"\x02" * 8
    expected_hash = "0x" + keccak(signed_bytes).hex()
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1310,
        kind=CustodyTxKind.BASE_MINT,
        id="reverted",
        signed_tx_hex="0x" + signed_bytes.hex(),
        tx_hash=None,
        status=CustodyTxStatus.BROADCAST,
    )
    Path(state_dir, f"custody_tx_{BASE}_1310.json").write_text(json.dumps(record.to_dict()))

    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1311)

    async def _receipt(h: Any) -> Dict[str, Any]:
        assert HexBytes(h).to_0x_hex() == expected_hash
        return _make_receipt(status=0)

    web3s[BASE].eth.get_transaction_receipt.side_effect = _receipt

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await ex.reconcile_on_startup()

    refreshed = ex.get_record(BASE, 1310)
    assert refreshed.status == CustodyTxStatus.AWAITING_CLEAR
    assert refreshed.receipt_status == 0


@pytest.mark.asyncio
async def test_reconcile_by_nonce_foreign_tx_burned_slot_marks_awaiting_clear(
    state_dir, accounting, web3s
):
    """Nonce advanced but the receipt lookup at our expected hash returns
    TransactionNotFound — another tx (replacement, cancellation, external
    sender) burned the slot. Must NOT mark SUCCESS."""
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1320,
        kind=CustodyTxKind.BASE_MINT,
        id="foreign",
        signed_tx_hex="0x" + ("03" * 8),
        tx_hash=None,
        status=CustodyTxStatus.BROADCAST,
    )
    Path(state_dir, f"custody_tx_{BASE}_1320.json").write_text(json.dumps(record.to_dict()))

    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1321)

    async def _receipt(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not found")

    web3s[BASE].eth.get_transaction_receipt.side_effect = _receipt

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await ex.reconcile_on_startup()

    refreshed = ex.get_record(BASE, 1320)
    assert refreshed.status == CustodyTxStatus.AWAITING_CLEAR


# The kind-routed dispatcher reconstructs every bridge preflight from
# persisted record fields, so a restart between enqueue and broadcast
# keeps the policy gate.


@pytest.mark.asyncio
async def test_nonce_gap_blocks_processing(state_dir, accounting, web3s):
    """SUCCESS at n, QUEUED at n+2, no record at n+1 → executor must NOT
    broadcast n+2 (the chain wouldn't mine it anyway, and proceeding would
    mask a missing reservation that catch-up failed to fill)."""
    success = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1600,
        kind=CustodyTxKind.BASE_MINT,
        id="ok",
        signed_tx_hex="0x00",
        status=CustodyTxStatus.SUCCESS,
        receipt_status=1,
    )
    gap = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1602,
        kind=CustodyTxKind.BASE_MINT,
        id="gapped",
        signed_tx_hex="0x01",
        status=CustodyTxStatus.QUEUED,
    )
    for rec in (success, gap):
        Path(state_dir, f"custody_tx_{BASE}_{rec.evm_nonce}.json").write_text(
            json.dumps(rec.to_dict())
        )

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )

    for _ in range(3):
        await ex._process_next_for_chain(BASE)

    refreshed = ex.get_record(BASE, 1602)
    assert refreshed.status == CustodyTxStatus.QUEUED
    assert web3s[BASE].eth.send_raw_transaction.call_count == 0


@pytest.mark.asyncio
async def test_lone_future_nonce_blocks_below_on_chain_floor(state_dir, accounting, web3s, caplog):
    """A SINGLE runnable record at a future nonce (no predecessor on disk to
    fail the +1 guard against) must NOT broadcast: the chain can't mine it and
    receipt retries would push it to AWAITING_CLEAR, stalling the loop forever.
    The absolute on-chain nonce floor must block it instead."""
    lone = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1801,
        kind=CustodyTxKind.BASE_MINT,
        id="lone-future",
        signed_tx_hex="0x00",
        status=CustodyTxStatus.QUEUED,
    )
    Path(state_dir, f"custody_tx_{BASE}_1801.json").write_text(json.dumps(lone.to_dict()))
    # On-chain next nonce is BELOW the record's nonce → future tx.
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1800)

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )

    with caplog.at_level(logging.CRITICAL, logger="src.services.custody_tx_executor"):
        for _ in range(3):
            await ex._process_next_for_chain(BASE)

    refreshed = ex.get_record(BASE, 1801)
    assert refreshed.status == CustodyTxStatus.QUEUED
    assert web3s[BASE].eth.send_raw_transaction.call_count == 0
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


@pytest.mark.asyncio
async def test_lone_nonce_at_on_chain_floor_broadcasts(executor, web3s):
    """The floor is future-only: when the on-chain next nonce equals the
    record's nonce it is the normal next-to-mine slot and must broadcast."""
    _script_broadcast_success(web3s[BASE], BASE, [1810])
    # The floor probe re-reads get_transaction_count; it must equal the record
    # nonce so the record is treated as the next-to-mine slot, not a future tx.
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1810)

    await _enqueue(executor, BASE, 1810, kind=CustodyTxKind.BASE_MINT, id="at-floor")
    await executor._process_next_for_chain(BASE)

    assert executor.get_record(BASE, 1810).status == CustodyTxStatus.SUCCESS
    assert web3s[BASE].eth.send_raw_transaction.call_count == 1


@pytest.mark.asyncio
async def test_receipt_timeout_escalates_to_awaiting_clear(state_dir, accounting, web3s):
    """A BROADCAST record whose nonce never advances past the wall-clock
    deadline must not stall the chain forever. With a zero deadline the first
    probe (nonce unadvanced, elapsed > 0) escalates to AWAITING_CLEAR so an
    operator can decide what to do."""
    tx_hash_for_test = _tx_hash_for(BASE, 1700)
    web3s[BASE].eth.send_raw_transaction.side_effect = [HexBytes(tx_hash_for_test)]
    # Nonce stays at 1700: our broadcast tx is never mined, slot never advances.
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1700)

    async def _never_receipt(_h: Any) -> Dict[str, Any]:
        raise TransactionNotFound("not yet")

    web3s[BASE].eth.get_transaction_receipt.side_effect = _never_receipt

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.0,
        receipt_timeout_seconds=0,
        receipt_probe_interval=1,
        receipt_stuck_deadline_seconds=0.0,
    )
    await _enqueue(ex, BASE, 1700, kind=CustodyTxKind.BASE_MINT, id="stuck")
    # First pass broadcasts, times out, probes the nonce (unadvanced), and with a
    # zero deadline escalates immediately.
    await ex._process_next_for_chain(BASE)

    rec_final = ex.get_record(BASE, 1700)
    assert rec_final.status == CustodyTxStatus.AWAITING_CLEAR
    assert "deadline" in (rec_final.error or "")
    assert rec_final.retry_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        CustodyTxStatus.AWAITING_CLEAR,
        CustodyTxStatus.AWAITING_CLEAR_GAS_CAP,
    ],
)
async def test_blocking_status_survives_restart(state_dir, accounting, status):
    """An operator-only-clear blocking state on disk must NOT be silently
    re-broadcast on restart. (WAITING_FOR_GAS_CAP is auto-retried — its
    survive-restart behavior is covered by the gas-cap preflight tests in
    test_rose_bridge_withdrawal.py.)"""
    record = CustodyTxRecord(
        chain_id=SAPPHIRE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1400,
        kind=CustodyTxKind.SAPPHIRE_RELEASE,
        id="blocked",
        signed_tx_hex="0xdeadbeef",
        status=status,
    )
    Path(state_dir, f"custody_tx_{SAPPHIRE}_1400.json").write_text(json.dumps(record.to_dict()))

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await ex.reconcile_on_startup()

    refreshed = ex.get_record(SAPPHIRE, 1400)
    assert refreshed is not None
    assert refreshed.status == status
    # And the chain loop must NOT advance past it
    await ex._process_next_for_chain(SAPPHIRE)
    assert ex.get_record(SAPPHIRE, 1400).status == status


@pytest.mark.asyncio
async def test_catchup_loads_onchain_reservations(state_dir, accounting):
    """With an empty state dir but pending withdrawal queue + BridgeBurnReserved
    events on-chain, reconcile_on_startup + load_from_onchain_reservations must
    seed the queue."""
    # Mock the contract reader to expose:
    # - 1 resolved withdrawal at index 0 with bridge-asset txIdentifier
    # - 1 BridgeBurnReserved event at chain=84532 nonce=2200
    from eth_abi import encode as _abi_encode

    bridge_tx_id = _abi_encode(
        ["uint256", "uint64", "address", "uint256"],
        [BASE, 2100, "0x" + "11" * 20, 10**18],
    )
    withdrawal_entry = (
        "0x" + "22" * 20,  # user
        "0x" + "33" * 20,  # to
        100,  # amount
        12345,  # block_number
        b"\xab" * 32,  # token_id
        True,  # resolved
        bridge_tx_id,  # txIdentifier
    )
    fake_signed_tx = b"\x01\x02\x03\x04"

    # Build the mock reader contract with the function/event surface the
    # executor's catch-up uses.
    contract = MagicMock()

    async def _withdrawalCount_call() -> int:
        return 1

    async def _withdrawals_call(_idx: int) -> tuple:
        return withdrawal_entry

    async def _generate_bridge_burn(_id: bytes) -> bytes:
        return fake_signed_tx

    contract.functions.withdrawalCount.return_value.call = _withdrawalCount_call
    contract.functions.withdrawals.return_value.call = _withdrawals_call
    contract.functions.withdrawals.side_effect = lambda idx: SimpleNamespace(
        call=lambda: _withdrawals_call(idx)
    )
    # Bridge records must NOT go through resolveWithdrawal — the on-chain
    # dispatcher reverts UnsupportedTokenType for TokenType.BridgeAsset.
    contract.functions.resolveWithdrawal.return_value.call = AsyncMock(
        side_effect=AssertionError("resolveWithdrawal must not be called for bridge records")
    )
    contract.functions.generateBridgeBurnTransfer.side_effect = lambda did: SimpleNamespace(
        call=lambda: _generate_bridge_burn(did)
    )
    accounting.resolve_bridge_withdrawal = AsyncMock(return_value=fake_signed_tx)

    # BridgeBurnReserved reservation: simulate a single past event for chain=BASE, nonce=2200
    burn_reservation = SimpleNamespace(
        deposit_id=b"\xcc" * 32,
        chain_id=BASE,
        bridge="0x" + "44" * 20,
        amount=10**18,
        nonce=2200,
        block_number=999,
    )

    accounting._get_reader_contract = MagicMock(return_value=contract)
    accounting.list_bridge_burn_reservations = AsyncMock(return_value=[burn_reservation])

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.001,
        receipt_timeout_seconds=2,
    )
    await ex.reconcile_on_startup()
    inserted = await ex.load_from_onchain_reservations()
    assert inserted == 2

    rec_wd = ex.get_record(BASE, 2100)
    rec_burn = ex.get_record(BASE, 2200)
    assert rec_wd is not None and rec_wd.kind == CustodyTxKind.BASE_MINT
    assert rec_wd.status == CustodyTxStatus.QUEUED
    assert rec_burn is not None and rec_burn.kind == CustodyTxKind.XROSE_BURN
    assert rec_burn.status == CustodyTxStatus.QUEUED


@pytest.mark.asyncio
async def test_startup_gas_balance_below_threshold_raises(executor, web3s):
    """If the custody EOA balance on ANY managed chain is below the
    threshold, verify_startup_gas_balances must raise — and the caller
    (main.py) must not proceed to start()."""
    web3s[BASE].eth.get_balance = AsyncMock(return_value=1)  # well below 10^13
    with pytest.raises(CustodyTxStartupError) as exc_info:
        await executor.verify_startup_gas_balances()
    assert "84532" in str(exc_info.value)


@pytest.mark.asyncio
async def test_startup_gas_balance_meeting_threshold_passes(executor, web3s):
    web3s[BASE].eth.get_balance = AsyncMock(return_value=10**14)
    web3s[SAPPHIRE].eth.get_balance = AsyncMock(return_value=10**14)
    # No raise
    await executor.verify_startup_gas_balances()


@pytest.mark.asyncio
async def test_enqueue_rejects_unmanaged_chain(executor):
    with pytest.raises(ValueError, match="not managed"):
        await _enqueue(executor, 1, 0, kind=CustodyTxKind.BASE_MINT)


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_at_same_nonce(state_dir, accounting):
    """A second enqueue for the same (chain_id, nonce) returns the existing
    record's key without overwriting any persisted fields. This subsumes
    both 'caller retried after success' and 'two callers raced at the same
    slot' under one safe rule: existing records are immutable.
    """
    existing = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=9999,
        kind=CustodyTxKind.BASE_MINT,
        id="prior",
        signed_tx_hex="0xprior",
        status=CustodyTxStatus.BROADCAST,
    )
    Path(state_dir, f"custody_tx_{BASE}_9999.json").write_text(json.dumps(existing.to_dict()))

    ex = CustodyTxExecutor(accounting_service=accounting, state_dir=state_dir)
    key = await _enqueue(ex, BASE, 9999, kind=CustodyTxKind.BASE_MINT, id="dup")
    assert key == ex._record_key(BASE, 9999)

    preserved = ex.get_record(BASE, 9999)
    assert preserved is not None
    assert preserved.id == "prior"
    assert preserved.signed_tx_hex == "0xprior"
    assert preserved.status == CustodyTxStatus.BROADCAST


@pytest.mark.asyncio
async def test_concurrent_enqueue_idempotent(state_dir, accounting):
    """Two concurrent enqueues at the same (chain_id, nonce) — exactly one
    record persists; both callers get the same key back."""
    ex = CustodyTxExecutor(accounting_service=accounting, state_dir=state_dir)
    import asyncio as _asyncio

    async def _do(id_str: str) -> str:
        return await _enqueue(ex, BASE, 7777, kind=CustodyTxKind.BASE_MINT, id=id_str)

    key_a, key_b = await _asyncio.gather(_do("a"), _do("b"))
    assert key_a == key_b
    records = ex.get_records_for_chain(BASE)
    assert len(records) == 1
    # The first writer wins; the second's enqueue is observed as a no-op.
    assert records[0].id in {"a", "b"}


# Migration record shape: every persisted dict must round-trip through
# to_dict/from_dict without losing fields. Acts as a forward-compat guard.


def test_record_to_dict_round_trip():
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=7,
        kind=CustodyTxKind.XROSE_BURN,
        id="round-trip",
        signed_tx_hex="0xdeadbeef",
        tx_hash="0xfeedface",
        status=CustodyTxStatus.BROADCAST,
        receipt_block_number=999,
        receipt_status=1,
        error=None,
    )
    rt = CustodyTxRecord.from_dict(record.to_dict())
    assert rt == record


def test_record_migration_adds_missing_fields():
    legacy = {
        "chain_id": SAPPHIRE,
        "accounting_contract_address": CONTRACT_ADDRESS,
        "evm_sender": CUSTODY_ADDRESS,
        "evm_nonce": 3,
        "kind": "sapphire_release",
        "id": "old",
        "signed_tx_hex": "0xdead",
        "status": "queued",
        "created_at": 1.0,
    }
    rt = CustodyTxRecord.from_dict(legacy)
    assert rt.tx_hash is None
    assert rt.receipt_block_number is None
    assert rt.error is None
    assert rt.retry_count == 0


def test_status_set_constants():
    assert CustodyTxStatus.SUCCESS in TERMINAL_STATUSES
    assert CustodyTxStatus.AWAITING_CLEAR in TERMINAL_STATUSES
    assert CustodyTxStatus.AWAITING_CLEAR_GAS_CAP in TERMINAL_STATUSES
    assert CustodyTxStatus.WAITING_FOR_GAS_CAP not in TERMINAL_STATUSES  # auto-retried
    # Only operator-only-clear states halt the chain loop.
    assert CustodyTxStatus.AWAITING_CLEAR in BLOCKING_STATUSES
    assert CustodyTxStatus.AWAITING_CLEAR_GAS_CAP in BLOCKING_STATUSES
    assert CustodyTxStatus.WAITING_FOR_GAS_CAP not in BLOCKING_STATUSES
    assert CustodyTxStatus.SUCCESS not in BLOCKING_STATUSES


def test_executor_chain_ids_constant():
    assert set(EXECUTOR_CHAIN_IDS) == {SAPPHIRE, BASE}


def test_factory_managed_chains_follow_config(tmp_path, monkeypatch):
    """The factory must derive the managed chain set from
    ``settings.chain_rpc_urls`` — including Sapphire MAINNET (23294) when that
    is the configured net — never from the testnet-default module constant."""
    # Redirect the constructor's bound default state dir away from /data.
    patched_defaults = tuple(
        str(tmp_path) if d == DEFAULT_STATE_DIR else d
        for d in CustodyTxExecutor.__init__.__defaults__
    )
    monkeypatch.setattr(CustodyTxExecutor.__init__, "__defaults__", patched_defaults)

    svc = SimpleNamespace()
    svc.settings = SimpleNamespace(chain_rpc_urls={23294: "http://x", 8453: "http://y"})

    reset_custody_tx_executor()
    try:
        executor = get_custody_tx_executor(svc)
        assert executor._chain_ids == (8453, 23294)
    finally:
        reset_custody_tx_executor()


@pytest.mark.asyncio
async def test_corrupt_record_raises_instead_of_silent_drop(state_dir, accounting):
    """A blocking-state record damaged on disk must not silently look like an
    empty slot — that would let the next enqueue overwrite an operator
    block. Loading raises, the chain loop refuses the nonce until repair."""
    path = Path(state_dir, f"custody_tx_{BASE}_5000.json")
    path.write_text("{not-valid-json")

    ex = CustodyTxExecutor(accounting_service=accounting, state_dir=state_dir)
    with pytest.raises(CorruptCustodyTxRecordError):
        ex.get_record(BASE, 5000)
    assert path.exists(), "corrupt file must not be silently renamed"


@pytest.mark.asyncio
async def test_load_all_records_ignores_clear_watcher_cursor(state_dir, accounting):
    """The clear watcher persists `custody_tx_clear_cursor.json` into the same
    state dir, sharing the `custody_tx_` prefix. The full-dir scan must treat it
    as a sidecar and skip it — not parse it as a record and crash the executor."""
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=7000,
        kind=CustodyTxKind.BASE_MINT,
        id="real",
        signed_tx_hex="0xdeadbeef",
        status=CustodyTxStatus.QUEUED,
    )
    Path(state_dir, f"custody_tx_{BASE}_7000.json").write_text(json.dumps(record.to_dict()))
    Path(state_dir, "custody_tx_clear_cursor.json").write_text(
        json.dumps({"cursor": 123, "seen": [], "pending": []})
    )

    ex = CustodyTxExecutor(accounting_service=accounting, state_dir=state_dir)
    records = ex._load_all_records()
    assert [(r.chain_id, r.evm_nonce, r.id) for r in records] == [(BASE, 7000, "real")]


@pytest.mark.asyncio
async def test_load_all_records_still_raises_on_corrupt_record(state_dir, accounting):
    """The sidecar skip must not weaken the fail-closed guard: a genuine
    `custody_tx_<int>_<int>.json` that fails to parse still raises."""
    Path(state_dir, f"custody_tx_{BASE}_7001.json").write_text("{not-valid-json")

    ex = CustodyTxExecutor(accounting_service=accounting, state_dir=state_dir)
    with pytest.raises(CorruptCustodyTxRecordError):
        ex._load_all_records()


@pytest.mark.asyncio
async def test_reconcile_by_tx_hash_rpc_error_escalates_past_deadline(state_dir, accounting, web3s):
    """A BROADCAST record whose receipt lookup keeps failing and whose nonce
    never advances must escalate via the wall-clock deadline so a permanently
    broken RPC eventually surfaces to an operator instead of stalling forever."""
    tx_hash = _tx_hash_for(BASE, 1801)
    record = CustodyTxRecord(
        chain_id=BASE,
        accounting_contract_address=CONTRACT_ADDRESS,
        evm_sender=CUSTODY_ADDRESS,
        evm_nonce=1801,
        kind=CustodyTxKind.BASE_MINT,
        id="rpc-flake",
        signed_tx_hex="0x" + "ab" * 64,
        tx_hash=tx_hash,
        status=CustodyTxStatus.BROADCAST,
    )
    Path(state_dir, f"custody_tx_{BASE}_1801.json").write_text(json.dumps(record.to_dict()))

    async def _rpc_blow_up(_h: Any) -> Dict[str, Any]:
        raise RuntimeError("provider 502")

    web3s[BASE].eth.get_transaction_receipt.side_effect = _rpc_blow_up
    # Nonce never advances past the broadcast slot.
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1801)

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        receipt_probe_interval=1,
        receipt_stuck_deadline_seconds=0.0,
    )
    # BROADCAST branch: _reconcile_by_tx_hash fails, _handle_receipt_retry probes
    # the (unadvanced) nonce and the zero deadline escalates on the first pass.
    await ex._process_next_for_chain(BASE)

    final = ex.get_record(BASE, 1801)
    assert final.status == CustodyTxStatus.AWAITING_CLEAR
    assert "deadline" in (final.error or "")
    assert final.retry_count >= 1


@pytest.mark.asyncio
async def test_post_broadcast_persist_failure_escalates_to_awaiting_clear(
    state_dir, accounting, web3s, monkeypatch
):
    """If the post-broadcast _save_record raises (disk full, EROFS), the
    in-memory tx_hash would be lost and a retry would re-broadcast against
    a chain that already mined the tx. Escalate to AWAITING_CLEAR so an
    operator correlates the broadcast log line with the chain state."""
    web3s[BASE].eth.send_raw_transaction.side_effect = [HexBytes(_tx_hash_for(BASE, 1900))]
    web3s[BASE].eth.get_transaction_count = AsyncMock(return_value=1900)

    ex = CustodyTxExecutor(
        accounting_service=accounting,
        state_dir=state_dir,
        poll_interval_seconds=0.0,
        receipt_timeout_seconds=2,
    )
    await _enqueue(ex, BASE, 1900, kind=CustodyTxKind.BASE_MINT, id="persist-fail")

    original_save = ex._save_record
    call_count = {"n": 0}

    def _flaky_save(record: CustodyTxRecord) -> None:
        call_count["n"] += 1
        # _enqueue ran with the unpatched method. After that, the broadcast
        # path saves twice: call 1 records the pre-broadcast `broadcast_hashes`
        # entry; call 2 is the post-broadcast persist of BROADCAST + tx_hash.
        # Raising on call 2 simulates the disk failure this test targets;
        # call 3 is the AWAITING_CLEAR save inside the except clause and must
        # succeed so the operator-visible state lands.
        if call_count["n"] == 2:
            raise OSError("no space left on device")
        original_save(record)

    monkeypatch.setattr(ex, "_save_record", _flaky_save)

    await ex._process_next_for_chain(BASE)

    final = ex.get_record(BASE, 1900)
    assert final is not None
    assert final.status == CustodyTxStatus.AWAITING_CLEAR
    assert "persist failed" in (final.error or "")


# --- Canonical failure-mode drills (pytest -k drill) ---


@pytest.mark.asyncio
async def test_drill_sapphire_release_surplus_delta_matches_max_minus_actual(
    executor, web3s, accounting
):
    """Drill: Sapphire native release under the reserve-vs-actual gas accounting.

    The user-signed transaction reserves ``max_gas_cost`` up front; the receipt
    reports the actual cost (``gas_used * effective_gas_price``). When actual
    <= reserved, ``surplus_delta`` captures the unspent reserve so the custody
    balance delta reconciles against the ledger debit.
    """
    web3s[SAPPHIRE].eth._gas_price_wei = 10_000_000
    max_gas_cost = 1_000_000_000_000
    actual_gas_cost = 21000 * 10_000_000
    expected_surplus = max_gas_cost - actual_gas_cost

    broadcast_hash = HexBytes("0x" + "5a" * 32)
    web3s[SAPPHIRE].eth.send_raw_transaction.side_effect = [broadcast_hash]
    web3s[SAPPHIRE].eth.get_transaction_count = AsyncMock(return_value=3000)

    async def _receipt(_h):
        return _make_receipt(
            status=1,
            gas_used=21000,
            effective_gas_price=10_000_000,
        )

    web3s[SAPPHIRE].eth.get_transaction_receipt.side_effect = _receipt

    await executor.enqueue(
        CustodyTxRequest(
            chain_id=SAPPHIRE,
            evm_nonce=3000,
            kind=CustodyTxKind.SAPPHIRE_RELEASE,
            id="42",
            signed_tx=b"",
            route_address=None,
            max_gas_cost=max_gas_cost,
            withdrawal_index=42,
            to_address="0x" + "34" * 20,
            amount=10**18,
        )
    )
    await executor._process_next_for_chain(SAPPHIRE)

    rec = executor.get_record(SAPPHIRE, 3000)
    assert rec.status == CustodyTxStatus.SUCCESS
    assert rec.gas_used == 21000
    assert rec.effective_gas_price == 10_000_000
    assert rec.surplus_delta == expected_surplus
    assert rec.tx_hash == broadcast_hash.to_0x_hex()
    assert executor.sapphire_release_surplus() == expected_surplus
    accounting.resolve_bridge_withdrawal.assert_awaited_once_with(42)
