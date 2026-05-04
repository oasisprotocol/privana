"""FastAPI routes exposing the Accounting module flows."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from hexbytes import HexBytes
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from src.auth.auth_token_service import get_auth_token_service
from src.auth.dependencies import get_current_user, get_current_user_optional
from src.auth.http import auth_exception, enforce_expected_origin, no_store_headers
from src.auth.jwt_service import get_jwt_service
from src.auth.rate_limiter import get_auth_rate_limiter, request_identity
from src.auth.siwe_config import get_siwe_config, get_siwe_configs
from src.auth.siwe_service import SiweAuthError, authenticate_siwe_message
from src.auth.token_store import get_token_store
from src.clients.rofl import TransactionRevertedError
from src.config import load_settings
from src.config.chain_config import MIN_DEPOSIT_ERC20_WEI, MIN_DEPOSIT_NATIVE_WEI
from src.models.accounting import (
    BalanceResponse,
    BatchBalancesRequest,
    BatchBalancesResponse,
    DepositAddressRequest,
    DepositAddressResponse,
    DepositCheckRequest,
    DepositCheckResponse,
    ExpiredLocksResponse,
    HistoryResponse,
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
    WithdrawFromLockRequest,
    _normalise_hex,
)
from src.services.accounting_contract import (
    SubmissionResult,
    get_accounting_contract_service,
)
from src.services.deposit_processor import get_deposit_processor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/accounting", tags=["Accounting"])

_service = get_accounting_contract_service()

_SIWE_TOKEN_HEADER = "X-SIWE-Token"
_ZERO_ADDRESS = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")


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


async def _require_user_and_private_read_token(
    current_user: Optional[str] = Depends(get_current_user_optional),
    token: Optional[str] = Header(None, alias=_SIWE_TOKEN_HEADER),
) -> tuple[str, bytes]:
    """Authenticate and return (user_address, siwe_token).

    JWT path: address from JWT, SIWE token minted server-side.
    SIWE path: address resolved on-chain via authSender(), token used directly.
    """
    if current_user:
        return current_user, _mint_private_read_token(current_user)
    if not token:
        raise HTTPException(
            status_code=401,
            detail=f"Missing Authorization bearer token or {_SIWE_TOKEN_HEADER} header",
        )
    try:
        raw = bytes(HexBytes(token))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {_SIWE_TOKEN_HEADER} header") from exc
    if not raw:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token")
    try:
        address = await _service.resolve_address_from_token(raw)
    except Exception as exc:
        logger.warning(
            "resolve_address_from_token failed: %s: %s (token length: %d bytes)",
            type(exc).__name__,
            exc,
            len(raw),
        )
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    if not Web3.is_address(address) or Web3.to_checksum_address(address) == _ZERO_ADDRESS:
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token")
    return address, raw


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
        expected_origins = {cfg.origin for cfg in get_siwe_configs(settings)}
    except ValueError as exc:
        raise auth_exception(status_code=500, detail=str(exc)) from exc
    enforce_expected_origin(
        request,
        expected_origins=expected_origins,
        detail="Browser SIWE requests must originate from a configured auth origin",
        allow_missing=True,
    )


def _wrap_submission(result: SubmissionResult) -> TransactionSubmissionResponse:
    return TransactionSubmissionResponse(
        submission_id=result.submission_id,
        status=result.status,
        detail=result.detail,
    )


@router.post("/deposits/address", response_model=DepositAddressResponse)
async def get_deposit_address(
    payload: DepositAddressRequest,
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> DepositAddressResponse:
    """Get the user's dedicated per-user deposit address."""
    try:
        address = await _service.get_deposit_address(
            payload.chain_type, payload.version, auth.token
        )
        return DepositAddressResponse(
            deposit_address=address,
            chain_type=payload.chain_type,
            version=payload.version,
            min_deposit={
                str(cid): {
                    "native": str(MIN_DEPOSIT_NATIVE_WEI.get(cid, 0)),
                    "erc20": str(MIN_DEPOSIT_ERC20_WEI.get(cid, 0)),
                }
                for cid in MIN_DEPOSIT_NATIVE_WEI
            },
        )
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in get_deposit_address: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get deposit address")
        raise HTTPException(status_code=500, detail="Failed to get deposit address") from exc


