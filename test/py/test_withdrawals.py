"""Tests for withdrawal processing service."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from src.services.withdrawal_processor import WithdrawalProcessor


TEST_USER_ADDRESS = "0x1234567890123456789012345678901234567890"
TEST_CHAIN_ID = 84532


class TestWithdrawalProcessor:
    """Tests for the WithdrawalProcessor service."""

    @pytest.fixture
    def mock_accounting_service(self):
        """Create a mock accounting service."""
        service = MagicMock()
        service.get_all_pending_withdrawals = MagicMock(return_value={
            "pending": [],
            "current_block": 100,
        })
        service.resolve_withdrawal = MagicMock(return_value=MagicMock(submission_id="test-123"))
        service._send_raw_transaction = MagicMock(return_value="0x" + "ab" * 32)
        service._get_reader_contract = MagicMock()
        return service

    @pytest.fixture
    def processor(self, mock_accounting_service):
        """Create a WithdrawalProcessor with mocked dependencies."""
        with patch("src.services.withdrawal_processor.load_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                withdrawal_poll_interval=1,
                sapphire_rpc_url="https://testnet.sapphire.oasis.io",
                accounting_contract_address="0x" + "ab" * 20,
            )
            with patch("src.services.withdrawal_processor.AccountingContractService", return_value=mock_accounting_service):
                with patch("src.services.withdrawal_processor.Web3") as mock_web3:
                    mock_web3.return_value.eth.block_number = 100
                    mock_web3.to_checksum_address.return_value = "0x" + "Ab" * 20
                    proc = WithdrawalProcessor()
                    proc.accounting_service = mock_accounting_service
                    return proc

    @pytest.mark.asyncio
    async def test_get_pending_empty(self, processor):
        """Test getting pending withdrawals when none exist."""
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        assert result == {}

    @pytest.mark.asyncio
    async def test_get_pending_filters_by_block_delay(self, processor):
        """Test that pending withdrawals are filtered by block delay."""
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {"index": 0, "block_number": 98, "chain_id": TEST_CHAIN_ID},  # eligible
                {"index": 1, "block_number": 100, "chain_id": TEST_CHAIN_ID},  # not eligible
                {"index": 2, "block_number": 50, "chain_id": TEST_CHAIN_ID},  # eligible
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        assert TEST_CHAIN_ID in result
        assert len(result[TEST_CHAIN_ID]) == 2
        indices = [w["index"] for w in result[TEST_CHAIN_ID]]
        assert 0 in indices
        assert 2 in indices
        assert 1 not in indices

    @pytest.mark.asyncio
    async def test_get_pending_groups_by_chain(self, processor):
        """Test that pending withdrawals are grouped by chain."""
        chain_1 = 1
        chain_2 = 2
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {"index": 0, "block_number": 50, "chain_id": chain_1},
                {"index": 1, "block_number": 50, "chain_id": chain_2},
                {"index": 2, "block_number": 50, "chain_id": chain_1},
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        assert chain_1 in result
        assert chain_2 in result
        assert len(result[chain_1]) == 2
        assert len(result[chain_2]) == 1

    @pytest.mark.asyncio
    async def test_get_pending_sorts_by_index(self, processor):
        """Test that withdrawals are sorted by index within each chain."""
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {"index": 5, "block_number": 50, "chain_id": TEST_CHAIN_ID},
                {"index": 2, "block_number": 50, "chain_id": TEST_CHAIN_ID},
                {"index": 8, "block_number": 50, "chain_id": TEST_CHAIN_ID},
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        indices = [w["index"] for w in result[TEST_CHAIN_ID]]
        assert indices == [2, 5, 8]

    @pytest.mark.asyncio
    async def test_start_stop(self, processor):
        """Test processor start and stop lifecycle."""
        assert processor._is_running is False

        await processor.start()
        assert processor._is_running is True
        assert processor._task is not None

        await processor.stop()
        assert processor._is_running is False
