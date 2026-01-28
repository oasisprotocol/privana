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


class ModifyLockRequest(BaseModel):
    """Payload for modifying an existing lock (add funds and/or extend expiry)."""

    user_address: str = Field(..., min_length=1)
    lock_id: int = Field(..., ge=1)
    amount: int = Field(..., ge=0)
    new_expiry: int = Field(..., gt=0)
    signature: str

    @field_validator("signature")
    def _normalise_ml_signature(cls, value: str) -> str:
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
    lock_id: int = Field(..., ge=1)
    to_address: str = Field(..., min_length=1)
    amount: int = Field(..., gt=0)
    signature: str

    @field_validator("signature")
    def _normalise_tl_signature(cls, value: str) -> str:
        return _normalise_hex(value)


class UnlockFundsRequest(BaseModel):
    """Payload for unlocking funds after expiry."""

    user_address: str
    lock_id: int = Field(..., ge=1)


class WithdrawalRequest(BaseModel):
    """Payload for requesting a withdrawal transaction."""

    user_address: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=4)
    amount: int = Field(..., gt=0)
    signature: str

    @field_validator("token_id", "signature")
    def _normalise_withdraw_hex(cls, value: str) -> str:
        return _normalise_hex(value)


class WithdrawalInfo(BaseModel):
    """Information about a single withdrawal request."""

    index: int
    user_address: str
    amount: str
    block_number: int
    token_id: str
    resolved: bool
    tx_identifier: str


class WithdrawalInfoResponse(WithdrawalInfo):
    """Response containing withdrawal information."""

    pass


class PendingWithdrawalsResponse(BaseModel):
    """Response containing pending withdrawals for a user."""

    user_address: str
    pending_withdrawals: list[WithdrawalInfo]


class TransactionSubmissionResponse(BaseModel):
    """Generic response for contract transaction submissions."""

    submission_id: str
    status: str
    detail: Optional[str] = None


class LockInfo(BaseModel):
    """Information about a single fund lock."""

    lock_id: int
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


class UnlockAllExpiredLocksRequest(BaseModel):
    """Payload for unlocking all expired locks for a user."""

    user_address: str


class ExpiredLocksResponse(BaseModel):
    """Response containing user's expired locks."""

    user_address: str
    expired_locks: list[LockInfo]


class BatchBalancesRequest(BaseModel):
    """Request for batch balance queries."""

    user_address: str = Field(..., min_length=1)
    token_ids: list[str] = Field(..., min_items=1)

    @field_validator("token_ids")
    def _normalise_token_ids(cls, value: list[str]) -> list[str]:
        return [_normalise_hex(tid) for tid in value]


class TokenBalance(BaseModel):
    """Individual token balance information."""

    token_id: str
    balance: str
    token_symbol: str
    chain_id: str


class BatchBalancesResponse(BaseModel):
    """Response containing multiple token balances."""

    user_address: str
    balances: list[TokenBalance]


class TotalLockedBalanceResponse(BaseModel):
    """Response containing total locked balance for a token."""

    user_address: str
    token_id: str
    total_locked: str


class TokenInfoResponse(BaseModel):
    """Response containing token information."""

    token_id: str
    token_type: int
    token_type_name: str
    data: str
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    token_address: Optional[str] = None


