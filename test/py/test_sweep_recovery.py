import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from _helpers import AwaitableValue
from web3.exceptions import TransactionNotFound

import src.services.sweep_engine as sweep_engine_module
from src.clients.rofl import TransactionRevertedError
from src.services.accounting_contract import BridgeBurnReservation
from src.services.custody_tx_executor import (
    CustodyTxKind,
    CustodyTxRecord,
    CustodyTxStatus,
)
from src.services.sweep_engine import (
    BASE_SEPOLIA_CHAIN_ID,
    FLOW_NATIVE_ROSE_BRIDGE_IN,
    FLOW_STANDARD,
    FLOW_XROSE_BRIDGE_IN,
    Reconstruction,
    ReconstructionEvidenceError,
    ReconstructionKind,
    SweepEngine,
    SweepRecord,
    SweepRecoveryStuckError,
    SweepState,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path)


def _write_record(state_dir, record):
    """Helper to persist a SweepRecord to the state directory."""
    path = Path(state_dir) / f"sweep_{record.deposit_address.lower()}_{record.chain_id}.json"
    path.write_text(json.dumps(record.to_dict()))


def test_schema_standard_sweep_record_defaults_on_load(state_dir):
    """Standard records should load with bridge-flow defaults."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    loaded = engine.get_sweep_record("0x" + "aa" * 20, 84532)

    assert loaded is not None
    assert loaded.flow_type == FLOW_STANDARD
    assert loaded.destination is None
    assert loaded.bridge_address is None
    assert loaded.burn_tx_hash is None
    assert loaded.burn_reserved is False


def test_schema_xrose_bridge_record_persists_burn_pending(state_dir):
    """Bridge sweep records should persist burn metadata and state."""
    destination = "0x" + "cc" * 20
    bridge_address = "0x" + "dd" * 20
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.BURN_PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        flow_type="xrose_bridge_in",
        destination=destination,
        bridge_address=bridge_address,
        burn_reserved=True,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    loaded = engine.get_sweep_record("0x" + "aa" * 20, 84532)

    assert loaded is not None
    assert loaded.state == SweepState.BURN_PENDING
    assert loaded.flow_type == "xrose_bridge_in"
    assert loaded.destination == destination
    assert loaded.bridge_address == bridge_address
    assert loaded.burn_reserved is True


def test_schema_old_format_record_migrates_to_standard(state_dir):
    """Old JSON records without bridge-flow fields should load as standard sweeps."""
    deposit_address = "0x" + "aa" * 20
    path = Path(state_dir) / f"sweep_{deposit_address.lower()}_84532.json"
    path.write_text(
        json.dumps(
            {
                "deposit_address": deposit_address,
                "chain_id": 84532,
                "state": "pending",
                "beneficiary": "0x" + "bb" * 20,
                "chain_type": "evm",
                "version": 0,
            }
        )
    )

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    loaded = engine.get_sweep_record(deposit_address, 84532)

    assert loaded is not None
    assert loaded.flow_type == FLOW_STANDARD
    assert loaded.destination is None
    assert loaded.bridge_address is None
    assert loaded.burn_tx_hash is None
    assert loaded.burn_reserved is False


def test_schema_manual_review_reachable_from_burn_pending(state_dir):
    """Bridge burn failures should be able to move into terminal manual review."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.BURN_PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        flow_type="xrose_bridge_in",
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    loaded = engine.get_sweep_record("0x" + "aa" * 20, 84532)

    assert loaded is not None
    engine._mark_manual_review(loaded, "burn reverted")

    persisted = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW
    assert persisted.error == "burn reverted"


def test_load_incomplete_sweeps(state_dir):
    """Startup should find incomplete sweep records with recovery metadata."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        gas_funding_tx_hash="0x" + "ff" * 32,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    incomplete = engine.load_incomplete_sweeps()
    assert len(incomplete) == 1
    assert incomplete[0].state == SweepState.GAS_FUNDED
    assert incomplete[0].gas_funding_tx_hash == "0x" + "ff" * 32
    # Recovery metadata must be populated
    assert incomplete[0].amount == 10**18
    assert incomplete[0].token_id_hex == "0x" + "11" * 32
    assert incomplete[0].deposit_id_hex == "0x" + "22" * 32


@pytest.mark.asyncio
async def test_resume_swept_record_credits_and_cleans_up(state_dir):
    """A record in SWEPT state should retry creditDeposit and clean up on success."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEPT,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    await engine.resume_incomplete_sweeps()

    # Credit should have been called with the persisted metadata
    mock_accounting.credit_deposit.assert_called_once()
    call_kwargs = mock_accounting.credit_deposit.call_args
    assert call_kwargs[1]["beneficiary"] == "0x" + "bb" * 20
    assert call_kwargs[1]["amount"] == 10**18

    # Record should be cleaned up after successful credit
    assert engine.get_sweep_record("0x" + "aa" * 20, 84532) is None


def _seed_web3(engine: SweepEngine, chain_id: int, receipt_outcome) -> AsyncMock:
    """Inject a mocked AsyncWeb3 whose get_transaction_receipt behaves as specified.

    receipt_outcome is either a dict (returned), or an Exception class/instance (raised).
    """
    get_receipt = AsyncMock()
    if isinstance(receipt_outcome, dict):
        get_receipt.return_value = receipt_outcome
    else:
        get_receipt.side_effect = receipt_outcome

    w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=get_receipt))
    engine._web3_cache[chain_id] = w3
    return get_receipt


@pytest.mark.asyncio
async def test_resume_gas_funded_with_mined_sweep_tx_is_promoted_and_credited(state_dir):
    """Crash window regression: GAS_FUNDED + mined sweep_tx_hash must credit on resume.

    Without reconciliation, a crash between sweep broadcast and the SWEPT state flip
    leaves funds swept on-chain but permanently uncredited.
    """
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        gas_funding_tx_hash="0x" + "ff" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, {"status": 1})

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_called_once()
    assert engine.get_sweep_record("0x" + "aa" * 20, 84532) is None


@pytest.mark.asyncio
async def test_resume_gas_funded_with_unmined_sweep_tx_is_left_alone(state_dir):
    """If the sweep tx is not yet mined, the record stays GAS_FUNDED for the next pass."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, TransactionNotFound("not mined"))

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert persisted is not None
    assert persisted.state == SweepState.GAS_FUNDED


@pytest.mark.asyncio
async def test_resume_gas_funded_with_reverted_sweep_tx_not_promoted(state_dir):
    """A reverted sweep tx must NOT be promoted to SWEPT — the funds never left."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock()

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, {"status": 0})

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert persisted is not None
    assert persisted.state == SweepState.GAS_FUNDED


