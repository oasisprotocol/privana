import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from web3 import Web3

from src.services.deposit_processor import DepositProcessor
from src.services.deposit_verifier import VerifiedDeposit
from src.services.sweep_engine import SweepRecord, SweepState

ONE_ETH = Web3.to_wei(1, "ether")
FIFTY_USDC = 50 * 10**6


@pytest.fixture
def mock_verifier():
    return AsyncMock()


@pytest.fixture
def mock_sweep_engine():
    engine = AsyncMock()
    engine.gas_funding_tx_hashes = set()
    engine.get_sweep_record = MagicMock(return_value=None)
    engine.get_record_by_deposit_id = MagicMock(return_value=None)
    engine.cleanup_record = MagicMock()
    engine.persist_error = MagicMock()
    return engine


@pytest.fixture
def mock_accounting():
    svc = AsyncMock()
    svc.get_deposit_address = AsyncMock(return_value="0x" + "aa" * 20)
    svc.get_token_id = AsyncMock(return_value=b"\x11" * 32)
    svc.is_deposit_processed = AsyncMock(return_value=False)  # not yet processed by default
    svc.is_token_registered = AsyncMock(return_value=True)  # registered by default
    return svc


@pytest.fixture
def processor(mock_verifier, mock_sweep_engine, mock_accounting):
    return DepositProcessor(
        verifier=mock_verifier,
        sweep_engine=mock_sweep_engine,
        accounting_service=mock_accounting,
    )


@pytest.mark.asyncio
async def test_process_native_deposit(processor, mock_verifier, mock_sweep_engine):
    """Full flow: verify → background sweep native."""
    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    assert result["status"] == "pending"
    assert result["deposit_id"] is not None

    await asyncio.sleep(0)
    mock_sweep_engine.sweep_native.assert_called_once()


@pytest.mark.asyncio
async def test_reject_gas_funding_tx(processor, mock_verifier, mock_sweep_engine):
    """Gas funding txs must not be claimable as deposits."""
    gas_tx_hash = "0x" + "ff" * 32
    mock_sweep_engine.gas_funding_tx_hashes = {gas_tx_hash.lower()}

    with pytest.raises(ValueError, match="[Gg]as funding"):
        await processor.process_deposit(
            chain_type="evm",
            chain_id=84532,
            tx_hash=gas_tx_hash,
            amount=ONE_ETH,
            log_index=0,
            version=0,
            siwe_token=b"\x00" * 65,
            beneficiary="0x" + "bb" * 20,
        )


@pytest.mark.asyncio
async def test_idempotent_duplicate_returns_credited(
    processor, mock_verifier, mock_sweep_engine, mock_accounting
):
    """Duplicate /deposits/check for an already-processed deposit returns success without sweeping."""
    mock_accounting.is_deposit_processed = AsyncMock(return_value=True)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        token_address=None,
        is_native=True,
        deposit_index=0,
        block_number=100,
    )

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    assert result["status"] == "credited"
    # No sweep should have been triggered
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_reject_unsupported_token(
    processor, mock_verifier, mock_sweep_engine, mock_accounting
):
    """Deposits of unregistered tokens must be rejected before sweeping."""
    mock_accounting.is_token_registered = AsyncMock(return_value=False)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        token_address="0x" + "ee" * 20,  # unregistered ERC20
        is_native=False,
        deposit_index=0,
        block_number=100,
    )

    with pytest.raises(ValueError, match="[Tt]oken not supported"):
        await processor.process_deposit(
            chain_type="evm",
            chain_id=84532,
            tx_hash="0x" + "cc" * 32,
            amount=ONE_ETH,
            log_index=0,
            version=0,
            siwe_token=b"\x00" * 65,
            beneficiary="0x" + "bb" * 20,
        )

    # Must not sweep unsupported tokens
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_reject_below_minimum_native(
    processor,
    mock_verifier,
    mock_sweep_engine,
):
    """Native deposits below MIN_DEPOSIT_NATIVE_WEI should be rejected."""
    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=1000,  # way below 0.01 ETH minimum
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    with pytest.raises(ValueError, match="minimum requirements"):
        await processor.process_deposit(
            chain_type="evm",
            chain_id=84532,
            tx_hash="0x" + "cc" * 32,
            amount=1000,
            log_index=0,
            version=0,
            siwe_token=b"\x00" * 65,
            beneficiary="0x" + "bb" * 20,
        )

    mock_sweep_engine.sweep_native.assert_not_called()


