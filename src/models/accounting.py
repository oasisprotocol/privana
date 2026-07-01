"""Pydantic models describing Accounting API payloads."""

from __future__ import annotations

from decimal import Decimal
from enum import IntEnum
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from web3 import Web3


class ChainType(IntEnum):
    """Chain family used for deposit-address derivation.

    Mirrors the Solidity ``ChainType`` enum in ``Types.sol``. Values must stay
    in sync with the contract: one deposit address serves all chains within
    the same family, so this dimension is intentionally coarser than chainId.
    """

    EVM = 0


class HistoryKind(IntEnum):
    """On-chain user activity kind.

    Mirrors the Solidity ``HistoryKind`` enum in ``Types.sol``. Values must stay
    in sync with the contract: each entry's ``kind`` is the on-chain enum index,
    decoded into the wire-format string via ``HISTORY_KIND_WIRE_NAMES``.
    """

    Deposit = 0
    Withdraw = 1
    CreateLock = 2
    TransferFromLockOut = 3
    TransferFromLockIn = 4
    TransferBalanceOut = 5
    TransferBalanceIn = 6
    ModifyLock = 7
    UnlockLock = 8


HISTORY_KIND_WIRE_NAMES: dict[HistoryKind, str] = {
    HistoryKind.Deposit: "deposit",
    HistoryKind.Withdraw: "withdraw",
    HistoryKind.CreateLock: "createLock",
    HistoryKind.TransferFromLockOut: "transferFromLockOut",
    HistoryKind.TransferFromLockIn: "transferFromLockIn",
    HistoryKind.TransferBalanceOut: "transferBalanceOut",
    HistoryKind.TransferBalanceIn: "transferBalanceIn",
    HistoryKind.ModifyLock: "modifyLock",
    HistoryKind.UnlockLock: "unlockLock",
}


_CHAIN_TYPE_BY_NAME: dict[str, ChainType] = {"evm": ChainType.EVM}


def parse_chain_type(value: str) -> ChainType:
    """Translate a wire-format string (e.g. ``"evm"``) into the on-chain enum value.

    Accepts the lowercase name of a :class:`ChainType` variant; comparison is
    case-insensitive. Raises :class:`ValueError` for unknown values. v1 only
    recognises ``"evm"``.

    Used at the HTTP boundary and at contract-call sites so the magic string
    never leaves the translation layer.
    """
    try:
        return _CHAIN_TYPE_BY_NAME[value.lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"Unknown chain_type: {value!r}") from exc


