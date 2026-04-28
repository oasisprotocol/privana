"""Tests for accounting request/response models introduced by lock withdrawal flows."""

from src.models.accounting import (
    WithdrawFromLockRequest,
)


def test_withdraw_from_lock_request_parses_amount_and_normalizes_signature() -> None:
    model = WithdrawFromLockRequest(
        to_address="0x9876543210987654321098765432109876543210",
        lock_id=1,
        amount="1e3",
        nonce="2",
        signature="ABCD",
    )

    assert model.amount == 1000
    assert model.nonce == 2
    assert model.signature == "0xabcd"
