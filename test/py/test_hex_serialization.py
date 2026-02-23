"""Tests for hex serialization in private-read helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from hexbytes import HexBytes
from web3 import Web3

from src.services.accounting_contract import AccountingContractService, _to_prefixed_hex


class TestHexSerialization:
    """Regression tests for 0x-prefixed response fields."""

    def test_to_prefixed_hex_returns_0x_lowercase(self):
        assert _to_prefixed_hex(HexBytes("0xABCD")) == "0xabcd"
        assert _to_prefixed_hex("abcd") == "0xabcd"
        assert _to_prefixed_hex(b"\xab\xcd") == "0xabcd"

    def test_siwe_login_returns_prefixed_token(self):
        service = AccountingContractService.__new__(AccountingContractService)
        contract_reader = MagicMock()
        contract_reader.functions.login.return_value.call.return_value = b"\x12\x34"
        service._get_confidential_siwe_auth_reader_contract = MagicMock(
            return_value=contract_reader
        )
        service._parse_signature_rsv = MagicMock(return_value=(b"\x00" * 32, b"\x00" * 32, 27))

        result = service.siwe_login("message", "0x" + "00" * 65)

        assert result["token"] == "0x1234"

    def test_get_batch_balances_returns_prefixed_token_ids(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
        token_id_input = "0x" + "AB" * 32
        token_hex = HexBytes(token_id_input)

        contract_reader = MagicMock()
        contract_reader.functions.balanceOf.return_value.call.return_value = 7

        service._require_address = MagicMock(return_value=user)
        service._require_hex = MagicMock(return_value=token_hex)
        service._get_confidential_reader_contract = MagicMock(return_value=contract_reader)
        service._get_token_context = MagicMock(return_value=SimpleNamespace(chain_id=23295))
        service._get_token_symbol = MagicMock(return_value="TEST")

        result = service.get_batch_balances(user, [token_id_input], b"")

        assert result["balances"][0]["token_id"] == token_id_input.lower()

    def test_lock_to_info_returns_prefixed_token_id(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")
        service_id = Web3.to_checksum_address("0x3333333333333333333333333333333333333333")
        token_bytes = bytes.fromhex("ab" * 32)

        result = service._lock_to_info(user, (1, service_id, token_bytes, 100, 1000), 500)

        assert result["token_id"] == "0x" + "ab" * 32

    def test_get_balance_returns_prefixed_token_id(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x4444444444444444444444444444444444444444")
        token_hex = HexBytes("0x" + "ab" * 32)

        contract_reader = MagicMock()
        contract_reader.functions.balanceOf.return_value.call.return_value = 9

        service._require_address = MagicMock(return_value=user)
        service._require_hex = MagicMock(return_value=token_hex)
        service._get_confidential_reader_contract = MagicMock(return_value=contract_reader)
        service._get_token_context = MagicMock(return_value=SimpleNamespace(chain_id=23295))
        service._get_token_symbol = MagicMock(return_value="TEST")

        result = service.get_balance(user, "ab" * 32, b"")

        assert result["token_id"] == "0x" + "ab" * 32

    def test_get_total_locked_balance_returns_prefixed_token_id(self):
        service = AccountingContractService.__new__(AccountingContractService)
        user = Web3.to_checksum_address("0x5555555555555555555555555555555555555555")
        token_hex = HexBytes("0x" + "ab" * 32)
        other_token = bytes.fromhex("cd" * 32)

        contract_reader = MagicMock()
        contract_reader.functions.getUserLocks.return_value.call.return_value = [
            (1, user, bytes(token_hex), 10, 1000),
            (2, user, other_token, 20, 1000),
        ]

        service._require_address = MagicMock(return_value=user)
        service._require_hex = MagicMock(return_value=token_hex)
        service._get_confidential_reader_contract = MagicMock(return_value=contract_reader)

        result = service.get_total_locked_balance(user, "0x" + "ab" * 32, b"")

        assert result["token_id"] == "0x" + "ab" * 32
        assert result["total_locked"] == "10"
