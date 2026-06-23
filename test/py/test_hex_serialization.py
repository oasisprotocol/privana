"""Tests for hex serialization in private-read helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from hexbytes import HexBytes
from web3 import Web3

from src.services.accounting_contract import (
    AccountingContractService,
    _to_prefixed_hex,
)


class TestHexSerialization:
    """Regression tests for 0x-prefixed response fields."""

    def test_to_prefixed_hex_returns_0x_lowercase(self):
        assert _to_prefixed_hex(HexBytes("0xABCD")) == "0xabcd"
        assert _to_prefixed_hex("abcd") == "0xabcd"
        assert _to_prefixed_hex(b"\xab\xcd") == "0xabcd"

    @pytest.mark.asyncio
    async def test_siwe_login_returns_prefixed_token(self):
        service = AccountingContractService.__new__(AccountingContractService)
        contract_reader = MagicMock()
        contract_reader.functions.login.return_value.call = AsyncMock(return_value=b"\x12\x34")
        service._get_confidential_siwe_auth_reader_contract = AsyncMock(
            return_value=contract_reader
        )
        service._parse_signature_rsv = MagicMock(return_value=(b"\x00" * 32, b"\x00" * 32, 27))

        result = await service.siwe_login("message", "0x" + "00" * 65)

        assert result["token"] == "0x1234"

    @pytest.mark.asyncio
    async def test_get_batch_balances_returns_prefixed_token_ids(self):
        service = AccountingContractService.__new__(AccountingContractService)
        token_id_input = "0x" + "AB" * 32
        token_hex = HexBytes(token_id_input)

        contract_reader = MagicMock()
        contract_reader.functions.balanceOf.return_value.call = AsyncMock(return_value=7)

        service._require_hex = MagicMock(return_value=token_hex)
        service._get_confidential_reader_contract = AsyncMock(return_value=contract_reader)
        service._fetch_balance = AsyncMock(return_value=7)
        service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=23295))
        service._get_token_symbol = AsyncMock(return_value="TEST")

        result = await service.get_batch_balances(b"", [token_id_input])

        assert result["balances"][0]["token_id"] == token_id_input.lower()

    def test_lock_to_info_returns_prefixed_token_id(self):
        service = AccountingContractService.__new__(AccountingContractService)
        service_id = Web3.to_checksum_address("0x3333333333333333333333333333333333333333")
        token_bytes = bytes.fromhex("ab" * 32)

        result = service._lock_to_info((1, service_id, token_bytes, 100, 1000), 500)

        assert result["token_id"] == "0x" + "ab" * 32
        assert result["amount"] == "100"

    @pytest.mark.asyncio
    async def test_get_balance_returns_prefixed_token_id(self):
        service = AccountingContractService.__new__(AccountingContractService)
        siwe_token = b"\x44" * 32
        token_hex = HexBytes("0x" + "ab" * 32)

        contract_reader = MagicMock()
        contract_reader.functions.balanceOf.return_value.call = AsyncMock(return_value=9)

        service._require_hex = MagicMock(return_value=token_hex)
        service._get_confidential_reader_contract = AsyncMock(return_value=contract_reader)
        service._fetch_balance = AsyncMock(return_value=9)
        service._get_token_context = AsyncMock(return_value=SimpleNamespace(chain_id=23295))
        service._get_token_symbol = AsyncMock(return_value="TEST")

        result = await service.get_balance(siwe_token, "ab" * 32)

        assert result["token_id"] == "0x" + "ab" * 32
        service._fetch_balance.assert_awaited_once_with(token_hex, siwe_token)

    @pytest.mark.asyncio
    async def test_get_total_locked_balance_returns_prefixed_token_id(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x5555555555555555555555555555555555555555")
        siwe_token = b"\x55" * 32
        token_hex = HexBytes("0x" + "ab" * 32)
        other_token = bytes.fromhex("cd" * 32)

        contract_reader = MagicMock()
        contract_reader.functions.getUserLocks.return_value.call = AsyncMock(
            return_value=[
                (1, user, bytes(token_hex), 10, 1000),
                (2, user, other_token, 20, 1000),
            ]
        )

        service._require_hex = MagicMock(return_value=token_hex)
        service._get_confidential_reader_contract = AsyncMock(return_value=contract_reader)
        service._fetch_user_locks = AsyncMock(
            return_value=[
                (1, user, bytes(token_hex), 10, 1000),
                (2, user, other_token, 20, 1000),
            ]
        )

        result = await service.get_total_locked_balance(siwe_token, "0x" + "ab" * 32)

        assert result["token_id"] == "0x" + "ab" * 32
        assert result["total_locked"] == "10"
        service._fetch_user_locks.assert_awaited_once_with(siwe_token)
