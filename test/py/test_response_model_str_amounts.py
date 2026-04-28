"""Tests for str serialization of token amounts in response models.

Issue #47: Response models must serialize token amounts as strings to prevent
JavaScript precision loss for integers above 2^53 - 1.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from web3 import Web3

from src.models.accounting import DepositQuoteResponse, LockedFundsResponse, LockInfo
from src.services.accounting_contract import AccountingContractService


class TestResponseModelStrAmounts:
    """Verify response models serialize token amounts as str, not int."""

    def test_deposit_quote_response_amount_is_str(self):
        resp = DepositQuoteResponse(
            user_address="0x1111111111111111111111111111111111111111",
            token_id="0x" + "ab" * 32,
            amount="1000000000000000000",
            deposit_address="0x2222222222222222222222222222222222222222",
            transaction={
                "to": "0x2222222222222222222222222222222222222222",
                "value": "0x0",
                "data": "0x",
                "chain_id": 84532,
            },
            instructions="Send native tokens.",
        )
        assert isinstance(resp.amount, str)
        assert resp.amount == "1000000000000000000"

    def test_deposit_quote_response_preserves_large_amount(self):
        large = "123456789012345678901234567890"
        resp = DepositQuoteResponse(
            user_address="0x1111111111111111111111111111111111111111",
            token_id="0x" + "ab" * 32,
            amount=large,
            deposit_address="0x2222222222222222222222222222222222222222",
            transaction={
                "to": "0x2222222222222222222222222222222222222222",
                "value": "0x0",
                "data": "0x",
                "chain_id": 84532,
            },
            instructions="Send native tokens.",
        )
        data = resp.model_dump()
        assert data["amount"] == large

    def test_lock_info_amount_is_str(self):
        info = LockInfo(
            lock_id=1,
            user_address="0x1111111111111111111111111111111111111111",
            service_address="0x2222222222222222222222222222222222222222",
            token_id="0x" + "ab" * 32,
            amount="5000000000000000000",
            expiry=9999999999,
            is_expired=False,
        )
        assert isinstance(info.amount, str)
        assert info.amount == "5000000000000000000"

    def test_locked_funds_response_total_locked_is_str(self):
        resp = LockedFundsResponse(
            user_address="0x1111111111111111111111111111111111111111",
            locks=[],
            total_locked="10000000000000000000",
        )
        assert isinstance(resp.total_locked, str)
        assert resp.total_locked == "10000000000000000000"

    def test_locked_funds_json_serializes_as_string(self):
        resp = LockedFundsResponse(
            user_address="0x1111111111111111111111111111111111111111",
            locks=[
                LockInfo(
                    lock_id=1,
                    user_address="0x1111111111111111111111111111111111111111",
                    service_address="0x2222222222222222222222222222222222222222",
                    token_id="0x" + "ab" * 32,
                    amount="1000000000000000000",
                    expiry=9999999999,
                    is_expired=False,
                )
            ],
            total_locked="1000000000000000000",
        )
        data = resp.model_dump()
        assert isinstance(data["total_locked"], str)
        assert isinstance(data["locks"][0]["amount"], str)


class TestServiceLayerStrAmounts:
    """Verify service layer returns str amounts in response dicts."""

    def test_lock_to_info_returns_str_amount(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
        service_addr = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")
        token_bytes = bytes.fromhex("ab" * 32)
        amount = 5 * 10**18

        result = service._lock_to_info(
            user, (1, service_addr, token_bytes, amount, 9999999999), 500
        )

        assert isinstance(result["amount"], str)
        assert result["amount"] == str(amount)

    @pytest.mark.asyncio
    async def test_get_locked_funds_returns_str_total_locked(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
        service_addr = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")
        token_bytes = bytes.fromhex("ab" * 32)

        lock1 = (1, service_addr, token_bytes, 3 * 10**18, 9999999999)
        lock2 = (2, service_addr, token_bytes, 7 * 10**18, 9999999999)

        contract_reader = MagicMock()
        contract_reader.functions.getUserLocks.return_value.call = AsyncMock(
            return_value=[lock1, lock2]
        )

        service._require_address = MagicMock(return_value=user)
        service._get_confidential_reader_contract = AsyncMock(return_value=contract_reader)
        service._get_chain_timestamp = AsyncMock(return_value=500)

        result = await service.get_locked_funds(user, None, b"")

        assert isinstance(result["total_locked"], str)
        assert result["total_locked"] == str(10 * 10**18)
        assert isinstance(result["locks"][0]["amount"], str)
        assert isinstance(result["locks"][1]["amount"], str)

    @pytest.mark.asyncio
    async def test_get_locked_funds_total_is_sum_not_concatenation(self):
        """Guard against total_locked being string concatenation instead of numeric sum."""
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
        service_addr = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")
        token_bytes = bytes.fromhex("ab" * 32)

        lock1 = (1, service_addr, token_bytes, 100, 9999999999)
        lock2 = (2, service_addr, token_bytes, 200, 9999999999)

        contract_reader = MagicMock()
        contract_reader.functions.getUserLocks.return_value.call = AsyncMock(
            return_value=[lock1, lock2]
        )

        service._require_address = MagicMock(return_value=user)
        service._get_confidential_reader_contract = AsyncMock(return_value=contract_reader)
        service._get_chain_timestamp = AsyncMock(return_value=500)

        result = await service.get_locked_funds(user, None, b"")

        # Must be "300" (numeric sum), NOT "100200" (string concatenation)
        assert result["total_locked"] == "300"

    @pytest.mark.asyncio
    async def test_get_expired_locks_returns_str_amounts(self):
        """get_expired_locks also uses _lock_to_info — verify amounts are str."""
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
        service_addr = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")
        token_bytes = bytes.fromhex("ab" * 32)
        now = 9999999999 + 1  # after expiry

        expired_lock = (1, service_addr, token_bytes, 2 * 10**18, 9999999999)

        contract_reader = MagicMock()
        contract_reader.functions.getUserLocks.return_value.call = AsyncMock(
            return_value=[expired_lock]
        )

        service._require_address = MagicMock(return_value=user)
        service._get_confidential_reader_contract = AsyncMock(return_value=contract_reader)
        service._get_chain_timestamp = AsyncMock(return_value=now)

        result = await service.get_expired_locks(user, b"")

        assert len(result["expired_locks"]) == 1
        assert isinstance(result["expired_locks"][0]["amount"], str)
        assert result["expired_locks"][0]["amount"] == str(2 * 10**18)
