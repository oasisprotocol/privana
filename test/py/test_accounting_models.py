"""Tests for accounting request/response models."""

import pytest
from pydantic import ValidationError

from src.models.accounting import (
    CreateOnRampSessionRequest,
    WithdrawFromLockRequest,
)


@pytest.mark.parametrize("amount", [None, 1, 12.5])
def test_onramp_quote_default_accepts_optional_positive_numbers(amount) -> None:
    model = CreateOnRampSessionRequest(transaction_id="intent", default_crypto_amount=amount)

    assert model.default_crypto_amount == amount
    assert CreateOnRampSessionRequest(transaction_id="intent").default_crypto_amount is None


@pytest.mark.parametrize(
    "amount", [True, False, "12.5", "", 0, -1, float("inf"), float("-inf"), float("nan")]
)
def test_onramp_quote_default_rejects_nonpositive_nonfinite_and_coerced_values(amount) -> None:
    with pytest.raises(ValidationError):
        CreateOnRampSessionRequest(transaction_id="intent", default_crypto_amount=amount)


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
