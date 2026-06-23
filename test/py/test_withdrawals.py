"""Tests for withdrawal processing service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode

from src.services.custody_tx_executor import (
    CustodyTxKind,
    CustodyTxRequest,
)
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
        # `resolve_bridge_withdrawal(idx)` is called for bridge records in
        # both the live and catch-up paths. Default mock returns the same
        # signed-tx blob as the legacy resolveWithdrawal.
        service.resolve_bridge_withdrawal = AsyncMock(return_value=TEST_SIGNED_TX)
        # Return a mock contract with async call methods
        mock_contract = MagicMock()
        mock_contract.functions.withdrawals.return_value.call = AsyncMock()
        mock_contract.functions.resolveWithdrawal.return_value.call = AsyncMock()
        mock_contract.functions.withdrawalCount.return_value.call = AsyncMock()
        service._get_reader_contract = MagicMock(return_value=mock_contract)
        service._get_token_context = AsyncMock()
        return service

    @pytest.fixture
    def mock_custody_executor(self):
        """Create a mock custody-tx executor.

        Default behavior: no prior record exists (get_record → None), and
        enqueue succeeds silently. Tests can override either by re-assigning
        the relevant AsyncMock.
        """
        executor = MagicMock()
        executor.get_record = MagicMock(return_value=None)
        executor.enqueue = AsyncMock(return_value="dummy-key")
        return executor

    @pytest.fixture
    def processor(self, mock_accounting_service, mock_custody_executor):
        """Create a WithdrawalProcessor with mocked dependencies."""
        with patch("src.services.withdrawal_processor.load_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                withdrawal_poll_interval=1,
                withdrawal_resolution_timeout=1,
                sapphire_rpc_url="https://testnet.sapphire.oasis.io",
                accounting_contract_address="0x" + "ab" * 20,
                chain_rpc_urls={TEST_CHAIN_ID: "https://example.com"},
                sapphire_chain_id=23295,
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

                        proc = WithdrawalProcessor(custody_executor=mock_custody_executor)
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
    async def test_bridge_asset_record_groups_into_decoded_chain(self, processor):
        """Bridge records carrying a decoded destChainId group correctly."""
        base_chain_id = 84532
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {
                    "index": 0,
                    "block_number": 50,
                    "chain_id": base_chain_id,
                    "dest_tx_nonce": 7,
                    "route_address": "0x" + "bb" * 20,
                    "max_gas_cost": 0,
                    "is_bridge_asset": True,
                },
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        assert list(result.keys()) == [base_chain_id]
        record = result[base_chain_id][0]
        assert record["dest_tx_nonce"] == 7
        assert record["is_bridge_asset"] is True
        # Cross-layer naming: bridge records never expose a generic "nonce" key.
        assert "nonce" not in record

    @pytest.mark.asyncio
    async def test_bridge_asset_missing_decoded_chain_id_logs_invariant(self, processor, caplog):
        """A bridge record without chain_id is a contract violation, not a routing skip."""
        import logging

        caplog.set_level(logging.ERROR)
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {
                    "index": 9,
                    "block_number": 50,
                    "chain_id": None,
                    "is_bridge_asset": True,
                },
            ],
            "current_block": 100,
        }

        result = await processor._get_pending_withdrawals()

        assert result == {}
        assert any(
            "missing decoded destChainId" in rec.message and "#9" in rec.message
            for rec in caplog.records
        )

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
            encode(["uint64"], [7]),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        processor._custody_executor.enqueue.assert_called_once()
        req = processor._custody_executor.enqueue.call_args.args[0]
        assert isinstance(req, CustodyTxRequest)
        assert req.chain_id == TEST_CHAIN_ID
        assert req.evm_nonce == 7
        assert req.kind == CustodyTxKind.NORMAL_WITHDRAWAL
        assert req.id == "0"
        assert req.signed_tx == TEST_SIGNED_TX
        # Non-bridge records must carry withdrawal_index so _get_pending_withdrawals's
        # known_indices filter can dedup them across polls; otherwise we waste an
        # RPC per cycle re-fetching an already-enqueued normal withdrawal.
        assert req.withdrawal_index == 0

    @pytest.mark.asyncio
    async def test_get_pending_skips_chain_when_executor_read_raises(self, processor):
        """If the executor's `get_records_for_chain` raises, the dedup set is
        unknown for that chain. Re-polling anyway re-introduces the duplicate
        records the filter exists to prevent — fail closed instead."""
        broken_chain = 999
        processor.accounting_service.get_all_pending_withdrawals.return_value = {
            "pending": [
                {"index": 0, "block_number": 50, "chain_id": broken_chain},
                {"index": 1, "block_number": 50, "chain_id": TEST_CHAIN_ID},
            ],
            "current_block": 100,
        }

        def _read(cid):
            if cid == broken_chain:
                raise RuntimeError("disk read failed")
            return []

        processor._custody_executor.get_records_for_chain = MagicMock(side_effect=_read)

        result = await processor._get_pending_withdrawals()

        assert broken_chain not in result
        assert TEST_CHAIN_ID in result
        assert [w["index"] for w in result[TEST_CHAIN_ID]] == [1]

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_already_resolved_on_chain(self, processor):
        """Test that an already-resolved withdrawal is enqueued without re-submitting."""
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

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        # resolve_withdrawal (ROFL submission) should NOT have been called
        processor.accounting_service.resolve_withdrawal.assert_not_called()
        # Executor was handed the signed tx exactly once
        processor._custody_executor.enqueue.assert_called_once()

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
        # Should not have tried to enqueue
        processor._custody_executor.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_and_broadcast_idempotent_when_already_enqueued(self, processor):
        """Processor always calls executor.enqueue — the executor itself is
        idempotent at (chain_id, evm_nonce) and refreshes the in-memory
        preflight when a record already exists. Skipping enqueue on the
        processor side would leave a restart-recovered record without a
        live preflight and force it to AWAITING_CLEAR.
        """
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
        existing_record = MagicMock()
        existing_record.status.value = "broadcast"
        processor._custody_executor.get_record = MagicMock(return_value=existing_record)

        withdrawal = {"index": 0, "chain_id": TEST_CHAIN_ID}
        result = await processor._resolve_and_broadcast(withdrawal)

        assert result is True
        processor._custody_executor.enqueue.assert_called_once()

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

        # Should not enqueue anything
        processor._custody_executor.enqueue.assert_not_called()

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

        await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        processor._custody_executor.enqueue.assert_called_once()
        req = processor._custody_executor.enqueue.call_args.args[0]
        assert req.chain_id == TEST_CHAIN_ID
        assert req.evm_nonce == 1
        assert req.kind == CustodyTxKind.NORMAL_WITHDRAWAL

    @pytest.mark.asyncio
    async def test_catch_up_handles_already_enqueued(self, processor):
        """Catch-up is idempotent: when the executor already has a record
        for this (chain_id, evm_nonce), the processor skips the enqueue.
        """
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

        existing_record = MagicMock()
        existing_record.status.value = "broadcast"
        processor._custody_executor.get_record = MagicMock(return_value=existing_record)

        await processor._catch_up_missing_broadcasts([TEST_CHAIN_ID])

        # Catch-up always calls enqueue so the executor can refresh the
        # in-memory preflight (wiped by restart). Executor-side idempotency
        # ensures the on-disk record is not overwritten.
        processor._custody_executor.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_catch_up_finds_and_broadcasts_missing_bridge_asset_base(self, processor):
        """Catch-up routes a BridgeAsset record by decoded destChainId (Base).

        Token context returns chain_id=None for BridgeAsset tokens; the
        catch-up path must derive routing from the txIdentifier itself.
        """
        base_chain_id = 84532
        route_address = "0x" + "Ab" * 20
        processor.settings.chain_rpc_urls = {base_chain_id: "https://example.com"}

        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=8)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=7)
        processor._destination_web3[base_chain_id] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawalCount.return_value.call.return_value = 1
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(
                ["uint256", "uint64", "address", "uint256"],
                [base_chain_id, 7, route_address, 0],
            ),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        # BridgeAsset token context carries chain_id=None — the fix must not
        # rely on it. If the catch-up path were still using token context for
        # routing, this record would be silently skipped.
        mock_context = MagicMock()
        mock_context.chain_id = None
        processor.accounting_service._get_token_context = AsyncMock(return_value=mock_context)

        await processor._catch_up_missing_broadcasts([base_chain_id])

        processor._custody_executor.enqueue.assert_called_once()
        req = processor._custody_executor.enqueue.call_args.args[0]
        assert req.chain_id == base_chain_id
        assert req.evm_nonce == 7
        assert req.kind == CustodyTxKind.BASE_MINT

    @pytest.mark.asyncio
    async def test_catch_up_finds_and_broadcasts_missing_bridge_asset_sapphire(self, processor):
        """Catch-up routes a BridgeAsset record by decoded destChainId (Sapphire)."""
        sapphire_chain_id = 23295
        processor.settings.chain_rpc_urls = {sapphire_chain_id: "https://example.com"}

        processor._contract.functions.nonces.return_value.call = AsyncMock(return_value=4)
        processor._evm_address = TEST_USER_ADDRESS

        mock_dest_web3 = MagicMock()
        mock_dest_web3.eth.get_transaction_count = AsyncMock(return_value=3)
        processor._destination_web3[sapphire_chain_id] = mock_dest_web3

        contract_reader = processor.accounting_service._get_reader_contract()
        contract_reader.functions.withdrawalCount.return_value.call.return_value = 1
        contract_reader.functions.withdrawals.return_value.call.return_value = (
            TEST_USER_ADDRESS,
            TEST_TO_ADDRESS,
            100,
            50,
            b"\x00" * 32,
            True,
            encode(
                ["uint256", "uint64", "address", "uint256"],
                [sapphire_chain_id, 3, "0x" + "00" * 20, 12345],
            ),
        )
        contract_reader.functions.resolveWithdrawal.return_value.call.return_value = TEST_SIGNED_TX

        mock_context = MagicMock()
        mock_context.chain_id = None
        processor.accounting_service._get_token_context = AsyncMock(return_value=mock_context)

        await processor._catch_up_missing_broadcasts([sapphire_chain_id])

        processor._custody_executor.enqueue.assert_called_once()
        req = processor._custody_executor.enqueue.call_args.args[0]
        assert req.chain_id == sapphire_chain_id
        assert req.evm_nonce == 3
        assert req.kind == CustodyTxKind.SAPPHIRE_RELEASE
