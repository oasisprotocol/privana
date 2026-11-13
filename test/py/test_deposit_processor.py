import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from web3 import Web3

from src.models.private_read import PrivateReadAuth
from src.services.deposit_processor import DepositProcessor
from src.services.sweep_engine import BalanceClaim, BalanceClaimStatus, SweepRecord, SweepState

ONE_ETH = Web3.to_wei(1, "ether")
FIFTY_USDC = 50 * 10**6
TOKEN_ID = f"0x{1:064x}"
PRIVATE_READ_AUTH = PrivateReadAuth(token=b"\x00" * 65, user_address="0x" + "bb" * 20)


@pytest.fixture
def mock_sweep_engine():
    engine = AsyncMock()
    engine.get_record_by_deposit_id = MagicMock(return_value=None)
    engine.persist_error = MagicMock()
    record = MagicMock(
        deposit_id_hex="0x" + "77" * 32, amount=ONE_ETH, error=None, sweep_tx_hash=None
    )
    engine.claim_balance.return_value = BalanceClaim(
        BalanceClaimStatus.STARTED, record, amount=ONE_ETH
    )
    return engine


@pytest.fixture
def mock_accounting():
    svc = AsyncMock()
    svc.get_deposit_address = AsyncMock(return_value="0x" + "aa" * 20)
    svc.is_deposit_processed = AsyncMock(return_value=False)  # not yet processed by default
    return svc


@pytest.fixture
def token_infos():
    return {f"0x{i:064x}": {"chain_id": 84532, "token_address": None} for i in range(1, 5)}


@pytest.fixture
def processor(mock_sweep_engine, mock_accounting, token_infos, monkeypatch):
    monkeypatch.setattr(
        "src.services.deposit_processor.load_settings",
        lambda: SimpleNamespace(token_infos=token_infos),
    )
    return DepositProcessor(
        sweep_engine=mock_sweep_engine,
        accounting_service=mock_accounting,
    )


@pytest.mark.asyncio
async def test_process_native_deposit(processor, mock_sweep_engine, mock_accounting):
    """Full flow: verify → background sweep native."""
    [result] = await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
    )

    assert result["status"] == "pending"
    assert result["deposit_id"] is not None
    mock_accounting.get_deposit_address.assert_awaited_once_with("evm", 0, PRIVATE_READ_AUTH.token)

    await asyncio.sleep(0)
    claim_kwargs = mock_sweep_engine.claim_balance.call_args.kwargs
    assert claim_kwargs["beneficiary"] == PRIVATE_READ_AUTH.user_address
    mock_sweep_engine.sweep_native.assert_called_once_with(b"\x77" * 32)


@pytest.mark.asyncio
async def test_unconfigured_chain_is_skipped(processor, mock_sweep_engine, token_infos, caplog):
    """A chain with no CHAIN_CONFIGS entry is logged and skipped, never swept."""
    token_infos[TOKEN_ID]["chain_id"] = 1234567

    result = await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
    )

    assert result == []
    assert "Unsupported chain_id" in caplog.text
    mock_sweep_engine.claim_balance.assert_not_called()