@pytest.mark.asyncio
async def test_resume_pending_without_sweep_tx_reruns_sweep(state_dir):
    """PENDING records without a sweep_tx_hash are safe to re-sweep — no nonce risk."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=None,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native = AsyncMock()
    engine.sweep_erc20 = AsyncMock()

    await engine.resume_incomplete_sweeps()

    engine.sweep_native.assert_called_once()
    call_kwargs = engine.sweep_native.call_args.kwargs
    assert call_kwargs["deposit_address"] == "0x" + "aa" * 20
    assert call_kwargs["beneficiary"] == "0x" + "bb" * 20
    assert call_kwargs["amount"] == 10**18
    engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_resume_gas_funded_without_sweep_tx_reruns_sweep(state_dir):
    """GAS_FUNDED with no broadcast sweep tx re-runs — sweep_native is idempotent."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        gas_funding_tx_hash="0x" + "ff" * 32,
        # sweep_tx_hash intentionally left None
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native = AsyncMock()

    await engine.resume_incomplete_sweeps()

    engine.sweep_native.assert_called_once()


@pytest.mark.asyncio
async def test_resume_pending_erc20_routes_to_sweep_erc20(state_dir):
    """PENDING ERC20 record recovery dispatches to sweep_erc20 with token_address."""
    token_addr = "0x" + "cc" * 20
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=50 * 10**6,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=token_addr,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native = AsyncMock()
    engine.sweep_erc20 = AsyncMock()

    await engine.resume_incomplete_sweeps()

    engine.sweep_erc20.assert_called_once()
    assert engine.sweep_erc20.call_args.kwargs["token_address"] == token_addr
    engine.sweep_native.assert_not_called()


@pytest.mark.asyncio
async def test_resume_swept_record_already_processed(state_dir):
    """If credit reverts with DepositAlreadyProcessed, record is still cleaned up."""
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.SWEPT,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.credit_deposit = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed",
            error_name="DepositAlreadyProcessed",
        )
    )

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    await engine.resume_incomplete_sweeps()

    # Record still cleaned up — DepositAlreadyProcessed means credit already happened
    assert engine.get_sweep_record("0x" + "aa" * 20, 84532) is None


@pytest.mark.asyncio
async def test_recovery_loop_resumes_pending_record(state_dir, monkeypatch):
    """The periodic recovery loop — not just startup — must resume PENDING records.

    This is the path that matters for long-running instances: a crash two hours
    into uptime shouldn't need a full restart to pick up a stuck PENDING sweep.
    """
    monkeypatch.setattr(sweep_engine_module, "SWEEP_RECOVERY_INTERVAL", 0.01)

    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=None,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native = AsyncMock()
    engine.sweep_erc20 = AsyncMock()

    engine.start_recovery_loop()
    try:
        for _ in range(100):  # up to 1s
            if engine.sweep_native.called:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Recovery loop did not call sweep_native within 1s")
    finally:
        await engine.stop_recovery_loop()

    assert engine.sweep_native.called
    engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_loop_persists_error_for_pending_record(state_dir, monkeypatch):
    """Persistent resume failures must set record.error so user retries can cleanup."""
    monkeypatch.setattr(sweep_engine_module, "SWEEP_RECOVERY_INTERVAL", 0.01)

    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        token_address=None,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native = AsyncMock(side_effect=RuntimeError("gas tank empty"))

    engine.start_recovery_loop()
    try:
        for _ in range(100):
            if engine.sweep_native.called:
                break
            await asyncio.sleep(0.01)
    finally:
        await engine.stop_recovery_loop()

    persisted = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert persisted is not None
    assert persisted.error == "gas tank empty"


@pytest.mark.asyncio
async def test_resume_does_not_overwrite_sweep_tx_hash_on_error(state_dir):
    """If _resume_sweep_from_pending raises AFTER a sweep tx was broadcast, the
    error flag must NOT be set — doing so lets user retries call cleanup_record
    and orphan the pending tx.
    """
    record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        sweep_tx_hash="0x" + "dd" * 32,
        gas_funding_tx_hash="0x" + "ff" * 32,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )

    engine._persist_resume_error(record, RuntimeError("boom"))

    persisted = engine.get_sweep_record("0x" + "aa" * 20, 84532)
    assert persisted is not None
    assert persisted.error is None, "error must not overwrite a record that has sweep_tx_hash"
    assert persisted.sweep_tx_hash == "0x" + "dd" * 32


# ----------------------------------------------------------------------
# Per-cell recovery decision-table tests
# ----------------------------------------------------------------------

DEP_ADDR = "0x" + "aa" * 20
BENEFICIARY = "0x" + "bb" * 20
TOKEN_ID_HEX = "0x" + "11" * 32
DEPOSIT_ID_HEX = "0x" + "22" * 32
DEPOSIT_ID_BYTES = b"\x22" * 32
SWEEP_TX = "0x" + "dd" * 32
ROFL_BRIDGE = "0x" + "ee" * 20


def _xrose_record(state: SweepState, **overrides) -> SweepRecord:
    base = dict(
        deposit_address=DEP_ADDR,
        chain_id=BASE_SEPOLIA_CHAIN_ID,
        state=state,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        token_address="0x" + "cc" * 20,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_XROSE_BRIDGE_IN,
        bridge_address=ROFL_BRIDGE,
        sweep_tx_hash=SWEEP_TX,
    )
    base.update(overrides)
    return SweepRecord(**base)


def _xrose_executor_record(
    status: CustodyTxStatus,
    *,
    kind: CustodyTxKind = CustodyTxKind.XROSE_BURN,
    record_id: str = DEPOSIT_ID_HEX,
    tx_hash: str | None = "0x" + "77" * 32,
    error: str | None = None,
) -> CustodyTxRecord:
    return CustodyTxRecord(
        chain_id=BASE_SEPOLIA_CHAIN_ID,
        accounting_contract_address="0x" + "99" * 20,
        evm_sender="0x" + "88" * 20,
        evm_nonce=42,
        kind=kind,
        id=record_id,
        signed_tx_hex="0x" + "ab" * 64,
        tx_hash=tx_hash,
        status=status,
        error=error,
    )


def _xrose_engine(state_dir, mock_accounting, mock_executor) -> SweepEngine:
    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={BASE_SEPOLIA_CHAIN_ID: "https://fake"},
        state_dir=state_dir,
        executor=mock_executor,
    )
    # Disable receipt reconciliation by short-circuiting _reconcile_sweep_tx:
    # the per-cell tests do not stage a real Base RPC.
    engine._reconcile_sweep_tx = AsyncMock()  # type: ignore[assignment]
    return engine


