"""FastAPI routes exposing the Accounting module flows."""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from hexbytes import HexBytes
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from src.auth.auth_token_service import get_auth_token_service
from src.auth.dependencies import get_current_user, get_current_user_optional
from src.auth.http import auth_exception, enforce_expected_origin, no_store_headers
from src.auth.jwt_service import get_jwt_service
from src.auth.rate_limiter import get_auth_rate_limiter, request_identity
from src.auth.siwe_config import get_siwe_config
from src.auth.siwe_service import SiweAuthError, authenticate_siwe_message
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
    TokenListResponse,
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


def _mint_private_read_token(user_address: str) -> bytes:
    settings = load_settings()
    try:
        siwe_config = get_siwe_config(settings)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    valid_until = int(time.time()) + settings.auth_token_validity_seconds
    token = get_auth_token_service().create_and_encrypt(
        domain=siwe_config.domain,
        user_addr=user_address,
        valid_until=valid_until,
    )
    return token


@dataclass(frozen=True)
class PrivateReadAuth:
    """Authenticated SIWE token + resolved user address for private reads."""

    token: bytes
    user_address: str


def _require_private_read_auth(
    current_user: Optional[str] = Depends(get_current_user_optional),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    token: Optional[str] = Header(None, alias=_SIWE_TOKEN_HEADER),
) -> PrivateReadAuth:
    if authorization and token:
        raise HTTPException(
            status_code=400,
            detail=f"Provide either Authorization bearer token or {_SIWE_TOKEN_HEADER}, not both",
        )
    if current_user:
        return PrivateReadAuth(_mint_private_read_token(current_user), current_user)
    if not token:
        raise HTTPException(
            status_code=401,
            detail=f"Missing Authorization bearer token or {_SIWE_TOKEN_HEADER} header",
        )
    try:
        raw = HexBytes(token)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {_SIWE_TOKEN_HEADER} header") from exc
    try:
        auth_token = get_auth_token_service().decode_auth_token(bytes(raw), validate_expiry=False)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    return PrivateReadAuth(bytes(raw), auth_token.user_addr)