@pytest.mark.asyncio
async def test_process_erc20_deposit(processor, mock_verifier, mock_sweep_engine):
    """ERC20 deposit should call sweep_erc20 with token_address."""
    token_addr = "0x" + "cc" * 20
    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "dd" * 32,
        amount=FIFTY_USDC,
        is_native=False,
        token_address=token_addr,
        deposit_index=0,
        block_number=100,
    )

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "dd" * 32,
        amount=FIFTY_USDC,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    assert result["status"] == "pending"
    assert result["deposit_id"] is not None

    await asyncio.sleep(0)
    mock_sweep_engine.sweep_erc20.assert_called_once()
    call_kwargs = mock_sweep_engine.sweep_erc20.call_args.kwargs
    assert call_kwargs["token_address"] == token_addr
    mock_sweep_engine.sweep_native.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_check_returns_pending(
    processor, mock_verifier, mock_sweep_engine, mock_accounting
):
    """Second /deposits/check while sweep is in progress returns pending without re-sweeping."""
    existing_record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=ONE_ETH,
        deposit_id_hex="0x" + "dd" * 32,
    )
    mock_sweep_engine.get_record_by_deposit_id = MagicMock(return_value=existing_record)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    assert result["status"] == "pending"
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_stop_awaits_in_flight_background_sweeps(processor, mock_verifier, mock_sweep_engine):
    """stop() must await in-flight sweep tasks so shutdown doesn't cut them mid-await."""
    sweep_started = asyncio.Event()
    sweep_can_finish = asyncio.Event()

    async def slow_sweep(**_kwargs):
        sweep_started.set()
        await sweep_can_finish.wait()

    mock_sweep_engine.sweep_native = AsyncMock(side_effect=slow_sweep)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    await sweep_started.wait()
    assert len(processor._background_tasks) == 1

    # Spin multiple times so stop() is definitively parked on asyncio.wait,
    # not just its first yield.
    stop_task = asyncio.create_task(processor.stop())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not stop_task.done(), "stop() returned while sweep was still in flight"

    sweep_can_finish.set()
    await asyncio.wait_for(stop_task, timeout=1)
    assert len(processor._background_tasks) == 0


@pytest.mark.asyncio
async def test_stop_with_no_background_tasks_is_noop(processor, mock_sweep_engine):
    """stop() on a fresh processor just stops the recovery loop and returns."""
    await processor.stop()
    mock_sweep_engine.stop_recovery_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_timeout_preserves_task_for_next_recovery(
    processor, mock_verifier, mock_sweep_engine, monkeypatch
):
    """stop() on timeout must NOT cancel in-flight tasks.

    The recovery contract relies on tasks either finishing naturally OR being
    cancelled only by event loop teardown AFTER their last _save_record. A
    future refactor to asyncio.wait_for (which cancels on timeout) would
    silently break this — that's what this test guards against.
    """
    import src.services.deposit_processor as processor_module

    monkeypatch.setattr(processor_module, "BACKGROUND_SWEEP_SHUTDOWN_TIMEOUT", 0.01)

    sweep_started = asyncio.Event()
    sweep_can_finish = asyncio.Event()

    async def slow_sweep(**_kwargs):
        sweep_started.set()
        await sweep_can_finish.wait()

    mock_sweep_engine.sweep_native = AsyncMock(side_effect=slow_sweep)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    await sweep_started.wait()
    background_task = next(iter(processor._background_tasks))

    await asyncio.wait_for(processor.stop(), timeout=1)

    assert not background_task.cancelled(), (
        "stop() timeout cancelled the task — wait_for regression"
    )
    assert not background_task.done(), "task unexpectedly finished"

    sweep_can_finish.set()
    await asyncio.wait_for(background_task, timeout=1)


@pytest.mark.asyncio
async def test_errored_record_allows_retry(
    processor, mock_verifier, mock_sweep_engine, mock_accounting
):
    """If previous sweep failed (error in record), a retry deletes the old record and starts fresh."""
    errored_record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.PENDING,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        error="Gas funding tx reverted",
    )
    mock_sweep_engine.get_record_by_deposit_id = MagicMock(return_value=errored_record)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    assert result["status"] == "pending"
    mock_sweep_engine.cleanup_record.assert_called_once()


@pytest.mark.asyncio
async def test_errored_record_with_sweep_tx_hash_preserves_record(
    processor, mock_verifier, mock_sweep_engine
):
    """Once a sweep tx is broadcast, retry must not delete its recovery pointer."""
    errored_record = SweepRecord(
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        error="receipt polling timed out",
        sweep_tx_hash="0x" + "dd" * 32,
    )
    mock_sweep_engine.get_record_by_deposit_id = MagicMock(return_value=errored_record)

    mock_verifier.verify_deposit.return_value = VerifiedDeposit(
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        is_native=True,
        token_address=None,
        deposit_index=0,
        block_number=100,
    )

    result = await processor.process_deposit(
        chain_type="evm",
        chain_id=84532,
        tx_hash="0x" + "cc" * 32,
        amount=ONE_ETH,
        log_index=0,
        version=0,
        siwe_token=b"\x00" * 65,
        beneficiary="0x" + "bb" * 20,
    )

    assert result["status"] == "pending"
    status = processor.get_deposit_status(result["deposit_id"], "0x" + "bb" * 20)
    assert status is not None
    assert status["status"] == "pending"
    assert "detail" not in status
    mock_sweep_engine.cleanup_record.assert_not_called()
    mock_sweep_engine.sweep_native.assert_not_called()