@pytest.mark.asyncio
async def test_recovery_swept_native_rose_retries_credit_with_persisted_net_amount(state_dir):
    """SWEPT + native_rose_bridge_in: retry credit with the *net* amount stored on the record."""
    net_amount = 9 * 10**17  # less than gross because gas was deducted at sweep time
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.SWEPT,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=net_amount,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_called_once()
    assert mock_accounting.credit_deposit.call_args.kwargs["amount"] == net_amount
    assert engine.get_sweep_record(DEP_ADDR, 23295) is None


@pytest.mark.asyncio
async def test_recovery_swept_xrose_no_reservation_reserves_and_enqueues_no_credit(state_dir):
    """SWEPT + xrose + burn_reserved=False: reserve, enqueue, do NOT credit."""
    record = _xrose_record(SweepState.SWEPT, burn_reserved=False)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.reserve_bridge_burn = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)
    mock_accounting.generate_bridge_burn_transfer = AsyncMock(return_value=b"\x99" * 100)

    mock_executor = MagicMock()
    mock_executor.enqueue = AsyncMock(return_value="84532_42")
    mock_executor.get_record = MagicMock(return_value=None)

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.reserve_bridge_burn.assert_awaited_once()
    mock_executor.enqueue.assert_awaited_once()
    enqueued = mock_executor.enqueue.await_args.args[0]
    assert enqueued.kind is CustodyTxKind.XROSE_BURN
    assert enqueued.chain_id == BASE_SEPOLIA_CHAIN_ID
    assert enqueued.evm_nonce == 42
    mock_accounting.credit_deposit.assert_not_called()

    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.BURN_PENDING
    assert persisted.burn_reserved is True


@pytest.mark.asyncio
async def test_recovery_swept_xrose_with_reservation_re_enqueues_no_credit(state_dir):
    """SWEPT + xrose + burn_reserved=True: reserve is idempotent, executor re-enqueued, no credit."""
    record = _xrose_record(SweepState.SWEPT, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.reserve_bridge_burn = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)
    mock_accounting.generate_bridge_burn_transfer = AsyncMock(return_value=b"\x99" * 100)

    mock_executor = MagicMock()
    mock_executor.enqueue = AsyncMock(return_value="84532_42")
    mock_executor.get_record = MagicMock(return_value=None)

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    # Already-reserved: reserve_bridge_burn must NOT be re-issued
    mock_accounting.reserve_bridge_burn.assert_not_awaited()
    mock_executor.enqueue.assert_awaited_once()
    mock_accounting.credit_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_burn_pending_xrose_executor_success_credits_and_deletes(state_dir):
    """BURN_PENDING + executor SUCCESS: credit and delete record."""
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)

    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(
        return_value=_xrose_executor_record(CustodyTxStatus.SUCCESS)
    )

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_awaited_once()
    assert mock_accounting.credit_deposit.await_args.kwargs["amount"] == 10**18
    assert engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID) is None


@pytest.mark.asyncio
async def test_recovery_burn_pending_xrose_executor_awaiting_clear_promotes_sweep(state_dir):
    """BURN_PENDING + executor AWAITING_CLEAR: sweep record promoted to MANUAL_REVIEW, no credit."""
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)

    executor_record = _xrose_executor_record(
        CustodyTxStatus.AWAITING_CLEAR, error="burn reverted on-chain"
    )
    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(return_value=executor_record)

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW
    assert "awaiting_clear" in (persisted.error or "")
    assert "burn reverted on-chain" in (persisted.error or "")


@pytest.mark.asyncio
async def test_recovery_burn_pending_xrose_executor_awaiting_clear_gas_cap_promotes_sweep(
    state_dir,
):
    """BURN_PENDING + executor AWAITING_CLEAR_GAS_CAP: sweep record promoted, no credit."""
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)

    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(
        return_value=_xrose_executor_record(
            CustodyTxStatus.AWAITING_CLEAR_GAS_CAP, error="hit gas cap"
        )
    )

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW


@pytest.mark.asyncio
async def test_recovery_burn_pending_xrose_executor_kind_mismatch_promotes_manual_review(state_dir):
    """BURN_PENDING + executor SUCCESS with kind mismatch: no credit, promote MANUAL_REVIEW.

    Kind mismatch at the reserved nonce means the executor record belongs to a
    different deposit — durable corruption that cannot resolve itself. Promoting
    avoids recurring critical-log spam every recovery tick.
    """
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)

    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(
        return_value=_xrose_executor_record(
            CustodyTxStatus.SUCCESS, kind=CustodyTxKind.NORMAL_WITHDRAWAL
        )
    )

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW
    assert persisted.error is not None
    assert "normal_withdrawal" in persisted.error


@pytest.mark.asyncio
async def test_recovery_burn_pending_xrose_executor_waiting_for_gas_cap_left_alone(state_dir):
    """BURN_PENDING + executor WAITING_FOR_GAS_CAP: log only, sweep record unchanged."""
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)

    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(
        return_value=_xrose_executor_record(CustodyTxStatus.WAITING_FOR_GAS_CAP)
    )

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.BURN_PENDING


@pytest.mark.asyncio
async def test_recovery_burned_xrose_retries_credit(state_dir):
    """BURNED + xrose: retry credit, then delete record."""
    record = _xrose_record(SweepState.BURNED, burn_reserved=True, burn_tx_hash="0x" + "77" * 32)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_executor = MagicMock()

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_awaited_once()
    assert engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID) is None


@pytest.mark.asyncio
async def test_recovery_manual_review_xrose_is_left_alone(state_dir):
    """MANUAL_REVIEW + xrose: no executor reads, no credit, no state change."""
    record = _xrose_record(SweepState.MANUAL_REVIEW, burn_reserved=True, error="prior failure")
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(return_value=None)

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    mock_executor.get_record.assert_not_called()
    mock_accounting.get_bridge_burn_nonce.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW
    assert persisted.error == "prior failure"


