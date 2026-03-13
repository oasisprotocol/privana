"""Tests for deposit retry scheduling behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.rofl import TransactionRevertedError
from src.services.block_state import BlockStateManager, PendingTx
from src.services.deposit_listener import DepositListener


@pytest.fixture
def listener():
    """Create a lightweight listener instance without running __init__."""
    instance = DepositListener.__new__(DepositListener)
    instance._is_running = True
    instance.chain_rpc_urls = {1: "http://example-rpc"}
    instance._deposit_tasks = set()
    instance._tasks = set()
    instance._retry_task = None
    return instance


@pytest.mark.asyncio
async def test_retry_worker_retries_only_failed_transactions(listener):
    """Retry loop should claim and process only failed transactions."""
    tx_hash = "0x" + "ab" * 32
    from_address = "0x1234567890123456789012345678901234567890"
    listener._block_state = MagicMock()
    listener._process_pending_tx = AsyncMock()
    listener._block_state.get_failed_txs.return_value = {
        tx_hash: PendingTx(
            tx_hash=tx_hash,
            block_number=123,
            from_address=from_address,
            status="failed",
        )
    }
    listener._block_state.get_pending_txs.return_value = {}
    listener._block_state.claim_retry = MagicMock(return_value=1)
    sleep_calls = 0

    async def fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            listener._is_running = False

    with patch("src.services.deposit_listener.asyncio.sleep", side_effect=fake_sleep):
        await listener._retry_pending_deposits()

    listener._block_state.get_pending_txs.assert_called_once_with(1)
    listener._block_state.get_failed_txs.assert_called_once_with(1)
    listener._block_state.claim_retry.assert_called_once_with(1, tx_hash)
    listener._process_pending_tx.assert_called_once_with(
        chain_id=1,
        tx_hash=tx_hash,
        from_address=from_address,
        value=None,
        block_number=123,
        token_address=None,
    )


@pytest.mark.asyncio
async def test_retry_worker_skips_unclaimable_failed_transactions(listener):
    """Retry loop should not process if retry claim fails."""
    tx_hash = "0x" + "cd" * 32
    listener._block_state = MagicMock()
    listener._process_pending_tx = AsyncMock()
    listener._block_state.get_failed_txs.return_value = {
        tx_hash: PendingTx(
            tx_hash=tx_hash,
            block_number=456,
            from_address="0x1234567890123456789012345678901234567890",
            status="failed",
        )
    }
    listener._block_state.get_pending_txs.return_value = {}
    listener._block_state.claim_retry = MagicMock(return_value=None)
    sleep_calls = 0

    async def fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            listener._is_running = False

    with patch("src.services.deposit_listener.asyncio.sleep", side_effect=fake_sleep):
        await listener._retry_pending_deposits()

    listener._block_state.get_pending_txs.assert_called_once_with(1)
    listener._block_state.get_failed_txs.assert_called_once_with(1)
    listener._block_state.claim_retry.assert_called_once_with(1, tx_hash)
    listener._process_pending_tx.assert_not_called()


@pytest.mark.asyncio
async def test_retry_worker_marks_dead_when_metadata_missing(listener):
    """Failed tx with missing from_address should be marked dead before claim."""
    tx_hash = "0x" + "99" * 32
    listener._block_state = MagicMock()
    listener._process_pending_tx = AsyncMock()
    listener._block_state.get_failed_txs.return_value = {
        tx_hash: PendingTx(
            tx_hash=tx_hash,
            block_number=321,
            # from_address intentionally missing
            status="failed",
        )
    }
    listener._block_state.get_pending_txs.return_value = {}
    sleep_calls = 0

    async def fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            listener._is_running = False

    with patch("src.services.deposit_listener.asyncio.sleep", side_effect=fake_sleep):
        await listener._retry_pending_deposits()

    # from_address check happens before claim_retry
    listener._block_state.claim_retry.assert_not_called()
    listener._block_state.mark_dead.assert_called_once_with(1, tx_hash, "missing from_address")
    listener._process_pending_tx.assert_not_called()


@pytest.mark.asyncio
async def test_process_deposit_marks_failed_when_block_hash_missing(listener):
    """Processing should record a failure when block hash is unavailable."""
    tx_hash = "0x" + "ef" * 32
    block_number = 789

    listener._block_state = MagicMock()
    listener._block_state.get_pending_txs.return_value = {}
    listener._get_chain_web3 = MagicMock(return_value=MagicMock())
    listener._rate_limited_rpc_call = AsyncMock(return_value={"status": 1})
    listener._wait_for_block_hash_stored = AsyncMock(return_value=False)

    await listener._process_pending_tx(
        chain_id=1,
        tx_hash=tx_hash,
        from_address="0x1234567890123456789012345678901234567890",
        value=42,
        block_number=block_number,
        token_address=None,
    )

    listener._block_state.add_pending_tx.assert_called_once()
    listener._block_state.mark_failed.assert_called_once()
    listener._block_state.complete_pending_tx.assert_not_called()
    assert f"block {block_number}" in listener._block_state.mark_failed.call_args[0][2]


@pytest.mark.asyncio
async def test_process_deposit_skips_dead_entry_on_rescan(listener, tmp_path):
    """Rescanning an old block should skip transactions already marked dead."""
    tx_hash = "0x" + "aa" * 32
    chain_id = 1
    block_number = 555
    from_address = "0x1234567890123456789012345678901234567890"

    manager = BlockStateManager(state_dir=str(tmp_path))
    manager.initialize()
    manager.add_pending_tx(
        chain_id=chain_id,
        tx_hash=tx_hash,
        block_number=block_number,
        from_address=from_address,
    )
    manager.mark_dead(chain_id, tx_hash, "terminal error")
    assert manager.get_pending_txs(chain_id)[tx_hash].status == "dead"

    listener._block_state = manager
    listener._get_chain_web3 = MagicMock(return_value=MagicMock())
    listener._rate_limited_rpc_call = AsyncMock(return_value={"status": 1})
    listener._wait_for_block_hash_stored = AsyncMock(return_value=False)

    await listener._process_pending_tx(
        chain_id=chain_id,
        tx_hash=tx_hash,
        from_address=from_address,
        value=42,
        block_number=block_number,
        token_address=None,
    )

    pending = manager.get_pending_txs(chain_id)[tx_hash]
    assert pending.status == "dead"
    assert pending.last_error == "terminal error"
    listener._get_chain_web3.assert_not_called()
    listener._rate_limited_rpc_call.assert_not_called()
    listener._wait_for_block_hash_stored.assert_not_called()


@pytest.mark.asyncio
async def test_retry_worker_marks_stale_processing_as_failed(listener):
    """Stale processing entries should be converted to failed before retries."""
    tx_hash = "0x" + "11" * 32
    listener._block_state = MagicMock()
    listener._process_pending_tx = AsyncMock()
    listener._block_state.get_pending_txs.return_value = {
        tx_hash: PendingTx(
            tx_hash=tx_hash,
            block_number=100,
            status="processing",
            processing_started_at=1.0,
        )
    }
    listener._block_state.get_failed_txs.return_value = {}
    sleep_calls = 0

    async def fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            listener._is_running = False

    with patch("src.services.deposit_listener.asyncio.sleep", side_effect=fake_sleep):
        with patch("src.services.deposit_listener.time.time", return_value=1000.0):
            await listener._retry_pending_deposits()

    listener._block_state.mark_failed.assert_called_once()
    assert "processing timeout" in listener._block_state.mark_failed.call_args[0][2]
    listener._process_pending_tx.assert_not_called()


@pytest.mark.asyncio
async def test_retry_worker_marks_processing_without_start_timestamp_as_failed(listener):
    """Processing entries without start timestamp should be recovered as failed."""
    tx_hash = "0x" + "45" * 32
    listener._block_state = MagicMock()
    listener._process_pending_tx = AsyncMock()
    listener._block_state.get_pending_txs.return_value = {
        tx_hash: PendingTx(
            tx_hash=tx_hash,
            block_number=101,
            status="processing",
            processing_started_at=0.0,
        )
    }
    listener._block_state.get_failed_txs.return_value = {}
    sleep_calls = 0

    async def fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            listener._is_running = False

    with patch("src.services.deposit_listener.asyncio.sleep", side_effect=fake_sleep):
        await listener._retry_pending_deposits()

    listener._block_state.mark_failed.assert_called_once()
    assert "missing start timestamp" in listener._block_state.mark_failed.call_args[0][2]
    listener._process_pending_tx.assert_not_called()


@pytest.mark.asyncio
async def test_retry_worker_marks_dead_when_retry_cap_reached(listener):
    """Failed tx at retry cap should be dead-lettered instead of retried."""
    tx_hash = "0x" + "22" * 32
    listener._block_state = MagicMock()
    listener._process_pending_tx = AsyncMock()
    listener._block_state.get_pending_txs.return_value = {}
    listener._block_state.get_failed_txs.return_value = {
        tx_hash: PendingTx(
            tx_hash=tx_hash,
            block_number=200,
            status="failed",
            retry_count=DepositListener.MAX_RETRY_ATTEMPTS,
            from_address="0x1234567890123456789012345678901234567890",
        )
    }
    sleep_calls = 0

    async def fake_sleep(_seconds: float):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            listener._is_running = False

    with patch("src.services.deposit_listener.asyncio.sleep", side_effect=fake_sleep):
        await listener._retry_pending_deposits()

    listener._block_state.mark_dead.assert_called_once()
    listener._block_state.claim_retry.assert_not_called()
    listener._process_pending_tx.assert_not_called()


@pytest.mark.asyncio
async def test_process_deposit_completes_when_already_processed(listener):
    """DepositAlreadyProcessed error should mark deposit as complete, not failed."""
    tx_hash = "0x" + "33" * 32
    listener._block_state = MagicMock()
    listener._block_state.get_pending_txs.return_value = {}
    listener._get_chain_web3 = MagicMock(return_value=MagicMock())
    listener._rate_limited_rpc_call = AsyncMock(return_value={"status": 1})
    listener._wait_for_block_hash_stored = AsyncMock(return_value=True)
    listener._proof_generator = MagicMock()
    listener._proof_generator.generate_deposit_proofs = MagicMock(
        return_value={"rlp_block_header": "0x01"}
    )
    listener._get_native_token_id = AsyncMock(return_value="0x" + "11" * 32)
    listener.accounting_service = MagicMock()
    listener.accounting_service.include_deposit = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: DepositAlreadyProcessed", error_name="DepositAlreadyProcessed"
        )
    )

    with patch(
        "src.services.deposit_listener.asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)
    ):
        await listener._process_pending_tx(
            chain_id=1,
            tx_hash=tx_hash,
            from_address="0x1234567890123456789012345678901234567890",
            value=42,
            block_number=555,
            token_address=None,
        )

    # Should be marked complete since the deposit was already processed successfully
    listener._block_state.complete_pending_tx.assert_called_once_with(1, tx_hash)
    listener._block_state.mark_dead.assert_not_called()
    listener._block_state.mark_failed.assert_not_called()


@pytest.mark.asyncio
async def test_process_deposit_marks_dead_for_terminal_contract_error(listener):
    """Terminal contract errors (not success-equivalent) should be marked dead."""
    tx_hash = "0x" + "44" * 32
    listener._block_state = MagicMock()
    listener._block_state.get_pending_txs.return_value = {}
    listener._get_chain_web3 = MagicMock(return_value=MagicMock())
    listener._rate_limited_rpc_call = AsyncMock(return_value={"status": 1})
    listener._wait_for_block_hash_stored = AsyncMock(return_value=True)
    listener._proof_generator = MagicMock()
    listener._proof_generator.generate_deposit_proofs = MagicMock(
        return_value={"rlp_block_header": "0x01"}
    )
    listener._get_native_token_id = AsyncMock(return_value="0x" + "11" * 32)
    listener.accounting_service = MagicMock()
    listener.accounting_service.include_deposit = AsyncMock(
        side_effect=TransactionRevertedError(
            "Transaction reverted: InvalidProof", error_name="InvalidProof"
        )
    )

    with patch(
        "src.services.deposit_listener.asyncio.to_thread", side_effect=lambda fn, *a: fn(*a)
    ):
        await listener._process_pending_tx(
            chain_id=1,
            tx_hash=tx_hash,
            from_address="0x1234567890123456789012345678901234567890",
            value=42,
            block_number=555,
            token_address=None,
        )

    listener._block_state.mark_dead.assert_called_once()
    listener._block_state.complete_pending_tx.assert_not_called()
    listener._block_state.mark_failed.assert_not_called()
