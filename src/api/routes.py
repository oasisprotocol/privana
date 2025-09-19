"""FastAPI routes exposing the Accounting module flows."""

import logging
from typing import Dict

from fastapi import APIRouter, HTTPException

from src.models.accounting import (
    DepositQuoteRequest,
    DepositQuoteResponse,
    IncludeDepositRequest,
    IncludeDepositResponse,
    LockFundsRequest,
    TransactionSubmissionResponse,
    TransferFundsRequest,
    TransferLockedFundsRequest,
    UnlockFundsRequest,
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


@router.post("/quotes/deposit", response_model=DepositQuoteResponse)
async def create_deposit_quote(payload: DepositQuoteRequest) -> DepositQuoteResponse:
    """Return deposit destination details for a user/token pair."""

    try:
        quote: Dict = _service.deposit_quote(payload.user_address, payload.token_id)
        return DepositQuoteResponse(**quote)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/deposits/native", response_model=IncludeDepositResponse)
async def include_native_deposit(payload: IncludeDepositRequest) -> IncludeDepositResponse:
    """Submit a native token deposit inclusion transaction."""

    try:
        result = _service.include_native_deposit(payload.dict())
        return IncludeDepositResponse(submission_id=result.submission_id, status=result.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - network errors
        logger.exception("Failed to submit native deposit inclusion")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/deposits/erc20", response_model=IncludeDepositResponse)
async def include_erc20_deposit(payload: IncludeDepositRequest) -> IncludeDepositResponse:
    """Submit an ERC20 deposit inclusion transaction."""

    try:
        result = _service.include_erc20_deposit(payload.dict())
        return IncludeDepositResponse(submission_id=result.submission_id, status=result.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to submit ERC20 deposit inclusion")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/lock", response_model=TransactionSubmissionResponse)
async def lock_funds(payload: LockFundsRequest) -> TransactionSubmissionResponse:
    """Lock user funds for a service with a signed authorization."""

    try:
        submission = _service.lock_funds(payload.dict())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to lock funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/transfer", response_model=TransactionSubmissionResponse)
async def transfer_funds(payload: TransferFundsRequest) -> TransactionSubmissionResponse:
    """Transfer funds between accounting balances using a user signature."""

    try:
        submission = _service.transfer_funds(payload.dict())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to transfer funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/funds/transfer-locked", response_model=TransactionSubmissionResponse)
async def transfer_locked_funds(payload: TransferLockedFundsRequest) -> TransactionSubmissionResponse:
    """Transfer locked funds based on a casino service signature."""

    try:
        submission = _service.transfer_locked_funds(payload.dict())
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
        submission = _service.unlock_funds(payload.dict())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to unlock funds")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.post("/withdrawals", response_model=TransactionSubmissionResponse)
async def request_withdrawal(payload: WithdrawalRequest) -> TransactionSubmissionResponse:
    """Commit a withdrawal request by validating the user's signature."""

    try:
        submission = _service.withdraw(payload.dict())
        return _wrap_submission(submission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to submit withdrawal request")
        raise HTTPException(status_code=500, detail="Failed to submit transaction") from exc


@router.get("/balances/{user_address}/{token_id}")
async def get_balance(user_address: str, token_id: str) -> Dict[str, str]:
    """Return a mocked balance view for clients during prototyping."""

    checksum_user = user_address
    try:
        checksum_user = _service.w3.to_checksum_address(user_address)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user address") from None

    return {
        "user_address": checksum_user,
        "token_id": token_id.lower(),
        "balance": "0", # TODO: Implement balance retrieval
        "token_symbol": _service.default_token_symbol,
        "chain_id": str(_service.chain_id),
    }