@pytest.mark.asyncio
async def test_recovery_manual_review_standard_is_left_alone(state_dir):
    """MANUAL_REVIEW + standard: dispatcher parity — no credit, no resume."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=84532,
        state=SweepState.MANUAL_REVIEW,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_STANDARD,
        error="prior failure",
    )
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native = AsyncMock()
    engine.sweep_erc20 = AsyncMock()

    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    engine.sweep_native.assert_not_called()
    engine.sweep_erc20.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, 84532)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW


# ----------------------------------------------------------------------
# xrose_bridge_in_in_flight() predicate tests
# ----------------------------------------------------------------------


def _reservation(
    deposit_id: bytes = DEPOSIT_ID_BYTES,
    chain_id: int = BASE_SEPOLIA_CHAIN_ID,
    bridge: str = ROFL_BRIDGE,
    amount: int = 10**18,
    nonce: int = 42,
) -> BridgeBurnReservation:
    return BridgeBurnReservation(
        deposit_id=deposit_id,
        chain_id=chain_id,
        bridge=bridge,  # type: ignore[arg-type]
        amount=amount,
        nonce=nonce,
    )


def _predicate_engine(
    state_dir,
    *,
    reservations,
    credited: bool = False,
    burned: bool = False,
    reservation_exc: Exception | None = None,
    credited_exc: Exception | None = None,
    burned_exc: Exception | None = None,
) -> SweepEngine:
    mock_accounting = AsyncMock()
    if reservation_exc is not None:
        mock_accounting.list_bridge_burn_reservations = AsyncMock(side_effect=reservation_exc)
    else:
        mock_accounting.list_bridge_burn_reservations = AsyncMock(return_value=reservations)
    if credited_exc is not None:
        mock_accounting.is_deposit_processed = AsyncMock(side_effect=credited_exc)
    else:
        mock_accounting.is_deposit_processed = AsyncMock(return_value=credited)

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={BASE_SEPOLIA_CHAIN_ID: "https://fake"},
        state_dir=state_dir,
    )

    async def _read_burned(_chain_id, _bridge, _deposit_id):
        if burned_exc is not None:
            raise burned_exc
        return burned

    engine._read_rofl_bridge_burned = _read_burned  # type: ignore[assignment]
    return engine


@pytest.mark.asyncio
async def test_in_flight_returns_false_when_no_records_and_no_reservations(state_dir):
    engine = _predicate_engine(state_dir, reservations=[])
    assert await engine.xrose_bridge_in_in_flight() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        SweepState.PENDING,
        SweepState.GAS_FUNDED,
        SweepState.SWEPT,
        SweepState.BURN_PENDING,
        SweepState.BURNED,
    ],
)
async def test_in_flight_returns_true_for_active_local_xrose_record(state_dir, state):
    _write_record(state_dir, _xrose_record(state, burn_reserved=(state != SweepState.PENDING)))
    engine = _predicate_engine(state_dir, reservations=[])
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_returns_false_when_local_manual_review_only_and_credit_consistent(
    state_dir,
):
    _write_record(state_dir, _xrose_record(SweepState.MANUAL_REVIEW, burn_reserved=True))
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burned=False,
    )
    # Even though credited=false, the MANUAL_REVIEW marker means operator owns the deposit
    assert await engine.xrose_bridge_in_in_flight() is False


@pytest.mark.asyncio
async def test_in_flight_returns_true_when_reserved_but_not_credited_and_not_burned(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burned=False,
    )
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_returns_true_when_reserved_burned_but_not_credited(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burned=True,
    )
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_returns_false_when_reserved_burned_and_credited(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited=True,
        burned=True,
    )
    assert await engine.xrose_bridge_in_in_flight() is False


@pytest.mark.asyncio
async def test_in_flight_returns_true_when_credited_but_not_burned_contradiction(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited=True,
        burned=False,
    )
    # Spec: "fail closed if state is missing or contradictory"
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_returns_true_when_reservation_scan_raises(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[],
        reservation_exc=RuntimeError("Sapphire RPC down"),
    )
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_returns_true_when_processed_deposits_read_raises(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited_exc=RuntimeError("processedDeposits revert"),
    )
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_returns_true_when_rofl_bridge_burned_read_raises(state_dir):
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        burned_exc=RuntimeError("Base RPC down"),
    )
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_skips_non_xrose_records(state_dir):
    # Standard record at PENDING — would be "in flight" by state but wrong flow_type
    _write_record(
        state_dir,
        SweepRecord(
            deposit_address=DEP_ADDR,
            chain_id=84532,
            state=SweepState.SWEPT,
            beneficiary=BENEFICIARY,
            chain_type="evm",
            version=0,
            amount=10**18,
            token_id_hex=TOKEN_ID_HEX,
            deposit_id_hex=DEPOSIT_ID_HEX,
            flow_type=FLOW_STANDARD,
            sweep_tx_hash=SWEEP_TX,
        ),
    )
    engine = _predicate_engine(state_dir, reservations=[])
    assert await engine.xrose_bridge_in_in_flight() is False


# ----------------------------------------------------------------------
# Native-rose routing, stuck-record signaling, predicate fail-closed on corrupt records
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_native_rose_pending_calls_native_rose_bridge_not_sweep_native(state_dir):
    """Pre-SWEPT native_rose recovery must re-enter sweep_native_rose_bridge.

    Routing to the generic sweep_native path would gas-tank-fund the deposit
    address and credit the gross verified amount — both violate native ROSE
    bridge invariants (deposit pays own gas, credit at net custody delta).
    """
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )
    engine.sweep_native_rose_bridge = AsyncMock()  # type: ignore[assignment]
    engine.sweep_native = AsyncMock()  # type: ignore[assignment]
    engine.sweep_erc20 = AsyncMock()  # type: ignore[assignment]

    await engine.resume_incomplete_sweeps()

    engine.sweep_native_rose_bridge.assert_awaited_once()
    engine.sweep_native.assert_not_called()
    engine.sweep_erc20.assert_not_called()
    kwargs = engine.sweep_native_rose_bridge.call_args.kwargs
    assert kwargs["deposit_address"] == DEP_ADDR
    assert kwargs["amount"] == 10**18


@pytest.mark.asyncio
async def test_resume_native_rose_stuck_raises_recovery_stuck_error(state_dir):
    """Pre-SWEPT native_rose with sweep_tx_hash set must signal stuck via exception."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 23295, TransactionNotFound("not mined"))

    with pytest.raises(SweepRecoveryStuckError) as exc_info:
        await engine._resume_one_record(engine.load_incomplete_sweeps()[0])

    assert exc_info.value.flow_type == FLOW_NATIVE_ROSE_BRIDGE_IN
    assert exc_info.value.sweep_tx_hash == SWEEP_TX