@pytest.mark.asyncio
async def test_nothing_left_to_claim_returns_none(processor, mock_sweep_engine):
    """A repeat /deposits/check after the funds were swept finds nothing and sweeps nothing."""
    mock_sweep_engine.claim_balance.return_value = BalanceClaim(BalanceClaimStatus.NONE)
    result = await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
    )

    assert result == []
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_reject_unsupported_token(processor, mock_sweep_engine):
    """Deposits of tokens that are not configured must be rejected before sweeping."""
    with pytest.raises(ValueError, match="Token not supported"):
        await processor.check_and_schedule_deposits(
            version=0,
            token_id="0x" + "99" * 32,
            auth=PRIVATE_READ_AUTH,
        )

    # Must not sweep unsupported tokens
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_process_erc20_deposit(processor, mock_sweep_engine, token_infos):
    """ERC20 deposit should call sweep_erc20 with token_address."""
    token_addr = Web3.to_checksum_address("0x" + "cc" * 20)
    token_infos[TOKEN_ID]["token_address"] = token_addr
    [result] = await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
    )

    assert result["status"] == "pending"
    assert result["deposit_id"] is not None

    await asyncio.sleep(0)
    claim_kwargs = mock_sweep_engine.claim_balance.call_args.kwargs
    assert claim_kwargs["token_address"] == token_addr
    mock_sweep_engine.sweep_erc20.assert_called_once_with(b"\x77" * 32)
    mock_sweep_engine.sweep_native.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_check_returns_pending(processor, mock_sweep_engine, mock_accounting):
    """Second /deposits/check while sweep is in progress returns pending without re-sweeping."""
    existing_record = SweepRecord(
        token_id_hex="0x" + "11" * 32,
        deposit_address="0x" + "aa" * 20,
        chain_id=84532,
        state=SweepState.GAS_FUNDED,
        beneficiary="0x" + "bb" * 20,
        chain_type="evm",
        version=0,
        amount=ONE_ETH,
        deposit_id_hex="0x" + "dd" * 32,
    )
    mock_sweep_engine.claim_balance.return_value = BalanceClaim(
        BalanceClaimStatus.IN_FLIGHT, existing_record
    )

    [result] = await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
    )

    assert result["status"] == "pending"
    mock_sweep_engine.sweep_native.assert_not_called()
    mock_sweep_engine.sweep_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_stop_awaits_in_flight_background_sweeps(processor, mock_sweep_engine):
    """stop() must await in-flight sweep tasks so shutdown doesn't cut them mid-await."""
    sweep_started = asyncio.Event()
    sweep_can_finish = asyncio.Event()

    async def slow_sweep(_deposit_id):
        sweep_started.set()
        await sweep_can_finish.wait()

    mock_sweep_engine.sweep_native = AsyncMock(side_effect=slow_sweep)

    await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
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
    processor, mock_sweep_engine, monkeypatch
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

    async def slow_sweep(_deposit_id):
        sweep_started.set()
        await sweep_can_finish.wait()

    mock_sweep_engine.sweep_native = AsyncMock(side_effect=slow_sweep)

    await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
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
async def test_errored_record_with_sweep_tx_hash_preserves_record(processor, mock_sweep_engine):
    """Once a sweep tx is broadcast, retry must not delete its recovery pointer."""
    errored_record = SweepRecord(
        token_id_hex="0x" + "11" * 32,
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
    mock_sweep_engine.claim_balance.return_value = BalanceClaim(
        BalanceClaimStatus.IN_FLIGHT, errored_record
    )

    [result] = await processor.check_and_schedule_deposits(
        version=0,
        token_id=TOKEN_ID,
        auth=PRIVATE_READ_AUTH,
    )

    assert result["status"] == "pending"
    status = processor.get_deposit_status(result["deposit_id"], "0x" + "bb" * 20)
    assert status is not None
    assert status["status"] == "pending"
    assert "detail" not in status
    mock_sweep_engine.sweep_native.assert_not_called()


@pytest.mark.asyncio
async def test_without_token_id_every_configured_token_is_claimed(processor, mock_sweep_engine):
    """Every token with something to claim gets a sweep; the others are only reported."""

    def scheduled(n, status=BalanceClaimStatus.STARTED):
        record = MagicMock(deposit_id_hex="0x" + f"{n:02x}" * 32)
        return BalanceClaim(status, record, amount=ONE_ETH)

    mock_sweep_engine.claim_balance.side_effect = [
        scheduled(7),
        BalanceClaim(BalanceClaimStatus.NONE),
        scheduled(5),
        scheduled(6, BalanceClaimStatus.IN_FLIGHT),
    ]
    started = []
    mock_sweep_engine.sweep_native.side_effect = lambda deposit_id: started.append(deposit_id)

    result = await processor.check_and_schedule_deposits(
        version=0, token_id=None, auth=PRIVATE_READ_AUTH
    )
    await processor.stop()

    assert [deposit["deposit_id"] for deposit in result] == [
        "0x" + "07" * 32,
        "0x" + "05" * 32,
        "0x" + "06" * 32,
    ]
    assert started == [b"\x07" * 32, b"\x05" * 32]  # the in-flight one is not started again


@pytest.mark.asyncio
async def test_without_token_id_skips_a_token_that_fails(
    processor, mock_sweep_engine, mock_accounting
):
    mock_sweep_engine.claim_balance.side_effect = [
        ValueError("no verified RPC"),
        BalanceClaim(BalanceClaimStatus.NONE),
        BalanceClaim(BalanceClaimStatus.NONE),
        BalanceClaim(BalanceClaimStatus.NONE),
    ]

    result = await processor.check_and_schedule_deposits(
        version=0, token_id=None, auth=PRIVATE_READ_AUTH
    )

    assert result == []
