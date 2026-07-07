import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from web3.exceptions import TransactionNotFound

import src.services.sweep_engine as sweep_engine_module
from src.clients.rofl import TransactionRevertedError
from src.services.sweep_engine import SweepEngine, SweepRecord, SweepState


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path)


def _write_record(state_dir, record):
    """Helper to persist a SweepRecord to the state directory."""
    key = record.deposit_id_hex.lower().removeprefix("0x")
    path = Path(state_dir) / f"sweep_{key}.json"
    path.write_text(json.dumps(record.to_dict()))


def _write_legacy_record(state_dir, record):
    """Persist a record under the pre-deposit_id address-keyed filename."""
    path = Path(state_dir) / f"sweep_{record.deposit_address.lower()}_{record.chain_id}.json"
    path.write_text(json.dumps(record.to_dict()))
    return path


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


def test_load_migrates_legacy_address_keyed_record(state_dir):
    """A pre-deposit_id file is rewritten under the deposit_id key exactly once.

    The old path must be unlinked, or the legacy file would resurrect the
    record on every restart.
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
    )
    legacy_path = _write_legacy_record(state_dir, record)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    incomplete = engine.load_incomplete_sweeps()
    assert len(incomplete) == 1
    assert not legacy_path.exists()
    migrated = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert migrated is not None
    assert migrated.state == SweepState.GAS_FUNDED

    # A second load must see exactly one record, not a resurrected duplicate.
    assert len(engine.load_incomplete_sweeps()) == 1


def test_load_dedupes_legacy_and_new_keyed_files(state_dir):
    """Legacy and deposit_id-keyed files for the same deposit load as one record.

    This happens when new code wrote the deposit_id file before the legacy
    file from an older deploy was cleaned up: the newer copy must win and the
    legacy file must be dropped without double-counting the deposit.
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
    )
    legacy_path = _write_legacy_record(state_dir, record)
    newer = SweepRecord.from_dict({**record.to_dict(), "state": SweepState.SWEPT.value})
    _write_record(state_dir, newer)

    engine = SweepEngine(
        accounting_service=AsyncMock(),
        chain_rpc_urls={84532: "https://fake"},
        state_dir=state_dir,
    )
    incomplete = engine.load_incomplete_sweeps()
    assert len(incomplete) == 1
    assert incomplete[0].state == SweepState.SWEPT
    assert not legacy_path.exists()


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
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


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
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


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
    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
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
    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
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
    assert engine.get_record_by_deposit_id("0x" + "22" * 32) is None


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

    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
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

    persisted = engine.get_record_by_deposit_id("0x" + "22" * 32)
    assert persisted is not None
    assert persisted.error is None, "error must not overwrite a record that has sweep_tx_hash"
    assert persisted.sweep_tx_hash == "0x" + "dd" * 32
