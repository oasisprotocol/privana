"""Tests for withdrawal processing service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode

from src.services.withdrawal_processor import WithdrawalProcessor

TEST_USER_ADDRESS = "0x1234567890123456789012345678901234567890"
TEST_TO_ADDRESS = "0x9876543210987654321098765432109876543210"
TEST_CHAIN_ID = 84532
TEST_TX_HASH = "0x" + "ab" * 32
TEST_SIGNED_TX = b"\x00" * 64


class TestWithdrawalProcessor:
    """Tests for the WithdrawalProcessor service."""

    @pytest.fixture
    def mock_accounting_service(self):
        """Create a mock accounting service."""
        service = MagicMock()
        service.get_all_pending_withdrawals = AsyncMock(
            return_value={
                "pending": [],
                "current_block": 100,
            }
        )
        service.resolve_withdrawal = AsyncMock(return_value=MagicMock(status="submitted"))
        service._send_raw_transaction = AsyncMock(return_value="0x" + "ab" * 32)
        # Return a mock contract with async call methods
        mock_contract = MagicMock()
        mock_contract.functions.withdrawals.return_value.call = AsyncMock()
        mock_contract.functions.resolveWithdrawal.return_value.call = AsyncMock()
        mock_contract.functions.withdrawalCount.return_value.call = AsyncMock()
        service._get_reader_contract = MagicMock(return_value=mock_contract)
        service._get_token_context = AsyncMock()
        return service

    @pytest.fixture
    def processor(self, mock_accounting_service):
        """Create a WithdrawalProcessor with mocked dependencies."""
        with patch("src.services.withdrawal_processor.load_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                withdrawal_poll_interval=1,
                withdrawal_resolution_timeout=1,
                sapphire_rpc_url="https://testnet.sapphire.oasis.io",
                accounting_contract_address="0x" + "ab" * 20,
                chain_rpc_urls={TEST_CHAIN_ID: "https://example.com"},
            )
            with patch(
                "src.services.withdrawal_processor.AccountingContractService",
                return_value=mock_accounting_service,
            ):
                with patch("src.services.withdrawal_processor.AsyncWeb3") as mock_async_web3:
                    # Mock the AsyncWeb3 instance
                    mock_w3_instance = MagicMock()
                    mock_contract = MagicMock()
                    mock_contract.functions.evmAddress.return_value.call = AsyncMock(
                        return_value=TEST_USER_ADDRESS
                    )
                    mock_contract.functions.nonces.return_value.call = AsyncMock(return_value=0)
                    mock_w3_instance.eth.contract.return_value = mock_contract
                    mock_async_web3.return_value = mock_w3_instance

                    with patch("src.services.withdrawal_processor.Web3") as mock_web3:
                        mock_web3.to_checksum_address.return_value = "0x" + "Ab" * 20

                        proc = WithdrawalProcessor()
                        proc.accounting_service = mock_accounting_service
                        proc._contract = mock_contract
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
    async def test_get_pending_filters_by_high_water_mark(self, processor):
        """Test that already-processed withdrawals are skipped via high-water mark."""
        processor._chain_high_water_mark[TEST_CHAIN_ID] = 2

        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {"index": 1, "block_number": 50, "chain_id": TEST_CHAIN_ID},
                {"index": 2, "block_number": 50, "chain_id": TEST_CHAIN_ID},
                {"index": 3, "block_number": 50, "chain_id": TEST_CHAIN_ID},
                {"index": 5, "block_number": 50, "chain_id": TEST_CHAIN_ID},
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        indices = [w["index"] for w in result[TEST_CHAIN_ID]]
        assert indices == [3, 5]

    @pytest.mark.asyncio
    async def test_get_pending_skips_invalid_chain_id(self, processor):
        """Test that withdrawals with missing or zero chain_id are skipped."""
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {"index": 0, "block_number": 50, "chain_id": TEST_CHAIN_ID},
                {"index": 1, "block_number": 50, "chain_id": 0},
                {"index": 2, "block_number": 50, "chain_id": None},
                {"index": 3, "block_number": 50},
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        assert list(result.keys()) == [TEST_CHAIN_ID]
        assert len(result[TEST_CHAIN_ID]) == 1

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_happy_path(self, processor):
        """Test successful resolve and broadcast flow."""
        contract_reader = processor.accounting_service._get_reader_contract()
        # withdrawals(index).call returns: (user, to, amount, block, tokenId, resolved, txId)
        # Already resolved on first check
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(["uint64"], [0]),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.return_value = TEST_TX_HASH

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        assert processor._chain_high_water_mark[TEST_CHAIN_ID] == 0
        processor.accounting_service._send_raw_transaction.assert_called_once_with(
            TEST_CHAIN_ID, TEST_SIGNED_TX
        )

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_already_resolved_on_chain(self, processor):
        """Test that an already-resolved withdrawal is broadcast without re-submitting."""
        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(["uint64"], [0]),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.return_value = TEST_TX_HASH

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        # resolve_withdrawal (ROFL submission) should NOT have been called
        processor.accounting_service.resolve_withdrawal.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_resolution_timeout(self, processor):
        """Test that a timeout waiting for resolution returns False."""
        contract_reader = processor.accounting_service._get_reader_contract()
        # Never becomes resolved
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            False,
            b"",
        )
        processor.accounting_service.resolve_withdrawal.return_value = MagicMock(status="submitted")

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}

        # Patch asyncio.sleep to avoid waiting
        with patch("src.services.withdrawal_processor.asyncio.sleep", new_callable=AsyncMock):
            result = await processor._resolve_and_broadcast(withdrawal)

        assert result is False
        # Should not have tried to broadcast
        processor.accounting_service._send_raw_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_nonce_too_low(self, processor):
        """Test that 'nonce too low' error is treated as success."""
        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(["uint64"], [0]),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.side_effect = Exception("nonce too low")

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        assert processor._chain_high_water_mark[TEST_CHAIN_ID] == 0

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_invalid_chain_id(self, processor):
        """Test that invalid chain_id returns True to prevent infinite retries."""
        withdrawal = {"index": 0, "chain_id": 0}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        # Should not have tried anything
        processor.accounting_service._get_reader_contract.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_stop(self, processor):
        """Test processor start and stop lifecycle."""
        assert processor._is_running is False

        await processor.start()
        assert processor._is_running is True
        assert processor._task is not None

        await processor.stop()
        assert processor._is_running is False

    @pytest.mark.asyncio
    async def test_catch_up_no_missing_broadcasts(self, processor):
        """Test catch-up when there are no missing broadcasts."""
        processor.settings.chain_rpc_urls = {TEST_CHAIN_ID: "https://example.com"}

        # Contract nonce <= chain nonce means nothing missing
        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=5)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=5)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        # Should not try to broadcast anything
        processor.accounting_service._send_raw_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_catch_up_finds_and_broadcasts_missing(self, processor):
        """Test catch-up finds and broadcasts missing withdrawals."""
        processor.settings.chain_rpc_urls = {TEST_CHAIN_ID: "https://example.com"}

        # Contract has nonce 2, chain has nonce 1 -> 1 missing
        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=2)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=1)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawalCount.return_value.call.return_value = 1
        # Withdrawal: resolved=True, txIdentifier encodes nonce=1
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(["uint64"], [1]),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        # Mock token context
        mock_context = MagicMock()
        mock_context.chain_id = TEST_CHAIN_ID
        processor.accounting_service._get_token_context = AsyncMock(return_value=mock_context)

        processor.accounting_service._send_raw_transaction.return_value = TEST_TX_HASH

        await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        processor.accounting_service._send_raw_transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_catch_up_handles_already_broadcast(self, processor):
        """Test catch-up handles 'nonce too low' gracefully."""
        processor.settings.chain_rpc_urls = {TEST_CHAIN_ID: "https://example.com"}

        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=2)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=1)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawalCount.return_value.call.return_value = 1
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(["uint64"], [1]),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        mock_context = MagicMock()
        mock_context.chain_id = TEST_CHAIN_ID
        processor.accounting_service._get_token_context = AsyncMock(return_value=mock_context)

        # Simulate already broadcast
        processor.accounting_service._send_raw_transaction.side_effect = Exception("nonce too low")

        # Should not raise, just log and continue
        await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])
