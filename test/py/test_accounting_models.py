"""Tests for accounting request/response models introduced by lock withdrawal flows."""

from src.models.accounting import (
    CreditDepositToRequest,
    IncludeDepositRequest,
    WithdrawFromLockRequest,
)


def test_withdraw_from_lock_request_parses_amount_and_normalizes_signature() -> None:
    model = WithdrawFromLockRequest(
        user_address="0x1234567890123456789012345678901234567890",
        to_address="0x9876543210987654321098765432109876543210",
        lock_id=1,
        amount="1e3",
        nonce="2",
        signature="ABCD",
    )

    assert model.amount == 1000
    assert model.nonce == 2
    assert model.signature == "0xabcd"


def test_credit_deposit_to_request_normalizes_all_hex_fields() -> None:
    model = CreditDepositToRequest(
        depositor_address="0x1234567890123456789012345678901234567890",
        beneficiary_address="0x9876543210987654321098765432109876543210",
        token_id="AA" * 32,
        nonce="3",
        depositor_signature="BEEF",
        rlp_block_header="11",
        transaction_index_rlp="22",
        transaction_proof_stack="33",
        receipt_index_rlp="44",
        receipt_proof_stack="55",
    )

    assert model.token_id == "0x" + ("aa" * 32)
    assert model.nonce == 3
    assert model.depositor_signature == "0xbeef"
    assert model.rlp_block_header == "0x11"
    assert model.transaction_index_rlp == "0x22"
    assert model.transaction_proof_stack == "0x33"
    assert model.receipt_index_rlp == "0x44"
    assert model.receipt_proof_stack == "0x55"


def test_include_deposit_request_accepts_receipt_proof_fields() -> None:
    model = IncludeDepositRequest(
        user_address="0x1234567890123456789012345678901234567890",
        token_id="AA" * 32,
        evm_transaction_data="1234",
        rlp_block_header="11",
        transaction_index_rlp="22",
        transaction_proof_stack="33",
        receipt_index_rlp="44",
        receipt_proof_stack="55",
    )

    assert model.receipt_index_rlp == "0x44"
    assert model.receipt_proof_stack == "0x55"
