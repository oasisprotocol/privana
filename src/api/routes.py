"""FastAPI routes exposing the Accounting module flows."""

import logging
from datetime import datetime
from typing import Dict, Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from hexbytes import HexBytes
from pydantic import BaseModel, Field
from siwe import SiweMessage
from web3 import Web3
from web3.exceptions import ContractLogicError

from src.auth.dependencies import get_current_user
from src.auth.jwt_service import get_jwt_service
from src.auth.token_store import get_token_store
from src.clients.rofl import TransactionRevertedError
from src.config import load_settings
from src.models.accounting import (
    BalanceResponse,
    BatchBalancesRequest,
    BatchBalancesResponse,
    DepositQuoteRequest,
    DepositQuoteResponse,
    ExpiredLocksResponse,
    IncludeDepositRequest,
    IncludeDepositResponse,
    LockedFundsResponse,
    LockFundsRequest,
    LockNonceResponse,
    ModifyLockNonceResponse,
    ModifyLockRequest,
    PendingWithdrawalsResponse,
    SiweDomainResponse,
    SiweLoginRequest,
    SiweLoginResponse,
    SiweNonceResponse,
    TokenInfoResponse,
    TotalLockedBalanceResponse,
    TransactionSubmissionResponse,
    TransferFundsRequest,
    TransferLockedFundsRequest,
    TransferLockedNonceResponse,
    TransferNonceResponse,
    UnlockAllExpiredLocksRequest,
    UnlockFundsRequest,
    WithdrawalInfoResponse,
    WithdrawalNonceResponse,
    WithdrawalRequest,
)
from src.services.accounting_contract import (
    SubmissionResult,
    get_accounting_contract_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/accounting", tags=["Accounting"])

_service = get_accounting_contract_service()

_SIWE_TOKEN_HEADER = "X-SIWE-Token"


def _require_siwe_token(
    token: Optional[str] = Header(None, alias=_SIWE_TOKEN_HEADER),
) -> bytes:
    if not token:
        raise HTTPException(status_code=401, detail=f"Missing {_SIWE_TOKEN_HEADER} header")
    try:
        raw = HexBytes(token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {_SIWE_TOKEN_HEADER} header") from exc
    return bytes(raw)


def _wrap_submission(result: SubmissionResult) -> TransactionSubmissionResponse:
    return TransactionSubmissionResponse(
        status=result.status,
        detail=result.detail,
    )


@router.post("/quote/deposit", response_model=DepositQuoteResponse)
async def create_deposit_quote(payload: DepositQuoteRequest) -> DepositQuoteResponse:
    """Return deposit destination details and transaction data for a user/token/amount."""

    try:
        quote: Dict = await _service.deposit_quote(
            payload.user_address, payload.token_id, payload.amount
        )
        return DepositQuoteResponse(**quote)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/deposits", response_model=IncludeDepositResponse)
async def include_deposit(payload: IncludeDepositRequest) -> IncludeDepositResponse:
    """Submit a deposit inclusion transaction (automatically detects native/ERC20)."""

    try:
        result = await _service.include_deposit(payload.model_dump())
        return IncludeDepositResponse(status=result.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Deposit inclusion transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - network errors
        logger.exception("Failed to submit deposit inclusion")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/lock", response_model=TransactionSubmissionResponse)
async def lock_funds(payload: LockFundsRequest) -> TransactionSubmissionResponse:
    """Lock user funds for a service with a signed authorization."""

    try:
        submission = await _service.lock_funds(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Lock funds transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to lock funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/modify-lock", response_model=TransactionSubmissionResponse)
async def modify_lock(payload: ModifyLockRequest) -> TransactionSubmissionResponse:
    """Modify an existing lock by adding funds and/or extending the expiry."""

    try:
        submission = await _service.modify_lock(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Modify lock transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to modify lock")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/transfer", response_model=TransactionSubmissionResponse)
async def transfer_funds(payload: TransferFundsRequest) -> TransactionSubmissionResponse:
    """Transfer funds between accounting balances using a user signature."""

    try:
        submission = await _service.transfer_funds(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Transfer funds transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to transfer funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.get("/funds/transfer/nonce/{user_address}", response_model=TransferNonceResponse)
async def get_transfer_nonce(user_address: str) -> TransferNonceResponse:
    """Get the current transfer nonce for a user."""

    try:
        result = await _service.get_transfer_nonce(user_address)
        return TransferNonceResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get transfer nonce")
        raise HTTPException(status_code=500, detail="Failed to retrieve transfer nonce") from exc


@router.get("/funds/lock/nonce/{user_address}", response_model=LockNonceResponse)
async def get_lock_nonce(user_address: str) -> LockNonceResponse:
    """Get the current createLock nonce for a user."""

    try:
        result = await _service.get_lock_nonce(user_address)
        return LockNonceResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get lock nonce")
        raise HTTPException(status_code=500, detail="Failed to retrieve lock nonce") from exc


@router.get("/funds/modify-lock/nonce/{user_address}", response_model=ModifyLockNonceResponse)
async def get_modify_lock_nonce(user_address: str) -> ModifyLockNonceResponse:
    """Get the current modifyLock nonce for a user."""

    try:
        result = await _service.get_modify_lock_nonce(user_address)
        return ModifyLockNonceResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get modify lock nonce")
        raise HTTPException(status_code=500, detail="Failed to retrieve modify lock nonce") from exc


@router.get(
    "/funds/transfer-locked/nonce/{service_address}",
    response_model=TransferLockedNonceResponse,
)
async def get_transfer_locked_nonce(service_address: str) -> TransferLockedNonceResponse:
    """Get the current transferFromLock nonce for a service."""

    try:
        result = await _service.get_transfer_locked_nonce(service_address)
        return TransferLockedNonceResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get transfer locked nonce")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve transfer locked nonce"
        ) from exc


@router.post("/funds/transfer-locked", response_model=TransactionSubmissionResponse)
async def transfer_locked_funds(
    payload: TransferLockedFundsRequest,
) -> TransactionSubmissionResponse:
    """Transfer locked funds based on a casino service signature."""

    try:
        submission = await _service.transfer_locked_funds(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Transfer locked funds transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to transfer locked funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/unlock", response_model=TransactionSubmissionResponse)
async def unlock_funds(payload: UnlockFundsRequest) -> TransactionSubmissionResponse:
    """Unlock funds when lock expiry has passed."""

    try:
        submission = await _service.unlock_funds(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Unlock funds transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to unlock funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/withdraw", response_model=TransactionSubmissionResponse)
async def request_withdrawal(payload: WithdrawalRequest) -> TransactionSubmissionResponse:
    """Request a withdrawal by validating the user's signature. Must be resolved in a later block."""

    try:
        submission = await _service.request_withdrawal(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Withdrawal request transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to submit withdrawal request")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.get("/withdraw/pending/{user_address}", response_model=PendingWithdrawalsResponse)
async def get_pending_withdrawals(user_address: str) -> PendingWithdrawalsResponse:
    """Get all pending (unresolved) withdrawal requests for a user."""

    try:
        result = await _service.get_pending_withdrawals(user_address)
        return PendingWithdrawalsResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get pending withdrawals")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve pending withdrawals"
        ) from exc


@router.get("/withdraw/nonce/{user_address}", response_model=WithdrawalNonceResponse)
async def get_withdrawal_nonce(user_address: str) -> WithdrawalNonceResponse:
    """Get the current withdrawal nonce for a user."""

    try:
        result = await _service.get_withdrawal_nonce(user_address)
        return WithdrawalNonceResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get withdrawal nonce")
        raise HTTPException(status_code=500, detail="Failed to retrieve withdrawal nonce") from exc


@router.get("/withdraw/{index}", response_model=WithdrawalInfoResponse)
async def get_withdrawal_info(index: int) -> WithdrawalInfoResponse:
    """Get information about a specific withdrawal request."""

    try:
        result = await _service.get_withdrawal(index)
        return WithdrawalInfoResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get withdrawal info")
        raise HTTPException(status_code=500, detail="Failed to retrieve withdrawal info") from exc


@router.get(
    "/funds/locked/{user_address}",
    response_model=LockedFundsResponse,
)
async def get_locked_funds(
    user_address: str,
    service_address: Optional[str] = None,
    siwe_token: bytes = Depends(_require_siwe_token),
) -> LockedFundsResponse:
    """Get locked funds for a user, optionally filtered by service address."""
    try:
        result = await _service.get_locked_funds(
            user_address,
            service_address,
            siwe_token,
        )
        return LockedFundsResponse(**result)
    except ContractLogicError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get locked funds")
        raise HTTPException(status_code=500, detail="Failed to retrieve locked funds") from exc


@router.get(
    "/balances/{user_address}/{token_id}",
    response_model=BalanceResponse,
)
async def get_balance(
    user_address: str,
    token_id: str,
    siwe_token: bytes = Depends(_require_siwe_token),
) -> BalanceResponse:
    """Get the user's balance for a specific token from the contract."""
    try:
        result = await _service.get_balance(user_address, token_id, siwe_token)
        return BalanceResponse(**result)
    except ContractLogicError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get balance")
        raise HTTPException(status_code=500, detail="Failed to retrieve balance") from exc


@router.post("/funds/unlock-all-expired", response_model=TransactionSubmissionResponse)
async def unlock_all_expired_locks(
    payload: UnlockAllExpiredLocksRequest,
) -> TransactionSubmissionResponse:
    """Unlock all expired locks for a user."""

    try:
        submission = await _service.unlock_all_expired_locks(payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Unlock all expired locks transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to unlock all expired locks")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.get(
    "/funds/expired/{user_address}",
    response_model=ExpiredLocksResponse,
)
async def get_expired_locks(
    user_address: str,
    siwe_token: bytes = Depends(_require_siwe_token),
) -> ExpiredLocksResponse:
    """Get all expired locks for a user."""
    try:
        result = await _service.get_expired_locks(user_address, siwe_token)
        return ExpiredLocksResponse(**result)
    except ContractLogicError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get expired locks")
        raise HTTPException(status_code=500, detail="Failed to retrieve expired locks") from exc


@router.post(
    "/balances/batch",
    response_model=BatchBalancesResponse,
)
async def get_batch_balances(
    payload: BatchBalancesRequest,
    siwe_token: bytes = Depends(_require_siwe_token),
) -> BatchBalancesResponse:
    """Get balances for multiple tokens for a user."""
    try:
        result = await _service.get_batch_balances(
            payload.user_address, payload.token_ids, siwe_token
        )
        return BatchBalancesResponse(**result)
    except ContractLogicError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get batch balances")
        raise HTTPException(status_code=500, detail="Failed to retrieve balances") from exc


@router.get(
    "/funds/locked/total/{user_address}/{token_id}",
    response_model=TotalLockedBalanceResponse,
)
async def get_total_locked_balance(
    user_address: str,
    token_id: str,
    siwe_token: bytes = Depends(_require_siwe_token),
) -> TotalLockedBalanceResponse:
    """Get total locked balance for a specific token across all locks."""
    try:
        result = await _service.get_total_locked_balance(user_address, token_id, siwe_token)
        return TotalLockedBalanceResponse(**result)
    except ContractLogicError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get total locked balance")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve total locked balance"
        ) from exc


@router.get("/auth/domain", response_model=SiweDomainResponse)
async def get_siwe_domain() -> SiweDomainResponse:
    """Get the SIWE domain configured for this service."""
    settings = load_settings()
    if not settings.siwe_domain:
        raise HTTPException(status_code=500, detail="SIWE_DOMAIN not configured")
    return SiweDomainResponse(domain=settings.siwe_domain)


@router.get("/auth/nonce", response_model=SiweNonceResponse)
async def get_siwe_nonce(address: str) -> SiweNonceResponse:
    """Get a nonce for SIWE authentication.

    Replay Protection:
        Each nonce can only be used once. Clients must:

        1. Request a nonce from this endpoint
        2. Include this nonce in the SIWE message's nonce field
        3. Submit the signed message to /auth/login before the nonce expires

        Replay attempts with the same nonce will be rejected.

    Args:
        address: Ethereum address requesting the nonce. If the address already
                 has a valid unexpired nonce, that nonce is returned instead of
                 generating a new one. This is a convenience feature - if someone
                 provides a wrong address, the real owner will simply get a new nonce.
    """
    token_store = get_token_store()

    # Normalize address to checksum format
    try:
        client_id = Web3.to_checksum_address(address)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Ethereum address") from exc

    try:
        nonce = token_store.generate_nonce(client_id=client_id)
        return SiweNonceResponse(
            nonce=nonce,
            expires_in=token_store.nonce_expiry_seconds,
        )
    except RuntimeError as exc:
        # Nonce store at capacity
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/auth/login", response_model=SiweLoginResponse)
async def siwe_login(payload: SiweLoginRequest) -> SiweLoginResponse:
    """Perform SIWE login and return both SIWE token and JWT tokens.

    This endpoint:
    1. Validates the nonce in the SIWE message (API-level replay protection)
    2. Parses the SIWE message to extract the address
    3. Verifies the SIWE signature locally using the siwe library
    4. Generates and encrypts the AuthToken locally using Deoxys-II
    5. Issues JWT tokens for API authentication

    The SIWE token is used for private reads from Sapphire contracts.
    The JWT tokens can be used for API authentication by any app using the accounting module.
    """
    import time

    from siwe import ExpiredMessage, InvalidSignature, MalformedSession

    from src.auth.auth_token_service import get_auth_token_service

    jwt_service = get_jwt_service()
    token_store = get_token_store()
    auth_token_service = get_auth_token_service()

    # Parse SIWE message to extract address and nonce
    try:
        siwe_message = SiweMessage.from_message(payload.siwe_message)
    except Exception as exc:
        logger.warning(f"Invalid SIWE message format: {exc}")
        raise HTTPException(status_code=400, detail="Invalid SIWE message") from exc

    # Check nonce validity first (don't consume yet - only consume after successful auth)
    # This allows retry with the same nonce if the signature is wrong
    nonce = siwe_message.nonce
    if not nonce or not token_store.is_nonce_valid(nonce):
        logger.warning(f"Invalid or already used nonce for address {siwe_message.address}")
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired nonce. Request a new nonce from /auth/nonce",
        )

    address = Web3.to_checksum_address(siwe_message.address)

    # Get the expected domain from config
    settings = load_settings()
    if not settings.siwe_domain:
        raise HTTPException(status_code=500, detail="SIWE_DOMAIN not configured")
    expected_domain = settings.siwe_domain

    # Validate chain_id is in allowed set
    # Defaults to configured RPC chains + Sapphire if no explicit list is set
    allowed_chains = settings.siwe_allowed_chain_ids
    if not allowed_chains:
        allowed_chains = set(settings.chain_rpc_urls.keys()) | {settings.sapphire_chain_id}

    if not siwe_message.chain_id or siwe_message.chain_id not in allowed_chains:
        logger.warning(
            f"SIWE chain_id {siwe_message.chain_id} not in allowed chains {allowed_chains} "
            f"for {address}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Chain ID {siwe_message.chain_id} is not supported",
        )

    # Verify the SIWE signature locally
    # The siwe library verifies: signature, domain, nonce, and expiration.
    # Chain ID is validated above against allowed chains for wallet compatibility
    # (reduces chain switching friction while ensuring only known chains are accepted).
    try:
        # Get provider for EIP-1271 smart contract wallet signature verification
        provider = None
        if siwe_message.chain_id in settings.chain_rpc_urls:
            provider = Web3.HTTPProvider(settings.chain_rpc_urls[siwe_message.chain_id])

        siwe_message.verify(
            payload.signature,
            domain=expected_domain,
            nonce=nonce,
            provider=provider,
        )
    except InvalidSignature as exc:
        logger.warning(f"Invalid SIWE signature for {address}: {exc}")
        raise HTTPException(status_code=400, detail="Invalid signature") from exc
    except ExpiredMessage as exc:
        logger.warning(f"Expired SIWE message for {address}: {exc}")
        raise HTTPException(status_code=400, detail="SIWE message expired") from exc
    except MalformedSession as exc:
        logger.warning(f"Malformed SIWE session for {address}: {exc}")
        raise HTTPException(status_code=400, detail="Invalid SIWE message format") from exc
    except Exception as exc:
        logger.warning(f"SIWE verification failed for {address}: {exc}")
        raise HTTPException(status_code=400, detail="SIWE verification failed") from exc

    # Validate SIWE message timestamps
    auth_token_validity_seconds = settings.auth_token_validity_seconds
    tolerance_seconds = 5 * 60  # 5 minutes tolerance for clock skew
    now = int(time.time())

    # Validate issued_at is within tolerance (not too far in the past or future).
    # The expiration check would catch future issued_at, but rejecting it here provides
    # a clearer error message.
    if not siwe_message.issued_at:
        raise HTTPException(
            status_code=400,
            detail="SIWE message must include issued_at",
        )

    # ISO8601Datetime is a str subclass, parse it to get timestamp
    issued_at_dt = datetime.fromisoformat(str(siwe_message.issued_at).replace("Z", "+00:00"))
    issued_at = int(issued_at_dt.timestamp())
    if abs(now - issued_at) > tolerance_seconds:
        logger.warning(
            f"SIWE issued_at outside tolerance for {address}: "
            f"issued_at={issued_at}, now={now}, diff={now - issued_at}s"
        )
        raise HTTPException(
            status_code=400,
            detail="SIWE message issued_at is outside acceptable time range",
        )

    # Validate expiration_time matches expected validity period
    if not siwe_message.expiration_time:
        raise HTTPException(
            status_code=400,
            detail="SIWE message must include expiration_time",
        )

    expiration_dt = datetime.fromisoformat(str(siwe_message.expiration_time).replace("Z", "+00:00"))
    valid_until = int(expiration_dt.timestamp())
    expected_valid_until = now + auth_token_validity_seconds

    if abs(valid_until - expected_valid_until) > tolerance_seconds:
        logger.warning(
            f"SIWE expiration mismatch for {address}: "
            f"got {valid_until}, expected ~{expected_valid_until}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"SIWE expiration_time must be ~{auth_token_validity_seconds} seconds from now",
        )

    # Use the validated domain
    domain = expected_domain

    # Statement and resources from SIWE message (informational, included in auth token)
    statement = siwe_message.statement or ""
    resources = list(siwe_message.resources) if siwe_message.resources else []

    # Generate and encrypt the AuthToken locally
    try:
        siwe_token_bytes = auth_token_service.create_and_encrypt(
            domain=domain,
            user_addr=address,
            valid_until=valid_until,
            statement=statement,
            resources=resources,
        )
        # Convert to hex for the response
        siwe_token = "0x" + siwe_token_bytes.hex()
    except Exception as exc:
        logger.exception("Failed to generate AuthToken")
        raise HTTPException(status_code=500, detail="Failed to generate auth token") from exc

    # Consume the nonce AFTER successful authentication
    # This is the API-level replay protection - each nonce can only be used once
    if not token_store.consume_nonce(nonce):
        # Race condition: nonce was consumed between check and consume
        # This is rare but possible with concurrent requests using the same nonce
        logger.warning(f"Nonce was consumed by concurrent request for {address}")
        raise HTTPException(
            status_code=400,
            detail="Nonce already used. Request a new nonce from /auth/nonce",
        )

    # Issue JWT tokens
    access_token = jwt_service.create_token(address)
    refresh_token = jwt_service.create_refresh_token(address)

    logger.info(f"User {address} logged in successfully")

    return SiweLoginResponse(
        siwe_token=siwe_token,
        jwt_access_token=access_token,
        jwt_refresh_token=refresh_token,
        address=address,
        jwt_expires_in=jwt_service.access_token_expiry_seconds,
        jwt_refresh_expires_in=jwt_service.refresh_token_expiry_seconds,
    )


@router.get("/tokens/{token_id}", response_model=TokenInfoResponse)
async def get_token_info(token_id: str) -> TokenInfoResponse:
    """Get information about a registered token."""

    try:
        result = await _service.get_token_info(token_id)
        return TokenInfoResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get token info")
        raise HTTPException(status_code=500, detail="Failed to retrieve token info") from exc


# JWT-related Pydantic models


class RefreshRequest(BaseModel):
    """Request for refreshing tokens."""

    refresh_token: str = Field(..., description="Current refresh token")


class RefreshResponse(BaseModel):
    """Response from successful token refresh."""

    token: str = Field(..., description="New JWT access token")
    refresh_token: str = Field(..., description="New JWT refresh token")
    expires_in: int = Field(..., description="Access token expiry in seconds")
    refresh_expires_in: int = Field(..., description="Refresh token expiry in seconds")


class JWKSResponse(BaseModel):
    """JWKS response for public key distribution."""

    keys: list = Field(..., description="Array of JWK objects")


class LogoutRequest(BaseModel):
    """Request for logout."""

    refresh_token: str | None = Field(
        None, description="Refresh token to revoke (optional, but recommended)"
    )
    revoke_all: bool = Field(False, description="Revoke all refresh tokens for this user")


class MeResponse(BaseModel):
    """Response from /me endpoint."""

    address: str = Field(..., description="Authenticated Ethereum address")


# JWT-related endpoints


@router.post("/auth/jwt/refresh", response_model=RefreshResponse)
async def refresh(payload: RefreshRequest) -> RefreshResponse:
    """Refresh access and refresh tokens.

    Exchanges a valid refresh token for new access and refresh tokens.
    The old refresh token is revoked (token rotation).
    """
    jwt_service = get_jwt_service()

    try:
        new_access_token, new_refresh_token = jwt_service.refresh_tokens(payload.refresh_token)

        return RefreshResponse(
            token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=jwt_service.access_token_expiry_seconds,
            refresh_expires_in=jwt_service.refresh_token_expiry_seconds,
        )
    except (ValueError, jwt.InvalidTokenError) as e:
        # ValueError: token revoked or invalid type
        # jwt.InvalidTokenError: invalid/expired/malformed JWT (includes ExpiredSignatureError)
        logger.warning(f"Token refresh failed: {e}")
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.exception("Token refresh failed with unexpected error")
        raise HTTPException(status_code=500, detail="Token refresh failed") from e


@router.post("/auth/jwt/logout")
async def logout(
    payload: LogoutRequest | None = None,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Logout the current user and revoke refresh tokens.

    The access token remains valid until expiration (stateless JWT).
    However, refresh tokens are revoked to prevent obtaining new access tokens.

    Options:
    - Provide refresh_token to revoke that specific token (must belong to current user)
    - Set revoke_all=true to revoke all refresh tokens for this user
    """
    jwt_service = get_jwt_service()
    revoked_count = 0

    if payload:
        if payload.revoke_all:
            # Revoke all refresh tokens for this user
            revoked_count = jwt_service.revoke_all_refresh_tokens(current_user)
        elif payload.refresh_token:
            # Verify the refresh token belongs to the current user before revoking
            # This prevents a user from revoking another user's refresh tokens
            try:
                token_address = jwt_service.verify_refresh_token(payload.refresh_token)
                if token_address.lower() != current_user.lower():
                    raise HTTPException(
                        status_code=403,
                        detail="Cannot revoke refresh token belonging to another user",
                    )
                # Token belongs to current user, safe to revoke
                if jwt_service.revoke_refresh_token(payload.refresh_token):
                    revoked_count = 1
            except ValueError as exc:
                # Token already revoked or invalid - not an error for logout
                logger.debug(f"Refresh token already invalid during logout: {exc}")

    logger.info(f"User {current_user} logged out, revoked {revoked_count} refresh token(s)")
    return {"message": "Logged out successfully", "revoked_tokens": revoked_count}


@router.get("/auth/jwt/jwks.json", response_model=JWKSResponse)
async def get_jwks() -> JWKSResponse:
    """Get the JWKS containing public keys for JWT verification.

    External services can use this endpoint to fetch the public key
    for verifying JWTs issued by this service.
    """
    jwt_service = get_jwt_service()
    jwks = jwt_service.get_jwks()
    return JWKSResponse(**jwks)


@router.get("/auth/jwt/me", response_model=MeResponse)
async def get_me(current_user: str = Depends(get_current_user)) -> MeResponse:
    """Get information about the currently authenticated user.

    Requires a valid JWT in the Authorization header.
    """
    return MeResponse(address=current_user)