@pytest.mark.asyncio
async def test_resume_standard_stuck_raises_recovery_stuck_error(state_dir):
    """Pre-SWEPT standard with sweep_tx_hash set must signal stuck via exception."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_STANDARD,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, TransactionNotFound("not mined"))

    with pytest.raises(SweepRecoveryStuckError):
        await engine._resume_one_record(engine.load_incomplete_sweeps()[0])


@pytest.mark.asyncio
async def test_resume_incomplete_sweeps_counts_stuck_as_failed(state_dir, caplog):
    """Outer loop must classify a stuck record as failed (not 'Recovered sweep')."""
    import logging

    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_STANDARD,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, TransactionNotFound("not mined"))

    with caplog.at_level(logging.WARNING, logger="src.services.sweep_engine"):
        await engine.resume_incomplete_sweeps()

    assert not any("Recovered sweep" in r.message for r in caplog.records)
    assert any("uncredited deposits need attention" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_reconcile_native_rose_promotion_refines_amount_to_tx_value(state_dir):
    """Crash-window SWEPT promotion for native_rose must overwrite record.amount with tx.value.

    Without this, recovery credits the verified gross instead of the net value
    actually transferred to custody.
    """
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,  # gross
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )

    net_value = 9 * 10**17
    get_receipt = AsyncMock(return_value={"status": 1})
    get_tx = AsyncMock(return_value={"value": net_value})
    w3 = SimpleNamespace(
        eth=SimpleNamespace(get_transaction_receipt=get_receipt, get_transaction=get_tx)
    )
    engine._web3_cache[23295] = w3

    loaded = engine.load_incomplete_sweeps()[0]
    await engine._reconcile_sweep_tx(loaded)

    persisted = engine.get_sweep_record(DEP_ADDR, 23295)
    assert persisted is not None
    assert persisted.state == SweepState.SWEPT
    assert persisted.amount == net_value


@pytest.mark.asyncio
async def test_in_flight_fails_closed_on_corrupt_record(state_dir):
    """A .corrupt sweep record in the state dir must force the predicate to True."""
    corrupt_path = Path(state_dir) / "sweep_0xdead_84532.corrupt"
    corrupt_path.write_text("{ not valid json")

    engine = _predicate_engine(state_dir, reservations=[])
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_reconcile_native_rose_refuses_promotion_on_non_positive_tx_value(state_dir):
    """tx.value <= 0 must keep the record at its current state — never SWEPT."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash="0x" + "ee" * 32,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )
    w3 = MagicMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1})
    w3.eth.get_transaction = AsyncMock(return_value={"value": 0})
    engine._web3_cache[23295] = w3

    await engine._reconcile_sweep_tx(engine.load_incomplete_sweeps()[0])

    persisted = engine.get_sweep_record(DEP_ADDR, 23295)
    assert persisted is not None
    assert persisted.state == SweepState.GAS_FUNDED
    assert persisted.amount == 10**18


