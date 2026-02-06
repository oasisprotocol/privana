"""FastAPI routes exposing the Accounting module flows."""

import asyncio
import logging
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException

from src.models.accounting import (
    BatchBalancesRequest,
    BatchBalancesResponse,
    DepositQuoteRequest,
    DepositQuoteResponse,
    ExpiredLocksResponse,
    IncludeDepositRequest,
    IncludeDepositResponse,
    LockedFundsResponse,
    LockFundsRequest,
    ModifyLockRequest,
    PendingWithdrawalsResponse,
    TokenInfoResponse,
    TotalLockedBalanceResponse,
    TransactionSubmissionResponse,
    TransferFundsRequest,
    TransferLockedFundsRequest,
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


def _wrap_submission(result: SubmissionResult) -> TransactionSubmissionResponse:
    return TransactionSubmissionResponse(
        submission_id=result.submission_id,
        status=result.status,
        detail=result.detail,
    )


@router.post("/quote/deposit", response_model=DepositQuoteResponse)
async def create_deposit_quote(payload: DepositQuoteRequest) -> DepositQuoteResponse:
    """Return deposit destination details and transaction data for a user/token/amount."""

    try:
        quote: Dict = await asyncio.to_thread(
            _service.deposit_quote, payload.user_address, payload.token_id, payload.amount
        )
        return DepositQuoteResponse(**quote)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/deposits", response_model=IncludeDepositResponse)
async def include_deposit(payload: IncludeDepositRequest) -> IncludeDepositResponse:
    """Submit a deposit inclusion transaction (automatically detects native/ERC20)."""

    try:
        result = await asyncio.to_thread(_service.include_deposit, payload.model_dump())
        return IncludeDepositResponse(submission_id=result.submission_id, status=result.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - network errors
        logger.exception("Failed to submit deposit inclusion")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/lock", response_model=TransactionSubmissionResponse)
async def lock_funds(payload: LockFundsRequest) -> TransactionSubmissionResponse:
    """Lock user funds for a service with a signed authorization."""

    try:
        submission = await asyncio.to_thread(_service.lock_funds, payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to lock funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/modify-lock", response_model=TransactionSubmissionResponse)
async def modify_lock(payload: ModifyLockRequest) -> TransactionSubmissionResponse:
    """Modify an existing lock by adding funds and/or extending the expiry."""

    try:
        submission = await asyncio.to_thread(_service.modify_lock, payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to modify lock")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/transfer", response_model=TransactionSubmissionResponse)
async def transfer_funds(payload: TransferFundsRequest) -> TransactionSubmissionResponse:
    """Transfer funds between accounting balances using a user signature."""

    try:
        submission = await asyncio.to_thread(_service.transfer_funds, payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to transfer funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/transfer-locked", response_model=TransactionSubmissionResponse)
async def transfer_locked_funds(
    payload: TransferLockedFundsRequest,
) -> TransactionSubmissionResponse:
    """Transfer locked funds based on a casino service signature."""

    try:
        submission = await asyncio.to_thread(_service.transfer_locked_funds, payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to transfer locked funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/unlock", response_model=TransactionSubmissionResponse)
async def unlock_funds(payload: UnlockFundsRequest) -> TransactionSubmissionResponse:
    """Unlock funds when lock expiry has passed."""

    try:
        submission = await asyncio.to_thread(_service.unlock_funds, payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to unlock funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/withdraw", response_model=TransactionSubmissionResponse)
async def request_withdrawal(payload: WithdrawalRequest) -> TransactionSubmissionResponse:
    """Request a withdrawal by validating the user's signature. Must be resolved in a later block."""

    try:
        submission = await asyncio.to_thread(_service.request_withdrawal, payload.model_dump())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to submit withdrawal request")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.get("/withdraw/pending/{user_address}", response_model=PendingWithdrawalsResponse)
async def get_pending_withdrawals(user_address: str) -> PendingWithdrawalsResponse:
    """Get all pending (unresolved) withdrawal requests for a user."""

    try:
        result = await asyncio.to_thread(_service.get_pending_withdrawals, user_address)
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
        result = await asyncio.to_thread(_service.get_withdrawal_nonce, user_address)
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
        result = await asyncio.to_thread(_service.get_withdrawal, index)
        return WithdrawalInfoResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get withdrawal info")
        raise HTTPException(status_code=500, detail="Failed to retrieve withdrawal info") from exc


@router.get("/funds/locked/{user_address}", response_model=LockedFundsResponse)
async def get_locked_funds(
    user_address: str, service_address: Optional[str] = None
) -> LockedFundsResponse:
    """Get locked funds for a user, optionally filtered by service address."""

    try:
        result = await asyncio.to_thread(_service.get_locked_funds, user_address, service_address)
        return LockedFundsResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to get locked funds")
        raise HTTPException(status_code=500, detail="Failed to retrieve locked funds") from exc


@router.get("/balances/{user_address}/{token_id}")
async def get_balance(user_address: str, token_id: str) -> Dict[str, str]:
    """Get the user's balance for a specific token from the contract."""

    def _get_balance_data():
        balance = _service.get_balance(user_address, token_id)
        checksum_user = _service.w3.to_checksum_address(user_address)

        token_hex = _service._require_hex(token_id, "token_id", expected_len=32)
        token_symbol = _service._get_token_symbol(token_hex)
        token_context = _service._get_token_context(token_hex)

        return {
            "user_address": checksum_user,
            "token_id": token_id.lower(),
            "balance": str(balance),
            "token_symbol": token_symbol,
            "chain_id": str(token_context.chain_id),
        }

    try:
        return await asyncio.to_thread(_get_balance_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get balance")
        raise HTTPException(status_code=500, detail="Failed to retrieve balance") from exc


@router.post("/funds/unlock-all-expired", response_model=TransactionSubmissionResponse)
async def unlock_all_expired_locks(
    payload: UnlockAllExpiredLocksRequest,
) -> TransactionSubmissionResponse:
    """Unlock all expired locks for a user."""

    try:
        submission = await asyncio.to_thread(
            _service.unlock_all_expired_locks, payload.model_dump()
        )
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to unlock all expired locks")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.get("/funds/expired/{user_address}", response_model=ExpiredLocksResponse)
async def get_expired_locks(user_address: str) -> ExpiredLocksResponse:
    """Get all expired locks for a user."""

    try:
        result = await asyncio.to_thread(_service.get_expired_locks, user_address)
        return ExpiredLocksResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get expired locks")
        raise HTTPException(status_code=500, detail="Failed to retrieve expired locks") from exc


@router.post("/balances/batch", response_model=BatchBalancesResponse)
async def get_batch_balances(payload: BatchBalancesRequest) -> BatchBalancesResponse:
    """Get balances for multiple tokens for a user."""

    try:
        result = await asyncio.to_thread(
            _service.get_balances, payload.user_address, payload.token_ids
        )
        return BatchBalancesResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get batch balances")
        raise HTTPException(status_code=500, detail="Failed to retrieve balances") from exc


@router.get(
    "/funds/locked/total/{user_address}/{token_id}", response_model=TotalLockedBalanceResponse
)
async def get_total_locked_balance(user_address: str, token_id: str) -> TotalLockedBalanceResponse:
    """Get total locked balance for a specific token across all locks."""

    try:
        result = await asyncio.to_thread(_service.get_total_locked_balance, user_address, token_id)
        return TotalLockedBalanceResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get total locked balance")
        raise HTTPException(
            status_code=500, detail="Failed to retrieve total locked balance"
        ) from exc


@router.get("/tokens/{token_id}", response_model=TokenInfoResponse)
async def get_token_info(token_id: str) -> TokenInfoResponse:
    """Get information about a registered token."""

    try:
        result = await asyncio.to_thread(_service.get_token_info, token_id)
        return TokenInfoResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get token info")
        raise HTTPException(status_code=500, detail="Failed to retrieve token info") from exc
