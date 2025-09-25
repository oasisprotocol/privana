"""Pydantic models describing Accounting API payloads."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _normalise_hex(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x"):
        value = "0x" + value
    return value


class DepositQuoteRequest(BaseModel):
    """Request parameters for generating a deposit quote."""

    user_address: str = Field(
        ..., description="User wallet address on source chain", min_length=1
    )
    token_id: str = Field(
        ..., description="Bytes32 token identifier (hex string)", min_length=4
    )
    amount: int = Field(
        ..., description="Amount to deposit in token's base units (e.g. wei for ETH)", gt=0
    )

    @field_validator("user_address", "token_id")
    def _lowercase(cls, value: str) -> str:  # noqa: D401 - simple normaliser
        return value.lower()


class TransactionData(BaseModel):
    """Transaction data for executing a deposit."""

    to: str = Field(..., description="Destination address for the transaction")
    value: str = Field(..., description="Value to send in wei (as hex string)")
    data: str = Field(..., description="Transaction data (hex string)")
    chain_id: int = Field(..., description="Chain ID for the transaction")


class DepositQuoteResponse(BaseModel):
    """Information describing how and where to deposit funds."""

    user_address: str
    token_id: str
    amount: int
    deposit_address: str
    transaction: TransactionData
    instructions: str


class IncludeDepositRequest(BaseModel):
    """Base payload for including an observed deposit."""

    user_address: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=4)
    evm_transaction_data: str = Field(..., description="RLP encoded transaction payload (hex)")
    rlp_block_header: Optional[str] = Field(
        None, description="Optional RLP block header proof (hex)"
    )
    transaction_index_rlp: Optional[str] = Field(
        None, description="Optional RLP encoded tx index proof (hex)"
    )
    transaction_proof_stack: Optional[str] = Field(
        None, description="Optional proof stack data (hex)"
    )

    @field_validator("token_id", "evm_transaction_data")
    def _normalise_required_hex(cls, value: str) -> str:
        return _normalise_hex(value)

    @field_validator(
        "rlp_block_header",
        "transaction_index_rlp",
        "transaction_proof_stack",
    )
    def _normalise_optional_hex(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return _normalise_hex(value)


class IncludeDepositResponse(BaseModel):
    """Response after submitting a deposit inclusion transaction."""

    submission_id: str
    status: str


class LockFundsRequest(BaseModel):
    """Payload for locking user funds for a service."""

    user_address: str = Field(..., min_length=1)
    service_address: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=4)
    amount: int = Field(..., gt=0)
    expiry: int = Field(..., gt=0)
    signature: str

    @field_validator("token_id", "signature")
    def _normalise_hex_fields(cls, value: str) -> str:
        return _normalise_hex(value)


class TransferFundsRequest(BaseModel):
    """Payload for transferring funds between users."""

    user_address: str = Field(..., min_length=1)
    to_address: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=4)
    amount: int = Field(..., gt=0)
    signature: str

    @field_validator("token_id", "signature")
    def _normalise_tf_hex_fields(cls, value: str) -> str:
        return _normalise_hex(value)


class TransferLockedFundsRequest(BaseModel):
    """Payload for transferring locked funds."""

    user_address: str = Field(..., min_length=1)
    lock_index: int = Field(..., ge=0)
    to_address: str = Field(..., min_length=1)
    amount: int = Field(..., gt=0)
    signature: str

    @field_validator("signature")
    def _normalise_tl_signature(cls, value: str) -> str:
        return _normalise_hex(value)


class UnlockFundsRequest(BaseModel):
    """Payload for unlocking funds after expiry."""

    user_address: str
    lock_index: int


class WithdrawalRequest(BaseModel):
    """Payload for requesting a withdrawal transaction."""

    user_address: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=4)
    amount: int = Field(..., gt=0)
    signature: str

    @field_validator("token_id", "signature")
    def _normalise_withdraw_hex(cls, value: str) -> str:
        return _normalise_hex(value)


class TransactionSubmissionResponse(BaseModel):
    """Generic response for contract transaction submissions."""

    submission_id: str
    status: str
    detail: Optional[str] = None


class LockInfo(BaseModel):
    """Information about a single fund lock."""

    lock_index: int
    user_address: str
    service_address: str
    token_id: str
    amount: int
    expiry: int
    is_expired: bool


class LockedFundsResponse(BaseModel):
    """Response containing user's locked funds information."""

    user_address: str
    service_address: Optional[str] = Field(None, description="Filter by service address if provided")
    locks: list[LockInfo]
    total_locked: int
