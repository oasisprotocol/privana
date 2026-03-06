"""Tests for AccountingContractService parsing and request validation."""

from unittest.mock import MagicMock

import pytest

from src.services.accounting_contract import AccountingContractService


def _make_service_with_reader(reader: MagicMock) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_reader = reader
    return service


def test_get_withdrawal_parses_new_tuple_shape_with_to_address() -> None:
    user = "0x1234567890123456789012345678901234567890"
    to_address = "0x9876543210987654321098765432109876543210"
    token = bytes.fromhex("11" * 32)
    tx_identifier = b"\x12\x34"

    reader = MagicMock()
    reader.functions.withdrawals.return_value.call.return_value = (
        user,
        to_address,
        42,
        777,
        token,
        False,
        tx_identifier,
    )

    service = _make_service_with_reader(reader)
    parsed = service.get_withdrawal(3)

    assert parsed["index"] == 3
    assert parsed["user_address"] == user
    assert parsed["to_address"] == to_address
    assert parsed["amount"] == "42"
    assert parsed["block_number"] == 777
    assert parsed["token_id"] == "0x" + ("11" * 32)
    assert parsed["resolved"] is False
    assert parsed["tx_identifier"] == "0x1234"


def test_get_pending_withdrawals_includes_to_address() -> None:
    user = "0x1234567890123456789012345678901234567890"
    to_address = "0x9876543210987654321098765432109876543210"
    token = bytes.fromhex("22" * 32)
    tx_identifier = b"\x56\x78"

    reader = MagicMock()

    def _withdrawals(index: int):
        result = MagicMock()
        if index == 0:
            result.call.return_value = (
                user,
                to_address,
                99,
                1234,
                token,
                False,
                tx_identifier,
            )
        else:
            result.call.side_effect = Exception("end")
        return result

    reader.functions.withdrawals.side_effect = _withdrawals

    service = _make_service_with_reader(reader)
    parsed = service.get_pending_withdrawals(user)

    assert parsed["user_address"] == user
    assert len(parsed["pending_withdrawals"]) == 1
    pending = parsed["pending_withdrawals"][0]
    assert pending["to_address"] == to_address
    assert pending["amount"] == "99"
    assert pending["token_id"] == "0x" + ("22" * 32)
    assert pending["resolved"] is False


def test_withdraw_from_lock_rejects_zero_to_address() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="to_address must not be the zero address"):
        service.withdraw_from_lock(
            {
                "user_address": "0x1234567890123456789012345678901234567890",
                "to_address": "0x0000000000000000000000000000000000000000",
                "lock_id": 1,
                "amount": 10,
                "nonce": 0,
                "signature": "0x1234",
            }
        )


def test_credit_deposit_to_rejects_zero_beneficiary() -> None:
    service = AccountingContractService.__new__(AccountingContractService)

    with pytest.raises(ValueError, match="beneficiary_address must not be the zero address"):
        service.credit_deposit_to(
            {
                "depositor_address": "0x1234567890123456789012345678901234567890",
                "beneficiary_address": "0x0000000000000000000000000000000000000000",
                "token_id": "0x" + ("11" * 32),
                "nonce": 0,
                "depositor_signature": "0x1234",
                "rlp_block_header": "0x11",
                "transaction_index_rlp": "0x22",
                "transaction_proof_stack": "0x33",
                "receipt_index_rlp": "0x44",
                "receipt_proof_stack": "0x55",
            }
        )