@pytest.mark.asyncio
async def test_reconcile_native_rose_leaves_state_on_get_transaction_failure(state_dir):
    """Transient get_transaction RPC failure must NOT promote the record."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex="0x" + "11" * 32,
        deposit_id_hex="0x" + "22" * 32,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash="0x" + "ee" * 32,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )
    w3 = MagicMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 1})
    w3.eth.get_transaction = AsyncMock(side_effect=ConnectionError("rpc down"))
    engine._web3_cache[23295] = w3

    await engine._reconcile_sweep_tx(engine.load_incomplete_sweeps()[0])

    persisted = engine.get_sweep_record(DEP_ADDR, 23295)
    assert persisted is not None
    assert persisted.state == SweepState.GAS_FUNDED


@pytest.mark.asyncio
async def test_recovery_burn_pending_xrose_manual_review_kind_mismatch_promotes_manual_review(
    state_dir,
):
    """MANUAL_REVIEW executor record with kind mismatch must promote, not log-spam."""
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(return_value=42)

    mock_executor = MagicMock()
    mock_executor.get_record = MagicMock(
        return_value=_xrose_executor_record(
            CustodyTxStatus.AWAITING_CLEAR, kind=CustodyTxKind.NORMAL_WITHDRAWAL
        )
    )

    engine = _xrose_engine(state_dir, mock_accounting, mock_executor)
    await engine.resume_incomplete_sweeps()

    mock_accounting.credit_deposit.assert_not_called()
    persisted = engine.get_sweep_record(DEP_ADDR, BASE_SEPOLIA_CHAIN_ID)
    assert persisted is not None
    assert persisted.state == SweepState.MANUAL_REVIEW
    assert persisted.error is not None
    assert "normal_withdrawal" in persisted.error


@pytest.mark.asyncio
async def test_recovery_xrose_pre_swept_with_sweep_tx_raises_stuck_error(state_dir):
    """xROSE PENDING/GAS_FUNDED with a broadcast sweep_tx must raise stuck error."""
    record = _xrose_record(
        SweepState.GAS_FUNDED,
        burn_reserved=False,
        sweep_tx_hash="0x" + "ee" * 32,
    )
    _write_record(state_dir, record)

    engine = _xrose_engine(state_dir, AsyncMock(), MagicMock())

    with pytest.raises(SweepRecoveryStuckError) as exc_info:
        await engine._resume_one_record(engine.load_incomplete_sweeps()[0])

    assert exc_info.value.flow_type == FLOW_XROSE_BRIDGE_IN
    assert exc_info.value.sweep_tx_hash == "0x" + "ee" * 32


@pytest.mark.asyncio
async def test_in_flight_fails_closed_on_contradiction_for_manual_review_id(state_dir):
    """A MANUAL_REVIEW local record cannot mask a Sapphire/Base contradiction.

    Divergence between Sapphire credit and Base burn is a hard bridge invariant
    break regardless of operator status, so the predicate must still fail closed.
    """
    _write_record(state_dir, _xrose_record(SweepState.MANUAL_REVIEW, burn_reserved=True))
    engine = _predicate_engine(
        state_dir,
        reservations=[_reservation()],
        credited=True,
        burned=False,
    )
    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_skips_manual_review_id_only_when_contradiction_absent(state_dir):
    """One MANUAL_REVIEW reservation is skipped, but a second unsettled reservation flips True.

    Verifies the MANUAL_REVIEW skip is per-deposit_id, not global.
    """
    manual_review_id = bytes.fromhex("aa" * 32)
    other_id = bytes.fromhex("bb" * 32)
    _write_record(
        state_dir,
        _xrose_record(
            SweepState.MANUAL_REVIEW,
            burn_reserved=True,
            deposit_id_hex="0x" + manual_review_id.hex(),
        ),
    )

    engine = _predicate_engine(
        state_dir,
        reservations=[
            _reservation(deposit_id=manual_review_id, nonce=1),
            _reservation(deposit_id=other_id, nonce=2),
        ],
    )

    async def _is_processed(deposit_id: bytes) -> bool:
        return deposit_id == manual_review_id

    engine._accounting.is_deposit_processed = AsyncMock(side_effect=_is_processed)
    engine._read_rofl_bridge_burned = AsyncMock(  # type: ignore[assignment]
        side_effect=lambda _c, _b, did: did == manual_review_id
    )

    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_in_flight_fails_closed_on_state_dir_glob_oserror(state_dir):
    """OSError during the .corrupt glob must fail closed, not propagate."""
    engine = _predicate_engine(state_dir, reservations=[])

    fake_dir = MagicMock()
    fake_dir.glob.side_effect = OSError("filesystem unmounted")
    engine._state_dir = fake_dir

    assert await engine.xrose_bridge_in_in_flight() is True


@pytest.mark.asyncio
async def test_recovery_xrose_burn_pending_propagates_non_value_error_from_nonce_lookup(
    state_dir,
):
    """get_bridge_burn_nonce() raising a non-ValueError must NOT be swallowed.

    A bare ``except Exception`` here would mask Sapphire RPC outages behind the
    "event not yet visible" log — operators must see the real failure.
    """
    record = _xrose_record(SweepState.BURN_PENDING, burn_reserved=True)
    _write_record(state_dir, record)

    mock_accounting = AsyncMock()
    mock_accounting.get_bridge_burn_nonce = AsyncMock(side_effect=ConnectionError("sapphire down"))

    engine = _xrose_engine(state_dir, mock_accounting, MagicMock())

    with pytest.raises(ConnectionError):
        await engine._resume_one_record(engine.load_incomplete_sweeps()[0])


@pytest.mark.asyncio
async def test_resume_does_not_log_recovered_for_manual_review_record(state_dir, caplog):
    """MANUAL_REVIEW records must be reported as operator-owned, not as recovered."""
    _write_record(
        state_dir, _xrose_record(SweepState.MANUAL_REVIEW, burn_reserved=True, error="prior")
    )

    engine = _xrose_engine(state_dir, AsyncMock(), MagicMock())

    with caplog.at_level("INFO", logger="src.services.sweep_engine"):
        await engine.resume_incomplete_sweeps()

    assert not any("Recovered sweep" in r.getMessage() for r in caplog.records)
    assert any("leaving for operator" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------
# reconstruct_xrose_deposit_state() helper tests
# ----------------------------------------------------------------------


def _reconstruct_engine(
    state_dir,
    *,
    reservations,
    credited: bool = False,
    burn_view: bool = False,
    burn_amount=None,
    reservation_exc: Exception | None = None,
    credited_exc: Exception | None = None,
    burn_view_exc: Exception | None = None,
    burn_event_exc: Exception | None = None,
    pinned_block: int = 12345,
) -> SweepEngine:
    mock_accounting = AsyncMock()
    if reservation_exc is not None:
        mock_accounting.list_bridge_burn_reservations = AsyncMock(side_effect=reservation_exc)
    else:
        mock_accounting.list_bridge_burn_reservations = AsyncMock(return_value=reservations)
    if credited_exc is not None:
        mock_accounting.is_deposit_processed = AsyncMock(side_effect=credited_exc)
    else:
        mock_accounting.is_deposit_processed = AsyncMock(return_value=credited)

    engine = SweepEngine(
        accounting_service=mock_accounting,
        chain_rpc_urls={BASE_SEPOLIA_CHAIN_ID: "https://fake"},
        state_dir=state_dir,
    )

    fake_w3 = MagicMock()
    fake_w3.eth.block_number = AwaitableValue(pinned_block)
    engine._get_web3 = MagicMock(return_value=fake_w3)  # type: ignore[assignment]

    async def _read_burned(_chain_id, _bridge, _deposit_id, *, block_identifier=None):
        if burn_view_exc is not None:
            raise burn_view_exc
        return burn_view

    async def _read_burned_event(_chain_id, _bridge, _deposit_id, *, to_block=None):
        if burn_event_exc is not None:
            raise burn_event_exc
        return burn_amount

    engine._read_rofl_bridge_burned = _read_burned  # type: ignore[assignment]
    engine._read_rofl_bridge_burned_event = _read_burned_event  # type: ignore[assignment]
    return engine


@pytest.mark.asyncio
async def test_reconstruct_unknown_no_local_no_chain(state_dir):
    """No local record + no on-chain evidence → UNKNOWN."""
    engine = _reconstruct_engine(state_dir, reservations=[])
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert isinstance(result, Reconstruction)
    assert result.kind == ReconstructionKind.UNKNOWN
    assert result.deposit_id == DEPOSIT_ID_BYTES
    assert result.reservation is None
    assert result.burn_amount is None
    assert result.credited is False
    assert result.burn_view is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [SweepState.PENDING, SweepState.GAS_FUNDED, SweepState.SWEPT],
)
async def test_reconstruct_swept_only_local_record_no_reservation(state_dir, state):
    """Local xROSE record in pre-burn-reservation state + no chain evidence → SWEPT_ONLY."""
    _write_record(state_dir, _xrose_record(state, burn_reserved=(state != SweepState.PENDING)))
    engine = _reconstruct_engine(state_dir, reservations=[])
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.SWEPT_ONLY
    assert result.reservation is None
    assert result.credited is False
    assert result.burn_view is False


@pytest.mark.asyncio
async def test_reconstruct_burn_reserved_not_mined(state_dir):
    """Reservation present, both views False, no Burned event → BURN_RESERVED_NOT_MINED."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burn_view=False,
        burn_amount=None,
    )
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.BURN_RESERVED_NOT_MINED
    assert result.reservation is not None
    assert result.reservation.deposit_id == DEPOSIT_ID_BYTES
    assert result.burn_amount is None
    assert result.credited is False
    assert result.burn_view is False


@pytest.mark.asyncio
async def test_reconstruct_burned(state_dir):
    """Reservation + burnedDepositIds=True + Burned event + processedDeposits=False → BURNED."""
    burn_amt = 7 * 10**17
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation(amount=burn_amt)],
        credited=False,
        burn_view=True,
        burn_amount=burn_amt,
    )
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.BURNED
    assert result.reservation is not None
    assert result.burn_amount == burn_amt
    assert result.credited is False
    assert result.burn_view is True


@pytest.mark.asyncio
async def test_reconstruct_credited(state_dir):
    """Reservation + processedDeposits=True + burnedDepositIds=True + Burned event → CREDITED."""
    burn_amt = 5 * 10**17
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation(amount=burn_amt)],
        credited=True,
        burn_view=True,
        burn_amount=burn_amt,
    )
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.CREDITED
    assert result.credited is True
    assert result.burn_view is True
    assert result.burn_amount == burn_amt


@pytest.mark.asyncio
async def test_reconstruct_raises_when_credited_but_burned_view_false(state_dir):
    """processedDeposits=True but burnedDepositIds=False → ReconstructionEvidenceError."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        credited=True,
        burn_view=False,
        burn_amount=None,
    )
    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert exc.value.deposit_id == DEPOSIT_ID_BYTES
    assert "credited" in exc.value.reason.lower()
    assert "burn" in exc.value.reason.lower()


@pytest.mark.asyncio
async def test_reconstruct_raises_when_credited_but_no_reservation(state_dir):
    """processedDeposits=True but no BridgeBurnReserved event → ReconstructionEvidenceError."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[],
        credited=True,
        burn_view=True,
        burn_amount=10**18,
    )
    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert "reservation" in exc.value.reason.lower() or "reserve" in exc.value.reason.lower()