def _enforce_auth_rate_limit(request: Request, bucket: str, limit: int) -> None:
    settings = load_settings()
    retry_after = get_auth_rate_limiter().hit(
        bucket=bucket,
        key=request_identity(request),
        limit=limit,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if retry_after is not None:
        raise auth_exception(
            status_code=429,
            detail="Too many authentication requests. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )


def _enforce_browser_auth_origin(request: Request) -> None:
    settings = load_settings()
    try:
        expected_origin = get_siwe_config(settings).origin
    except ValueError as exc:
        raise auth_exception(status_code=500, detail=str(exc)) from exc
    enforce_expected_origin(
        request,
        expected_origin=expected_origin,
        detail="Browser SIWE requests must originate from the configured auth origin",
        allow_missing=True,
    )


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
    "/funds/locked",
    response_model=LockedFundsResponse,
)
async def get_locked_funds(
    service_address: Optional[str] = None,
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> LockedFundsResponse:
    """Get locked funds for the authenticated user, optionally filtered by service address."""
    try:
        result = await _service.get_locked_funds(
            auth.user_address,
            service_address,
            auth.token,
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
    "/balances/{token_id}",
    response_model=BalanceResponse,
)
async def get_balance(
    token_id: str,
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> BalanceResponse:
    """Get the authenticated user's balance for a specific token from the contract."""
    try:
        result = await _service.get_balance(auth.user_address, token_id, auth.token)
        return BalanceResponse(**result)
    except ContractLogicError as exc:
        logger.warning("SIWE token validation failed for %s: %s", auth.user_address, exc)
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
    "/funds/expired",
    response_model=ExpiredLocksResponse,
)
async def get_expired_locks(
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> ExpiredLocksResponse:
    """Get all expired locks for the authenticated user."""
    try:
        result = await _service.get_expired_locks(auth.user_address, auth.token)
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
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> BatchBalancesResponse:
    """Get balances for multiple tokens for the authenticated user."""
    try:
        result = await _service.get_batch_balances(auth.user_address, payload.token_ids, auth.token)
        return BatchBalancesResponse(**result)
    except ContractLogicError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get batch balances")
        raise HTTPException(status_code=500, detail="Failed to retrieve balances") from exc


@router.get(
    "/funds/locked/total/{token_id}",
    response_model=TotalLockedBalanceResponse,
)
async def get_total_locked_balance(
    token_id: str,
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> TotalLockedBalanceResponse:
    """Get total locked balance for a specific token across all locks for the authenticated user."""
    try:
        result = await _service.get_total_locked_balance(auth.user_address, token_id, auth.token)
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
    """Get the configured SIWE domain for this service."""
    settings = load_settings()
    try:
        return SiweDomainResponse(domain=get_siwe_config(settings).domain)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/auth/nonce", response_model=SiweNonceResponse)
async def get_siwe_nonce(address: str, request: Request, response: Response) -> SiweNonceResponse:
    """Issue a single-use nonce for SIWE login."""
    _enforce_browser_auth_origin(request)
    settings = load_settings()
    _enforce_auth_rate_limit(request, "siwe_nonce", settings.auth_nonce_rate_limit)

    token_store = get_token_store()
    try:
        client_id = Web3.to_checksum_address(address)
    except Exception as exc:
        raise auth_exception(status_code=400, detail="Invalid Ethereum address") from exc

    nonce = token_store.generate_nonce(client_id=client_id)
    response.headers.update(no_store_headers())
    return SiweNonceResponse(
        address=client_id,
        nonce=nonce,
        expires_in=token_store.nonce_expiry_seconds,
    )


@router.post("/auth/login", response_model=SiweLoginResponse)
async def siwe_login(
    payload: SiweLoginRequest,
    request: Request,
    response: Response,
) -> SiweLoginResponse:
    """Perform SIWE login, mint a Sapphire AuthToken, and issue JWTs."""
    _enforce_browser_auth_origin(request)
    settings = load_settings()
    _enforce_auth_rate_limit(request, "siwe_login", settings.auth_login_rate_limit)

    jwt_service = get_jwt_service()
    try:
        auth_result = authenticate_siwe_message(payload.siwe_message, payload.signature)
    except SiweAuthError as exc:
        raise auth_exception(status_code=exc.status_code, detail=exc.detail) from exc

    access_token = jwt_service.create_token(auth_result.address)
    refresh_token = jwt_service.create_refresh_token(auth_result.address)
    response.headers.update(no_store_headers())

    return SiweLoginResponse(
        siwe_token=auth_result.siwe_token_hex,
        jwt_access_token=access_token,
        jwt_refresh_token=refresh_token,
        address=auth_result.address,
        jwt_expires_in=jwt_service.access_token_expiry_seconds,
        jwt_refresh_expires_in=jwt_service.refresh_token_expiry_seconds,
    )


@router.get("/tokens", response_model=TokenListResponse)
async def list_tokens() -> TokenListResponse:
    """List all registered tokens."""
    try:
        tokens = await _service.list_all_tokens()
        return TokenListResponse(tokens=[TokenInfoResponse(**t) for t in tokens])
    except Exception as exc:
        logger.exception("Failed to list tokens")
        raise HTTPException(status_code=500, detail="Failed to list tokens") from exc


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


@router.post("/auth/jwt/refresh", response_model=RefreshResponse)
async def refresh(payload: RefreshRequest, response: Response) -> RefreshResponse:
    """Rotate a refresh token and issue fresh access and refresh tokens."""
    jwt_service = get_jwt_service()
    try:
        new_access_token, new_refresh_token = jwt_service.refresh_tokens(payload.refresh_token)
        response.headers.update(no_store_headers())
        return RefreshResponse(
            token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=jwt_service.access_token_expiry_seconds,
            refresh_expires_in=jwt_service.refresh_token_expiry_seconds,
        )
    except (ValueError, jwt.InvalidTokenError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Token refresh failed")
        raise HTTPException(status_code=500, detail="Token refresh failed") from exc


@router.post("/auth/jwt/logout")
async def logout(
    payload: LogoutRequest | None = None,
    current_user: str = Depends(get_current_user),
) -> dict:
    """Revoke refresh tokens for the current user."""
    jwt_service = get_jwt_service()
    revoked_count = 0

    if payload:
        if payload.revoke_all:
            revoked_count = jwt_service.revoke_all_refresh_tokens(current_user)
        elif payload.refresh_token:
            try:
                token_address = jwt_service.verify_refresh_token(payload.refresh_token)
                if token_address.lower() != current_user.lower():
                    raise HTTPException(
                        status_code=403,
                        detail="Cannot revoke refresh token belonging to another user",
                    )
                if jwt_service.revoke_refresh_token(payload.refresh_token):
                    revoked_count = 1
            except ValueError:
                revoked_count = 0

    return {"message": "Logged out successfully", "revoked_tokens": revoked_count}


@router.get("/auth/jwt/jwks.json", response_model=JWKSResponse)
async def get_jwks() -> JWKSResponse:
    """Return the public keys used to verify JWTs from this service."""
    return JWKSResponse(**get_jwt_service().get_jwks())


@router.get("/auth/jwt/me", response_model=MeResponse)
async def get_me(current_user: str = Depends(get_current_user)) -> MeResponse:
    """Return the authenticated address from the JWT bearer token."""
    return MeResponse(address=current_user)