def _normalise_hex(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x"):
        value = "0x" + value
    return value


def _normalise_fixed_hex(value: str, *, byte_length: int, field_name: str) -> str:
    value = _normalise_hex(value)
    body = value[2:]
    if len(body) != byte_length * 2:
        raise ValueError(f"{field_name} must be {byte_length} bytes")
    try:
        int(body, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be hex") from exc
    return value


def _normalise_currency_code(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


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


class LockFundsRequest(BaseModel):
    """Payload for locking user funds for a service."""

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


class DepositLockAuthorization(BaseModel):
    """Signed policy for locking a deposit after the credited amount is known."""

    service_address: str = Field(..., min_length=1)
    token_id: str
    max_amount: int = Field(..., gt=0)
    min_amount: int = Field(0, ge=0)
    lock_duration: int = Field(..., gt=0)
    authorization_deadline: int = Field(..., gt=0)
    intent_id: str
    signature: str

    @field_validator("service_address")
    def _normalise_service_address(cls, value: str) -> str:
        if not Web3.is_address(value):
            raise ValueError("Invalid service_address")
        checksum = Web3.to_checksum_address(value)
        if checksum.lower() == "0x0000000000000000000000000000000000000000":
            raise ValueError("service_address must not be zero")
        return checksum

    @field_validator(
        "max_amount",
        "min_amount",
        "lock_duration",
        "authorization_deadline",
        mode="before",
    )
    def _parse_numeric_fields(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

    @field_validator("token_id")
    def _normalise_dla_token_id(cls, value: str) -> str:
        return _normalise_fixed_hex(value, byte_length=32, field_name="token_id")

    @field_validator("intent_id")
    def _normalise_dla_intent_id(cls, value: str) -> str:
        intent_id = _normalise_fixed_hex(value, byte_length=32, field_name="intent_id")
        if int(intent_id, 16) == 0:
            raise ValueError("intent_id must not be zero")
        return intent_id

    @field_validator("signature")
    def _normalise_dla_signature(cls, value: str) -> str:
        return _normalise_hex(value)

    @model_validator(mode="after")
    def _validate_amount_bounds(self) -> "DepositLockAuthorization":
        if self.min_amount > self.max_amount:
            raise ValueError("min_amount must not exceed max_amount")
        return self


class ModifyLockRequest(BaseModel):
    """Payload for modifying an existing lock (add funds and/or extend expiry)."""

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


class WithdrawFromLockRequest(BaseModel):
    """Payload for withdrawing funds directly from a lock to an external address."""

    to_address: str = Field(..., min_length=1)
    lock_id: int = Field(..., ge=1)
    amount: int = Field(..., gt=0)
    nonce: int = Field(..., ge=0)
    signature: str

    @field_validator("amount", "nonce", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

    @field_validator("signature")
    def _normalise_wfl_signature(cls, value: str) -> str:
        return _normalise_hex(value)


class UnlockFundsRequest(BaseModel):
    """Payload for unlocking funds after expiry."""

    user_address: str
    lock_id: int = Field(..., ge=1)


class WithdrawalRequest(BaseModel):
    """Payload for requesting a withdrawal transaction."""

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
    to_address: str
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
    symbol: Optional[str] = None
    name: Optional[str] = None
    decimals: Optional[int] = None


class TokenListResponse(BaseModel):
    """Response containing a list of all registered tokens."""

    tokens: list[TokenInfoResponse]


class BalanceResponse(BaseModel):
    """Response containing a user's balance for a specific token."""

    user_address: str
    token_id: str
    balance: str
    token_symbol: Optional[str] = None
    chain_id: str


class HistoryEntry(BaseModel):
    """A single authenticated user activity entry."""

    kind: Literal[
        "deposit",
        "withdraw",
        "createLock",
        "transferFromLockOut",
        "transferFromLockIn",
        "transferBalanceOut",
        "transferBalanceIn",
        "modifyLock",
        "unlockLock",
        "unknown",
    ]
    timestamp: int
    token_id: Optional[str] = None
    amount: Optional[str] = None
    counterparty: Optional[str] = None
    deposit_id: Optional[str] = None
    chain_id: Optional[int] = None


class HistoryResponse(BaseModel):
    """Paginated user history response."""

    history: list[HistoryEntry]
    total: int


class TokenBalance(BaseModel):
    """Balance info for a single token (used in batch balance responses)."""

    token_id: str
    balance: str
    token_symbol: Optional[str] = None
    chain_id: str


class BatchBalancesRequest(BaseModel):
    """Request payload for querying multiple token balances for the authenticated user."""

    MAX_TOKEN_IDS: ClassVar[int] = 100

    token_ids: list[str] = Field(..., min_length=1, max_length=MAX_TOKEN_IDS)

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
    """Response from successful SIWE login.

    Contains both the on-chain SIWE token (for private reads from Sapphire)
    and JWT tokens (for API authentication).
    """

    siwe_token: str = Field(..., description="Encrypted SIWE token for on-chain private reads")
    jwt_access_token: str = Field(..., description="JWT access token for API authentication")
    jwt_refresh_token: str = Field(
        ..., description="JWT refresh token for obtaining new access tokens"
    )
    address: str = Field(..., description="Authenticated Ethereum address")
    jwt_expires_in: int = Field(..., description="Access token expiry in seconds")
    jwt_refresh_expires_in: int = Field(..., description="Refresh token expiry in seconds")


class JwtSiweTokenResponse(BaseModel):
    """Response from exchanging a JWT access token for a private-read SIWE token."""

    siwe_token: str = Field(..., description="Encrypted SIWE token for on-chain private reads")
    address: str = Field(..., description="Authenticated Ethereum address")
    expires_in: int = Field(..., description="SIWE token expiry in seconds")


class SiweDomainResponse(BaseModel):
    """Response containing the SIWE domain configured in the contract."""

    domain: str


class SiweNonceResponse(BaseModel):
    """Response containing a nonce for SIWE authentication.

    Replay Protection:
        Each nonce can only be used once. Clients must:

        1. Request a nonce from /auth/nonce
        2. Include this nonce in the SIWE message's nonce field
        3. Submit the signed message to /auth/login before the nonce expires

        Replay attempts with the same nonce will be rejected.
    """

    address: str = Field(..., description="Checksummed Ethereum address associated with the nonce")
    nonce: str = Field(..., description="Nonce to include in SIWE message")
    expires_in: int = Field(..., description="Seconds until nonce expires")


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


class DepositAddressRequest(BaseModel):
    """Request to get a per-user deposit address."""

    # Literal pins the wire format and surfaces invalid input as 422 at parse time.
    # Extend to Literal["evm", "utxo", ...] when adding a non-EVM chain family.
    chain_type: Literal["evm"] = "evm"
    version: int = 0


class DepositAddressResponse(BaseModel):
    """Response containing a user's dedicated deposit address."""

    deposit_address: str
    chain_type: Literal["evm"]
    version: int
    min_deposit: dict[str, dict[str, str]] = Field(
        default_factory=dict
    )  # {chain_id: {native, erc20}}


class DepositCheckRequest(BaseModel):
    """Request to check/trigger deposit verification for a specific tx."""

    chain_type: Literal["evm"] = Field("evm", description="Chain family (v1: 'evm' only)")
    chain_id: int = Field(..., description="Source chain ID where the deposit was made")
    tx_hash: str = Field(..., description="Transaction hash of the deposit on the source chain")
    amount: int = Field(..., gt=0, description="Claimed deposit amount in base units (e.g. wei)")
    log_index: int = Field(0, description="Log index for ERC20 deposits (0 for native)")
    version: int = Field(0, description="Deposit address derivation version")
    lock_authorization: DepositLockAuthorization | None = Field(
        None,
        description="Optional signed post-deposit lock policy",
    )

    @field_validator("amount", mode="before")
    def _parse_amount(cls, value: int | str | float) -> int:
        return _parse_int_amount(value)

    @field_validator("tx_hash")
    def _normalise_tx_hash(cls, value: str) -> str:
        return _normalise_hex(value)


class DepositCheckResponse(BaseModel):
    """Response for deposit check status."""

    status: Literal["credited", "pending", "error"]
    deposit_id: str | None = None
    amount: str | None = None
    token_address: str | None = None
    detail: str | None = None


class SignOnRampUrlRequest(BaseModel):
    """Request to sign a MoonPay on-ramp widget URL."""

    url: str = Field(..., min_length=1)


class SignOnRampUrlResponse(BaseModel):
    """MoonPay URL signature response."""

    signature: str


class CreateOnRampIntentRequest(BaseModel):
    """Authenticated Privana intent created before opening MoonPay."""

    wallet_address: str | None = None
    token_id: str
    chain_id: int
    moonpay_currency_code: str

    @field_validator("wallet_address")
    def _normalise_wallet_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not Web3.is_address(value):
            raise ValueError("Invalid wallet_address")
        return Web3.to_checksum_address(value)

    @field_validator("token_id")
    def _normalise_token_id(cls, value: str) -> str:
        return _normalise_fixed_hex(value, byte_length=32, field_name="token_id")

    @field_validator("moonpay_currency_code")
    def _normalise_currency_code(cls, value: str | None) -> str | None:
        return _normalise_currency_code(value)


class UpdateOnRampRequest(BaseModel):
    """Compatibility update for caller-owned on-ramp transaction metadata."""

    model_config = ConfigDict(extra="forbid")

    wallet_address: str | None = None
    token_id: str | None = None
    chain_id: int | None = None
    moonpay_transaction_id: str | None = None
    base_currency_code: str | None = None
    base_currency_amount: str | None = None
    quote_currency_amount: str | None = None
    on_chain_tx_hash: str | None = None
    deposit_tx_hash: str | None = None
    lock_authorization: DepositLockAuthorization | None = None

    @field_validator("wallet_address")
    def _normalise_wallet_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not Web3.is_address(value):
            raise ValueError("Invalid wallet_address")
        return Web3.to_checksum_address(value)

    @field_validator("token_id")
    def _normalise_optional_token_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_fixed_hex(value, byte_length=32, field_name="token_id")

    @field_validator("on_chain_tx_hash", "deposit_tx_hash")
    def _normalise_optional_tx_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_fixed_hex(value, byte_length=32, field_name="tx_hash")

    @field_validator("base_currency_code")
    def _normalise_optional_currency_code(cls, value: str | None) -> str | None:
        return _normalise_currency_code(value)


class OnRampRecord(BaseModel):
    """MoonPay on-ramp transaction visible to the SDK."""

    transaction_id: str
    external_transaction_id: str | None = None
    moonpay_transaction_id: str | None = None
    status: Literal["pending", "completed", "failed", "cancelled"]
    wallet_address: str | None = None
    token_id: str | None = None
    chain_id: int | None = None
    moonpay_currency_code: str | None = None
    base_currency_code: str | None = None
    base_currency_amount: str | None = None
    quote_currency_amount: str | None = None
    on_chain_tx_hash: str | None = None
    deposit_id: str | None = None
    deposit_tx_hash: str | None = None
    lock_authorization: DepositLockAuthorization | None = None
    deposit_triggered_at: int | None = None
    credited_at: int | None = None
    created_at: int
    updated_at: int


class PendingOnRampsResponse(BaseModel):
    """Completed MoonPay transactions that still need Privana deposit verification."""

    pending: list[OnRampRecord]