@pytest.mark.asyncio
async def test_reconstruct_raises_when_burn_view_true_but_no_event(state_dir):
    """burnedDepositIds=True but Burned event missing → ReconstructionEvidenceError."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burn_view=True,
        burn_amount=None,
    )
    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert "event" in exc.value.reason.lower()


@pytest.mark.asyncio
async def test_reconstruct_raises_when_burn_event_present_but_view_false(state_dir):
    """Burned event present but burnedDepositIds=False → ReconstructionEvidenceError."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burn_view=False,
        burn_amount=10**18,
    )
    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert "event" in exc.value.reason.lower()


@pytest.mark.asyncio
async def test_reconstruct_propagates_reservation_scan_failure(state_dir):
    """RPC failure on the reservation scan propagates to the caller (fail closed at caller)."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[],
        reservation_exc=ConnectionError("sapphire down"),
    )
    with pytest.raises(ConnectionError):
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )


@pytest.mark.asyncio
async def test_reconstruct_propagates_processed_deposits_failure(state_dir):
    """RPC failure on processedDeposits propagates to the caller."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        credited_exc=ConnectionError("sapphire down"),
    )
    with pytest.raises(ConnectionError):
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_kw", ["burn_view_exc", "burn_event_exc"])
async def test_reconstruct_propagates_base_read_failure(state_dir, exc_kw):
    """RPC failure on either Base read propagates to the caller."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        **{exc_kw: ConnectionError("base down")},
    )
    with pytest.raises(ConnectionError):
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )


@pytest.mark.asyncio
async def test_reconstruct_rejects_non_32_byte_deposit_id(state_dir):
    """Boundary check: deposit_id must be exactly 32 bytes."""
    engine = _reconstruct_engine(state_dir, reservations=[])
    with pytest.raises(ValueError):
        await engine.reconstruct_xrose_deposit_state(b"\x00" * 31, rofl_bridge_address=ROFL_BRIDGE)


@pytest.mark.asyncio
async def test_reconstruct_ignores_non_xrose_local_record(state_dir):
    """A local SweepRecord with flow_type=standard must not classify as SWEPT_ONLY."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=BASE_SEPOLIA_CHAIN_ID,
        state=SweepState.SWEPT,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_STANDARD,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)
    engine = _reconstruct_engine(state_dir, reservations=[])
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.UNKNOWN


@pytest.mark.asyncio
async def test_reconstruct_uses_reservation_bridge_when_present(state_dir):
    """When a reservation exists, the helper reads Base views against the reservation's bridge,
    not the operator-supplied address. Preserves per-deposit binding under route rotation."""
    different_bridge = "0x" + "ff" * 20
    captured = {}

    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation(bridge=different_bridge)],
        credited=False,
        burn_view=False,
        burn_amount=None,
    )

    async def _read_burned(chain_id, bridge, _deposit_id, *, block_identifier=None):
        captured["view_bridge"] = bridge
        captured["view_block"] = block_identifier
        return False

    async def _read_burned_event(chain_id, bridge, _deposit_id, *, to_block=None):
        captured["event_bridge"] = bridge
        captured["event_block"] = to_block
        return None

    engine._read_rofl_bridge_burned = _read_burned  # type: ignore[assignment]
    engine._read_rofl_bridge_burned_event = _read_burned_event  # type: ignore[assignment]

    await engine.reconstruct_xrose_deposit_state(DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE)
    assert captured["view_bridge"] == different_bridge
    assert captured["event_bridge"] == different_bridge


@pytest.mark.asyncio
async def test_reconstruct_pins_both_base_reads_to_same_block(state_dir):
    """View + event reads must share a single pinned block_identifier so they
    cannot disagree across a burn landing between two RPC calls."""
    pinned = 99999
    captured = {}
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation()],
        credited=False,
        burn_view=False,
        burn_amount=None,
        pinned_block=pinned,
    )

    async def _read_burned(_chain_id, _bridge, _deposit_id, *, block_identifier=None):
        captured["view_block"] = block_identifier
        return False

    async def _read_burned_event(_chain_id, _bridge, _deposit_id, *, to_block=None):
        captured["event_block"] = to_block
        return None

    engine._read_rofl_bridge_burned = _read_burned  # type: ignore[assignment]
    engine._read_rofl_bridge_burned_event = _read_burned_event  # type: ignore[assignment]

    await engine.reconstruct_xrose_deposit_state(DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE)
    assert captured["view_block"] == pinned
    assert captured["event_block"] == pinned


@pytest.mark.asyncio
async def test_reconstruct_raises_when_reservation_amount_disagrees_with_burn_event(state_dir):
    """A reservation amount that disagrees with the Burned event is a hard
    bridge-invariant break — must raise rather than picking one side."""
    engine = _reconstruct_engine(
        state_dir,
        reservations=[_reservation(amount=10**18)],
        credited=False,
        burn_view=True,
        burn_amount=9 * 10**17,
    )
    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert "amount" in exc.value.reason.lower()
    assert str(10**18) in exc.value.reason
    assert str(9 * 10**17) in exc.value.reason


@pytest.mark.asyncio
async def test_reconstruct_raises_on_corrupt_state_file(state_dir):
    """A ``.corrupt`` quarantine file may hide an xROSE deposit; the helper
    must fail closed before any RPC read."""
    corrupt = Path(state_dir) / "sweep_0xaaaa_84532.corrupt"
    corrupt.write_text("{ broken json")

    engine = _reconstruct_engine(state_dir, reservations=[])
    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert "corrupt" in exc.value.reason.lower()
    engine._accounting.list_bridge_burn_reservations.assert_not_called()


@pytest.mark.asyncio
async def test_reconstruct_raises_when_state_dir_glob_fails(state_dir):
    """OSError from the corrupt-file glob is fail-closed as ReconstructionEvidenceError."""
    engine = _reconstruct_engine(state_dir, reservations=[])
    fake_dir = MagicMock()
    fake_dir.glob.side_effect = OSError("filesystem unmounted")
    engine._state_dir = fake_dir

    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine.reconstruct_xrose_deposit_state(
            DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
        )
    assert "state-dir glob failed" in exc.value.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [SweepState.BURN_PENDING, SweepState.BURNED])
async def test_reconstruct_returns_unknown_for_local_burn_side_record_without_chain_evidence(
    state_dir, state
):
    """A local BURN_PENDING / BURNED record on its own is not chain-side evidence."""
    _write_record(state_dir, _xrose_record(state, burn_reserved=True))
    engine = _reconstruct_engine(state_dir, reservations=[])
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.UNKNOWN
    assert result.reservation is None
    assert result.credited is False
    assert result.burn_view is False


