"""Pydantic models describing Accounting API payloads."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Optional

from pydantic import BaseModel, Field, field_validator


def _normalise_hex(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x"):
        value = "0x" + value
    return value


def _parse_int_amount(value: int | str | float) -> int:
    """Parse amount from int, string, or scientific notation.

    For string inputs, tries direct int conversion first to preserve precision
    for large integers. Uses Decimal for scientific notation or decimal strings
    to avoid float precision loss.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        # Try direct int conversion first (preserves precision for large ints)
        # Use Decimal for scientific notation or decimals to preserve precision
        if "e" in value.lower() or "." in value:
            return int(Decimal(value))
        return int(value)
    return int(value)


class DepositQuoteRequest(BaseModel):
    """Request parameters for generating a deposit quote."""

    user_address: str = Field(..., description="User wallet address on source chain", min_length=1)
    token_id: str = Field(..., description="Bytes32 token identifier (hex string)", min_length=4)
    amount: int = Field(
        ..., description="Amount to deposit in token's base units (e.g. wei for ETH)", gt=0
    )

    @field_validator("amount", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

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
    amount: str
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
    nonce: int = Field(..., ge=0)
    signature: str

    @field_validator("amount", "expiry", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

    @field_validator("token_id", "signature")
    def _normalise_hex_fields(cls, value: str) -> str:
        return _normalise_hex(value)


class ModifyLockRequest(BaseModel):
    """Payload for modifying an existing lock (add funds and/or extend expiry)."""

    user_address: str = Field(..., min_length=1)
    lock_id: int = Field(..., ge=1)
    amount: int = Field(..., ge=0)
    new_expiry: int = Field(..., gt=0)
    nonce: int = Field(..., ge=0)
    signature: str

    @field_validator("amount", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

    @field_validator("signature")
    def _normalise_ml_signature(cls, value: str) -> str:
        return _normalise_hex(value)


class TransferFundsRequest(BaseModel):
    """Payload for transferring funds between users."""

    user_address: str = Field(..., min_length=1)
    to_address: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=4)
    amount: int = Field(..., gt=0)
    nonce: int = Field(..., ge=0)
    signature: str

    @field_validator("amount", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

    @field_validator("token_id", "signature")
    def _normalise_tf_hex_fields(cls, value: str) -> str:
        return _normalise_hex(value)


class TransferLockedFundsRequest(BaseModel):
    """Payload for transferring locked funds."""

    user_address: str = Field(..., min_length=1)
    lock_id: int = Field(..., ge=1)
    to_address: str = Field(..., min_length=1)
    amount: int = Field(..., gt=0)
    service_address: str = Field(..., min_length=1)
    nonce: int = Field(..., ge=0)
    signature: str

    @field_validator("amount", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

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
    nonce: int = Field(..., ge=0)
    signature: str

    @field_validator("amount", "nonce", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

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


class UnlockAllExpiredLocksRequest(BaseModel):
    """Payload for unlocking all expired locks for a user."""

    user_address: str


class TokenInfoResponse(BaseModel):
    """Response containing token information."""

    token_id: str
    token_type: int
    token_type_name: str
    data: str
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    token_address: Optional[str] = None


class BalanceResponse(BaseModel):
    """Response containing a user's balance for a specific token."""

    user_address: str
    token_id: str
    balance: str
    token_symbol: str
    chain_id: str


class TokenBalance(BaseModel):
    """Balance info for a single token (used in batch balance responses)."""

    token_id: str
    balance: str
    token_symbol: str
    chain_id: str


class BatchBalancesRequest(BaseModel):
    """Request payload for querying multiple token balances for a user."""

    MAX_TOKEN_IDS: ClassVar[int] = 100

    user_address: str = Field(..., min_length=1)
    token_ids: list[str] = Field(..., min_length=1, max_length=MAX_TOKEN_IDS)

    @field_validator("user_address")
    def _normalise_user_address(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("token_ids")
    def _normalise_token_ids(cls, value: list[str]) -> list[str]:
        return [_normalise_hex(item) for item in value]


class BatchBalancesResponse(BaseModel):
    """Response containing balances for multiple tokens."""

    user_address: str
    balances: list[TokenBalance]


class LockInfo(BaseModel):
    """Public shape of a lock returned by the API."""

    lock_id: int
    user_address: str
    service_address: str
    token_id: str
    amount: str
    expiry: int
    is_expired: bool


class LockedFundsResponse(BaseModel):
    """Response containing active locks for a user, optionally filtered by service."""

    user_address: str
    service_address: Optional[str] = None
    locks: list[LockInfo]
    total_locked: str


class ExpiredLocksResponse(BaseModel):
    """Response containing expired locks for a user."""

    user_address: str
    expired_locks: list[LockInfo]


class TotalLockedBalanceResponse(BaseModel):
    """Response containing total locked balance for a token."""

    user_address: str
    token_id: str
    total_locked: str


class SiweLoginRequest(BaseModel):
    """Request payload for SIWE login."""

    siwe_message: str = Field(..., min_length=1)
    signature: str = Field(..., min_length=4)

    @field_validator("signature")
    def _normalise_siwe_signature(cls, value: str) -> str:
        return _normalise_hex(value)


class SiweLoginResponse(BaseModel):
    """Response containing an opaque SIWE authentication token."""

    token: str


class SiweDomainResponse(BaseModel):
    """Response containing the SIWE domain configured in the contract."""

    domain: str


class WithdrawalNonceResponse(BaseModel):
    """Response containing the current withdrawal nonce for a user."""

    user_address: str
    nonce: int


class TransferNonceResponse(BaseModel):
    """Response containing the current transfer nonce for a user."""

    user_address: str
    nonce: int


class LockNonceResponse(BaseModel):
    """Response containing the current createLock nonce for a user."""

    user_address: str
    nonce: int


class ModifyLockNonceResponse(BaseModel):
    """Response containing the current modifyLock nonce for a user."""

    user_address: str
    nonce: int


class TransferLockedNonceResponse(BaseModel):
    """Response containing the current transferFromLock nonce for a service."""

    service_address: str
    nonce: int