@router.post("/deposits/check", response_model=DepositCheckResponse)
async def check_deposit(
    payload: DepositCheckRequest,
    response: Response,
    auth: tuple[str, bytes] = Depends(_require_user_and_private_read_token),
) -> DepositCheckResponse:
    """Verify a deposit and start the sweep in the background.

    Returns 200 with status="credited" for idempotent replays.
    Returns 202 with status="pending" when sweep is started or in progress.
    Clients poll GET /deposits/status/{deposit_id} for completion.
    """
    beneficiary, siwe_token = auth
    try:
        processor = get_deposit_processor()
        result = await processor.process_deposit(
            beneficiary=beneficiary,
            chain_type=payload.chain_type,
            chain_id=payload.chain_id,
            tx_hash=payload.tx_hash,
            amount=payload.amount,
            log_index=payload.log_index,
            version=payload.version,
            siwe_token=siwe_token,
        )
        resp = DepositCheckResponse(**result)
        if resp.status == "pending":
            response.status_code = 202
        return resp
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in deposit check: %s", exc)
        raise HTTPException(status_code=422, detail="Deposit credit failed") from exc
    except ValueError as exc:
        logger.warning("Deposit check rejected: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to process deposit")
        raise HTTPException(status_code=500, detail="Internal error") from exc


@router.get("/deposits/status/{deposit_id}", response_model=DepositCheckResponse)
async def get_deposit_status(
    deposit_id: str,
    auth: tuple[str, bytes] = Depends(_require_user_and_private_read_token),
) -> DepositCheckResponse:
    """Poll deposit status by deposit_id.

    Returns credited/pending/error based on sweep state.
    Falls through to on-chain check when no in-memory record exists.
    """
    beneficiary, _siwe_token = auth
    deposit_id_hex = _normalise_hex(deposit_id)

    processor = get_deposit_processor()

    # Fast path: check in-memory sweep record
    local_status = processor.get_deposit_status(deposit_id_hex, beneficiary)
    if local_status is not None:
        return DepositCheckResponse(**local_status)

    # No in-flight record — check on-chain
    try:
        deposit_id_bytes = bytes.fromhex(deposit_id_hex.removeprefix("0x"))
        is_processed = await _service.is_deposit_processed(deposit_id_bytes)
        if is_processed:
            return DepositCheckResponse(status="credited", deposit_id=deposit_id_hex)
    except Exception:
        logger.exception("Failed to check deposit status on-chain for %s", deposit_id_hex)
        raise HTTPException(status_code=500, detail="Failed to check deposit status")

    raise HTTPException(status_code=404, detail=f"No deposit found for key {deposit_id_hex}")


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


@router.post("/funds/withdraw-from-lock", response_model=TransactionSubmissionResponse)
async def withdraw_from_lock(
    payload: WithdrawFromLockRequest,
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> TransactionSubmissionResponse:
    """Withdraw locked funds directly to an external destination."""

    try:
        submission = await _service.withdraw_from_lock(
            payload.model_dump(), auth.user_address, auth.token
        )
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TransactionRevertedError as exc:
        logger.error("Withdraw-from-lock transaction reverted: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to withdraw from lock")
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


@router.get("/history", response_model=HistoryResponse)
async def get_history(
    offset: int = Query(
        -1,
        description=(
            "0-indexed page number from the oldest entries, or negative page number "
            "from the end (-1 is the latest page)"
        ),
    ),
    limit: int = Query(50, ge=0, le=100, description="Page size, max 100"),
    auth: PrivateReadAuth = Depends(_require_private_read_auth),
) -> HistoryResponse:
    """Get one page of authenticated user history."""
    try:
        result = await _service.get_history(offset, limit, auth.token)
        return HistoryResponse(**result)
    except ContractLogicError as exc:
        logger.warning("History token validation failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get history")
        raise HTTPException(status_code=500, detail="Failed to retrieve history") from exc


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