@pytest.mark.asyncio
async def test_reconstruct_returns_unknown_for_local_manual_review_only(state_dir):
    """MANUAL_REVIEW means an operator owns the deposit; reconstruction
    reports the chain view (UNKNOWN here) and does not surface the local
    hand-off — preventing a caller from acting on the operator's behalf."""
    _write_record(state_dir, _xrose_record(SweepState.MANUAL_REVIEW, burn_reserved=True))
    engine = _reconstruct_engine(state_dir, reservations=[])
    result = await engine.reconstruct_xrose_deposit_state(
        DEPOSIT_ID_BYTES, rofl_bridge_address=ROFL_BRIDGE
    )
    assert result.kind == ReconstructionKind.UNKNOWN


@pytest.mark.asyncio
async def test_read_rofl_bridge_burned_event_raises_on_duplicate(state_dir):
    """Exercise the real ``_read_rofl_bridge_burned_event`` body: two matching
    Burned events for one depositId violate the single-burn invariant
    enforced by ``ROFLBridge.burn`` and must raise rather than silently
    pick one."""
    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={BASE_SEPOLIA_CHAIN_ID: "https://fake"},
        state_dir=state_dir,
    )

    fake_contract = MagicMock()
    fake_contract.events.Burned.get_logs = AsyncMock(
        return_value=[
            {"args": {"amount": 10**18, "depositId": DEPOSIT_ID_BYTES}},
            {"args": {"amount": 10**18, "depositId": DEPOSIT_ID_BYTES}},
        ]
    )
    fake_w3 = MagicMock()
    fake_w3.eth.contract = MagicMock(return_value=fake_contract)
    fake_w3.to_checksum_address = lambda a: a
    engine._get_web3 = MagicMock(return_value=fake_w3)  # type: ignore[assignment]

    with pytest.raises(ReconstructionEvidenceError) as exc:
        await engine._read_rofl_bridge_burned_event(
            BASE_SEPOLIA_CHAIN_ID, ROFL_BRIDGE, DEPOSIT_ID_BYTES
        )
    assert "2 Burned events" in exc.value.reason
    assert "single-burn" in exc.value.reason


@pytest.mark.asyncio
async def test_reconstruct_raises_when_no_reservation_and_no_bridge_address(state_dir):
    """No reservation observed AND no ``rofl_bridge_address`` supplied → ValueError.
    Without either source there is no Base contract address to query, so the
    helper cannot proceed and must surface that as a configuration error
    distinct from contradictory evidence."""
    engine = _reconstruct_engine(state_dir, reservations=[])
    with pytest.raises(ValueError) as exc:
        await engine.reconstruct_xrose_deposit_state(DEPOSIT_ID_BYTES)
    assert "rofl_bridge_address" in str(exc.value)


@pytest.mark.asyncio
async def test_reconcile_native_rose_revert_clears_tx_hash_and_updates_amount(state_dir):
    """Reverted native-rose sweep must re-read residual balance and unblock re-sweep.

    Without the recovery branch, `_resume_native_rose_record` raises
    `SweepRecoveryStuckError` because the record still carries the broadcast
    sweep_tx_hash. The reconcile pass must clear `sweep_tx_hash`, set
    `record.amount` to the current deposit-address balance, and persist — so
    the next resume cycle re-enters `sweep_native_rose_bridge` and succeeds
    against the new gas-limit values.
    """
    residual_balance = 47_900_000_000_000_000  # 0.0479 ROSE after a reverted 21k-burn

    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=50_000_000_000_000_000,  # original verified gross (0.05 ROSE)
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )
    w3 = MagicMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 0})
    w3.eth.get_balance = AsyncMock(return_value=residual_balance)
    engine._web3_cache[23295] = w3

    await engine._reconcile_sweep_tx(engine.load_incomplete_sweeps()[0])

    persisted = engine.get_sweep_record(DEP_ADDR, 23295)
    assert persisted is not None
    assert persisted.sweep_tx_hash is None
    assert persisted.amount == residual_balance
    assert persisted.state == SweepState.PENDING

    # The cleared sweep_tx_hash lets `_resume_native_rose_record` take the
    # no-tx-hash branch on the next pass; with the recovery patch, this no
    # longer raises `SweepRecoveryStuckError`.
    sweep_native_calls: list[dict] = []

    async def _capture(**kwargs):
        sweep_native_calls.append(kwargs)

    engine.sweep_native_rose_bridge = _capture  # type: ignore[assignment]
    await engine._resume_one_record(engine.load_incomplete_sweeps()[0])
    assert len(sweep_native_calls) == 1
    assert sweep_native_calls[0]["amount"] == residual_balance


@pytest.mark.asyncio
async def test_reconcile_native_rose_revert_refuses_advance_on_zero_balance(state_dir):
    """Zero residual after a reverted sweep is unexpected — refuse to clear the record."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=23295,
        state=SweepState.PENDING,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=50_000_000_000_000_000,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_NATIVE_ROSE_BRIDGE_IN,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={23295: "https://fake"},
        state_dir=state_dir,
    )
    w3 = MagicMock()
    w3.eth.get_transaction_receipt = AsyncMock(return_value={"status": 0})
    w3.eth.get_balance = AsyncMock(return_value=0)
    engine._web3_cache[23295] = w3

    await engine._reconcile_sweep_tx(engine.load_incomplete_sweeps()[0])

    persisted = engine.get_sweep_record(DEP_ADDR, 23295)
    assert persisted is not None
    assert persisted.sweep_tx_hash == SWEEP_TX
    assert persisted.amount == 50_000_000_000_000_000


@pytest.mark.asyncio
async def test_reconcile_standard_flow_revert_still_logs_only(state_dir):
    """The recovery branch is FLOW_NATIVE_ROSE_BRIDGE_IN-only; standard flows keep their record."""
    record = SweepRecord(
        deposit_address=DEP_ADDR,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary=BENEFICIARY,
        chain_type="evm",
        version=0,
        amount=10**18,
        token_id_hex=TOKEN_ID_HEX,
        deposit_id_hex=DEPOSIT_ID_HEX,
        flow_type=FLOW_STANDARD,
        sweep_tx_hash=SWEEP_TX,
    )
    _write_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    _seed_web3(engine, 84532, {"status": 0})

    await engine._reconcile_sweep_tx(engine.load_incomplete_sweeps()[0])

    persisted = engine.get_sweep_record(DEP_ADDR, 84532)
    assert persisted is not None
    assert persisted.sweep_tx_hash == SWEEP_TX
    assert persisted.amount == 10**18
    assert persisted.state == SweepState.GAS_FUNDED
