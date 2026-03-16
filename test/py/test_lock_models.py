import pytest
from pydantic import ValidationError

from src.models.accounting import (
    LockFundsRequest,
    LockNonceResponse,
    ModifyLockNonceResponse,
    ModifyLockRequest,
    TransferLockedFundsRequest,
    TransferLockedNonceResponse,
)


def test_lock_funds_request_requires_nonce():
    with pytest.raises(ValidationError):
        LockFundsRequest(
            user_address="0x1234567890123456789012345678901234567890",
            service_address="0x1234567890123456789012345678901234567890",
            token_id="0xdeadbeef",
            amount=1000000,
            expiry=9999999999,
            signature="0xabcd",
            # nonce intentionally missing
        )


def test_lock_funds_request_accepts_nonce():
    req = LockFundsRequest(
        user_address="0x1234567890123456789012345678901234567890",
        service_address="0x1234567890123456789012345678901234567890",
        token_id="0xdeadbeef",
        amount=1000000,
        expiry=9999999999,
        nonce=0,
        signature="0xabcd",
    )
    assert req.nonce == 0


def test_lock_funds_request_rejects_negative_nonce():
    with pytest.raises(ValidationError):
        LockFundsRequest(
            user_address="0x1234567890123456789012345678901234567890",
            service_address="0x1234567890123456789012345678901234567890",
            token_id="0xdeadbeef",
            amount=1000000,
            expiry=9999999999,
            nonce=-1,
            signature="0xabcd",
        )


def test_modify_lock_request_requires_nonce():
    with pytest.raises(ValidationError):
        ModifyLockRequest(
            user_address="0x1234567890123456789012345678901234567890",
            lock_id=1,
            amount=0,
            new_expiry=9999999999,
            signature="0xabcd",
            # nonce intentionally missing
        )


def test_modify_lock_request_accepts_nonce():
    req = ModifyLockRequest(
        user_address="0x1234567890123456789012345678901234567890",
        lock_id=1,
        amount=0,
        new_expiry=9999999999,
        nonce=5,
        signature="0xabcd",
    )
    assert req.nonce == 5


def test_transfer_locked_requires_nonce():
    with pytest.raises(ValidationError):
        TransferLockedFundsRequest(
            user_address="0x1234567890123456789012345678901234567890",
            lock_id=1,
            to_address="0x1234567890123456789012345678901234567890",
            amount=1000000,
            service_address="0x1234567890123456789012345678901234567890",
            signature="0xabcd",
            # nonce intentionally missing
        )


def test_transfer_locked_requires_service_address():
    with pytest.raises(ValidationError):
        TransferLockedFundsRequest(
            user_address="0x1234567890123456789012345678901234567890",
            lock_id=1,
            to_address="0x1234567890123456789012345678901234567890",
            amount=1000000,
            nonce=0,
            signature="0xabcd",
            # service_address intentionally missing
        )


def test_transfer_locked_accepts_nonce_and_service():
    req = TransferLockedFundsRequest(
        user_address="0x1234567890123456789012345678901234567890",
        lock_id=1,
        to_address="0x1234567890123456789012345678901234567890",
        amount=1000000,
        service_address="0xabcdef1234567890abcdef1234567890abcdef12",
        nonce=3,
        signature="0xabcd",
    )
    assert req.nonce == 3
    assert req.service_address == "0xabcdef1234567890abcdef1234567890abcdef12"


def test_lock_nonce_response():
    r = LockNonceResponse(user_address="0xabc", nonce=7)
    assert r.nonce == 7


def test_modify_lock_nonce_response():
    r = ModifyLockNonceResponse(user_address="0xabc", nonce=2)
    assert r.nonce == 2


def test_transfer_locked_nonce_response():
    r = TransferLockedNonceResponse(service_address="0xabc", nonce=0)
    assert r.nonce == 0
