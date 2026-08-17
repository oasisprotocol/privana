"""Tests for withdrawal processing service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode
from web3.exceptions import TransactionNotFound

from src.services.accounting_contract import AccountingContractService
from src.services.withdrawal_processor import WithdrawalProcessor

TEST_USER_ADDRESS = "0x1234567890123456789012345678901234567890"
TEST_TO_ADDRESS = "0x9876543210987654321098765432109876543210"
TEST_CHAIN_ID = 84532
TEST_TX_HASH = "0x" + "ab" * 32
TEST_SIGNED_TX = b"\x00" * 64
TEST_OTHER_SIGNED_TX = b"\xff" * 64


def _hash_lookup(known: dict) -> AsyncMock:
    """Async mock shaped like an EVM node: answers only for the hashes it knows."""

    def _lookup(tx_hash):
        if tx_hash not in known:
            raise TransactionNotFound(f"{tx_hash} not found")
        return known[tx_hash]

    return AsyncMock(side_effect=_lookup)


def _resolved_withdrawal(nonce: int = 0) -> tuple:
    """withdrawals(index) tuple: (user, to, amount, block, tokenId, resolved, txId)."""
    return (
        TEST_USER_ADDRESS,
        TEST_TO_ADDRESS,
        100,
        50,
        b"\x00" * 32,
        True,
        encode(["uint64"], [nonce]),
    )


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
    async def test_duplicate_broadcast_confirmed_by_receipt_succeeds(self, processor):
        """A rejected re-broadcast is success only when a receipt exists for the hash
        of the exact signed transaction."""
        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            _resolved_withdrawal()
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        # Oasis wording, not geth's "nonce too low" - the error text must not matter
        processor.accounting_service._send_raw_transaction.side_effect = Exception("invalid nonce")

        expected_hash = WithdrawalProcessor._expected_tx_hash(TEST_SIGNED_TX)
        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_receipt = _hash_lookup({expected_hash: {"status": 1}})
        mock_dest_web3.eth.get_transaction = _hash_lookup({})
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        with patch("src.services.withdrawal_processor.asyncio.sleep", new_callable=AsyncMock):
            result = await processor._resolve_and_broadcast({"index": 0, "chain_id": TEST_CHAIN_ID})

        assert result is True
        assert processor._chain_high_water_mark[TEST_CHAIN_ID] == 0
        mock_dest_web3.eth.get_transaction_receipt.assert_any_await(expected_hash)

    @pytest.mark.asyncio
    async def test_duplicate_broadcast_confirmed_by_pending_tx_succeeds(self, processor):
        """A mempool entry for the expected hash is also proof of a prior broadcast."""
        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            _resolved_withdrawal()
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.side_effect = Exception("already known")

        expected_hash = WithdrawalProcessor._expected_tx_hash(TEST_SIGNED_TX)
        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_receipt = _hash_lookup({})
        mock_dest_web3.eth.get_transaction = _hash_lookup({expected_hash: {"hash": expected_hash}})
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        with patch("src.services.withdrawal_processor.asyncio.sleep", new_callable=AsyncMock):
            result = await processor._resolve_and_broadcast({"index": 0, "chain_id": TEST_CHAIN_ID})

        assert result is True
        assert processor._chain_high_water_mark[TEST_CHAIN_ID] == 0

    @pytest.mark.asyncio
    async def test_nonce_spent_by_foreign_tx_is_not_resolved(self, processor):
        """A nonce burned by a *different* transaction must never read as a paid
        withdrawal: no receipt for our hash means no success, no high-water mark."""
        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            _resolved_withdrawal()
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.side_effect = Exception("nonce too low")

        # The nonce was spent by an unrelated transaction, which is mined and known
        foreign_hash = WithdrawalProcessor._expected_tx_hash(TEST_OTHER_SIGNED_TX)
        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_receipt = _hash_lookup({foreign_hash: {"status": 1}})
        mock_dest_web3.eth.get_transaction = _hash_lookup({foreign_hash: {"hash": foreign_hash}})
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        with patch("src.services.withdrawal_processor.asyncio.sleep", new_callable=AsyncMock):
            result = await processor._resolve_and_broadcast({"index": 0, "chain_id": TEST_CHAIN_ID})

        assert result is False
        assert TEST_CHAIN_ID not in processor._chain_high_water_mark

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
        """Catch-up counts a rejected re-broadcast only when the chain has the exact
        signed transaction."""
        processor.settings.chain_rpc_urls = {TEST_CHAIN_ID: "https://example.com"}

        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=2)
        processor._evm_address = TEST_USER_ADDRESS

        expected_hash = WithdrawalProcessor._expected_tx_hash(TEST_SIGNED_TX)
        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=1)
        mock_dest_web3.eth.get_transaction_receipt = _hash_lookup({expected_hash: {"status": 1}})
        mock_dest_web3.eth.get_transaction = _hash_lookup({})
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawalCount.return_value.call.return_value = 1
        contract_reader.functions.withdrawals.return_value.call.return_value = _resolved_withdrawal(
            nonce=1
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        mock_context = MagicMock()
        mock_context.chain_id = TEST_CHAIN_ID
        processor.accounting_service._get_token_context = AsyncMock(return_value=mock_context)

        # Simulate already broadcast
        processor.accounting_service._send_raw_transaction.side_effect = Exception("invalid nonce")

        with patch("src.services.withdrawal_processor.asyncio.sleep", new_callable=AsyncMock):
            await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        mock_dest_web3.eth.get_transaction_receipt.assert_any_await(expected_hash)

    @pytest.mark.asyncio
    async def test_catch_up_does_not_trust_error_text_for_foreign_nonce(self, processor):
        """A duplicate-broadcast error whose nonce was spent elsewhere is verified by
        hash, found missing, and reported instead of being counted as broadcast."""
        processor.settings.chain_rpc_urls = {TEST_CHAIN_ID: "https://example.com"}

        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=2)
        processor._evm_address = TEST_USER_ADDRESS

        expected_hash = WithdrawalProcessor._expected_tx_hash(TEST_SIGNED_TX)
        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=1)
        mock_dest_web3.eth.get_transaction_receipt = _hash_lookup({})
        mock_dest_web3.eth.get_transaction = _hash_lookup({})
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawalCount.return_value.call.return_value = 1
        contract_reader.functions.withdrawals.return_value.call.return_value = _resolved_withdrawal(
            nonce=1
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        mock_context = MagicMock()
        mock_context.chain_id = TEST_CHAIN_ID
        processor.accounting_service._get_token_context = AsyncMock(return_value=mock_context)

        processor.accounting_service._send_raw_transaction.side_effect = Exception("nonce too low")

        with patch("src.services.withdrawal_processor.asyncio.sleep", new_callable=AsyncMock):
            await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        # Both lookups were tried for our hash, and neither answered
        mock_dest_web3.eth.get_transaction_receipt.assert_any_await(expected_hash)
        mock_dest_web3.eth.get_transaction.assert_any_await(expected_hash)

    @pytest.mark.asyncio
    async def test_process_chain_refuses_when_contract_nonce_behind_chain(self, processor):
        """B3: a fresh chain whose evmAddress has already transacted must not be
        processed - the contract would sign spent nonces."""
        processor._is_running = True
        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=0)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=3)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()

        await processor._process_chain(TEST_CHAIN_ID, [{"index": 0, "chain_id": TEST_CHAIN_ID}])

        # Nothing signed, nothing resolved, no state advanced
        contract_reader.functions.withdrawals.assert_not_called()
        processor.accounting_service.resolve_withdrawal.assert_not_called()
        processor.accounting_service._send_raw_transaction.assert_not_called()
        assert processor._chain_high_water_mark == {}
        # The pending count is what matters: a queued tx has already spent its nonce
        mock_dest_web3.eth.get_transaction_count.assert_any_await(TEST_USER_ADDRESS, "pending")

    @pytest.mark.asyncio
    async def test_catch_up_refuses_when_contract_nonce_behind_chain(self, processor):
        """The same gate stops the catch-up pass before it scans or broadcasts."""
        processor.settings.chain_rpc_urls = {TEST_CHAIN_ID: "https://example.com"}

        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=1)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=4)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()

        await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        contract_reader.functions.withdrawalCount.assert_not_called()
        processor.accounting_service._send_raw_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_chain_proceeds_when_nonces_match(self, processor):
        """Matched nonces are the ready state: the chain processes normally."""
        processor._is_running = True
        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=4)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=4)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = _resolved_withdrawal(
            nonce=4
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.return_value = TEST_TX_HASH

        await processor._process_chain(TEST_CHAIN_ID, [{"index": 7, "chain_id": TEST_CHAIN_ID}])

        processor.accounting_service._send_raw_transaction.assert_called_once_with(
            TEST_CHAIN_ID, TEST_SIGNED_TX
        )
        assert processor._chain_high_water_mark[TEST_CHAIN_ID] == 7

    @pytest.mark.asyncio
    async def test_process_chain_allows_contract_nonce_ahead_of_chain(self, processor):
        """The gate is one-directional: a contract nonce ahead of the chain is an
        un-broadcast backlog, not a hazard."""
        processor._is_running = True
        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=6)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=4)
        processor._destination_web3[TEST_CHAIN_ID] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawals.return_value.call.return_value = _resolved_withdrawal(
            nonce=6
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX
        processor.accounting_service._send_raw_transaction.return_value = TEST_TX_HASH

        await processor._process_chain(TEST_CHAIN_ID, [{"index": 9, "chain_id": TEST_CHAIN_ID}])

        processor.accounting_service._send_raw_transaction.assert_called_once()
        assert processor._chain_high_water_mark[TEST_CHAIN_ID] == 9


class TestWithdrawalAdmission:
    """Per-chain gas admission for withdrawals (`_check_destination_balance`)."""

    SAPPHIRE_GAS_PRICE = 100_000_000_000  # 100 gwei, as published for chain 23295
    SAPPHIRE_CHAIN_ID = 23295
    ERC20_GAS_LIMIT = 100_000  # gasLimitERC20Withdraw
    NATIVE_GAS_LIMIT = 50_000  # gasLimitNativeWithdraw
    GLOBAL_FLOOR = 10_000_000_000_000  # MIN_WITHDRAWAL_GAS_BALANCE, now only a floor

    @staticmethod
    def _make_service(gas_price: int, balance: int, floor: int = 0) -> AccountingContractService:
        service = AccountingContractService.__new__(AccountingContractService)

        reader = MagicMock()
        reader.functions.gasPrices.return_value.call = AsyncMock(return_value=gas_price)
        reader.functions.gasLimitERC20Withdraw.return_value.call = AsyncMock(
            return_value=TestWithdrawalAdmission.ERC20_GAS_LIMIT
        )
        reader.functions.gasLimitNativeWithdraw.return_value.call = AsyncMock(
            return_value=TestWithdrawalAdmission.NATIVE_GAS_LIMIT
        )
        service.contract_reader = reader
        service._withdrawal_gas_limits = {}
        service.settings = MagicMock(min_withdrawal_gas_balance=floor)

        chain_w3 = MagicMock()
        chain_w3.eth.get_balance = AsyncMock(return_value=balance)
        service._get_chain_web3 = AsyncMock(return_value=chain_w3)
        service._get_deposit_address = AsyncMock(return_value=TEST_USER_ADDRESS)
        return service

    @pytest.mark.asyncio
    async def test_rejects_balance_that_only_clears_the_global_floor(self):
        """1e13 wei cleared the old global check but covers 0.1% of a 100 gwei ERC-20
        withdrawal: 100_000 gas x 100 gwei = 1e16 wei, 1.2e16 with the buffer."""
        service = self._make_service(
            self.SAPPHIRE_GAS_PRICE, balance=self.GLOBAL_FLOOR, floor=self.GLOBAL_FLOOR
        )

        with pytest.raises(ValueError, match="needs at least 12000000000000000 wei"):
            await service._check_destination_balance(
                self.SAPPHIRE_CHAIN_ID, is_native=False, amount=10**18
            )

    @pytest.mark.asyncio
    async def test_admits_when_balance_covers_gas_price_times_gas_limit(self):
        service = self._make_service(
            self.SAPPHIRE_GAS_PRICE, balance=12_000_000_000_000_000, floor=self.GLOBAL_FLOOR
        )

        await service._check_destination_balance(
            self.SAPPHIRE_CHAIN_ID, is_native=False, amount=10**18
        )

    @pytest.mark.asyncio
    async def test_native_withdrawal_requires_gas_plus_amount(self):
        gas_required = self.SAPPHIRE_GAS_PRICE * self.NATIVE_GAS_LIMIT * 120 // 100
        amount = 5 * 10**16
        service = self._make_service(self.SAPPHIRE_GAS_PRICE, balance=gas_required + amount - 1)
        reader = service.contract_reader

        with pytest.raises(ValueError, match="Insufficient native balance"):
            await service._check_destination_balance(
                self.SAPPHIRE_CHAIN_ID, is_native=True, amount=amount
            )

        # The native path must read the native limit, never the ERC-20 one
        reader.functions.gasLimitNativeWithdraw.return_value.call.assert_awaited_once()
        reader.functions.gasLimitERC20Withdraw.return_value.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_min_withdrawal_gas_balance_remains_a_floor(self):
        """A near-zero published gas price still requires the configured floor."""
        service = self._make_service(1, balance=10**12, floor=self.GLOBAL_FLOOR)

        with pytest.raises(ValueError, match="needs at least 10000000000000 wei"):
            await service._check_destination_balance(84532, is_native=False, amount=1)

    @pytest.mark.asyncio
    async def test_rejects_chain_without_published_gas_price(self):
        """No gasPrices(chainId) means the contract cannot sign; admit nothing."""
        service = self._make_service(0, balance=10**18, floor=self.GLOBAL_FLOOR)

        with pytest.raises(ValueError, match="No gas price published"):
            await service._check_destination_balance(
                self.SAPPHIRE_CHAIN_ID, is_native=False, amount=1
            )

    @pytest.mark.asyncio
    async def test_gas_limit_is_read_from_the_contract_and_cached(self):
        """The limit comes from the contract getter, never a Python mirror."""
        service = self._make_service(self.SAPPHIRE_GAS_PRICE, balance=10**18)
        reader = service.contract_reader

        for _ in range(2):
            await service._check_destination_balance(
                self.SAPPHIRE_CHAIN_ID, is_native=False, amount=1
            )

        reader.functions.gasLimitERC20Withdraw.return_value.call.assert_awaited_once()
        assert service._withdrawal_gas_limits == {"gasLimitERC20Withdraw": self.ERC20_GAS_LIMIT}
