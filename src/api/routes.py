"""FastAPI routes exposing the Accounting module flows."""

import logging
import time
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from hexbytes import HexBytes
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from src.auth.auth_token_service import get_auth_token_service
from src.auth.dependencies import (
    CurrentAccessToken,
    get_current_access_token_without_siwe_token,
    get_current_user,
    get_current_user_optional,
)
from src.auth.http import auth_exception, enforce_expected_origin, no_store_headers
from src.auth.jwt_service import get_jwt_service
from src.auth.rate_limiter import get_auth_rate_limiter, request_identity
from src.auth.siwe_config import get_siwe_config, get_siwe_configs
from src.auth.siwe_service import SiweAuthError, authenticate_siwe_message
from src.auth.token_store import get_token_store
from src.clients.rofl import TransactionRevertedError
from src.config import load_settings
from src.config.chain_config import (
    CHAIN_CONFIGS,
    MIN_DEPOSIT_ERC20_WEI,
    MIN_DEPOSIT_NATIVE_WEI,
    get_finality_depth,
)
from src.models.accounting import (
    BalanceResponse,
    BatchBalancesRequest,
    BatchBalancesResponse,
    CreateOnRampIntentRequest,
    CreateOnRampSessionRequest,
    DepositAddressRequest,
    DepositAddressResponse,
    DepositCheckRequest,
    DepositCheckResponse,
    ExpiredLocksResponse,
    HistoryResponse,
    JwtSiweTokenResponse,
    LockedFundsResponse,
    LockFundsRequest,
    LockNonceResponse,
    ModifyLockNonceResponse,
    ModifyLockRequest,
    OnRampRecord,
    OnRampSessionResponse,
    PendingDeposit,
    PendingDepositsResponse,
    PendingOnRampsResponse,
    PendingWithdrawalsResponse,
    SignOnRampUrlRequest,
    SignOnRampUrlResponse,
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
    UpdateOnRampRequest,
    WithdrawalInfoResponse,
    WithdrawalNonceResponse,
    WithdrawalRequest,
    WithdrawFromLockRequest,
    _normalise_hex,
)
from src.models.private_read import PrivateReadAuth
from src.services import onramp as onramp_service
from src.services.accounting_contract import (
    SubmissionResult,
    get_accounting_contract_service,
)
from src.services.deposit_discovery import (
    DiscoveryNotConfiguredError,
    DiscoveryRPCError,
    get_deposit_discovery_service,
)
from src.services.deposit_processor import get_deposit_processor
from src.services.onramp import (
    MoonPayAPIError,
    OnRampError,
    OnRampNotConfiguredError,
    decode_onramp_intent,
    dedupe_moonpay_transactions,
    fetch_moonpay_buy_transactions,
    fetch_moonpay_buy_transactions_by_external_id,
    moonpay_url_external_transaction_id,
    onramp_log_summary,
    onramp_record_from_intent,
    parse_webhook_body,
    pending_records_from_moonpay_transactions,
    short_address,
    short_identifier,
    sign_moonpay_url,
    verify_moonpay_webhook,
    webhook_updates,
)
from src.services.onramp_intent import (
    PROVIDER_MOONPAY,
    PROVIDER_TRANSAK,
    configured_provider,
    decode_intent,
)
from src.services.transak import (
    TransakAPIError,
    TransakConfig,
    TransakRateLimitError,
    TransakWebhookVerificationError,
    client_ip_from_values,
    create_transak_intent,
    get_transak_service,
    load_transak_config,
    pending_records_from_transak_orders,
    transak_webhook_log_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/accounting", tags=["Accounting"])

_service = get_accounting_contract_service()

_SIWE_TOKEN_HEADER = "X-SIWE-Token"
_ZERO_ADDRESS = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")
# MoonPay buy webhooks are a few KB; the webhook is unauthenticated, so cap the
# body before buffering it instead of trusting upstream proxy limits.
_ONRAMP_WEBHOOK_MAX_BODY_BYTES = 1024 * 1024
_ONRAMP_PENDING_MAX_INTENT_LOOKUPS = 10
_ONRAMP_INTENT_RATE_LIMIT = 20
_ONRAMP_PENDING_RATE_LIMIT = 20
_ONRAMP_SESSION_RATE_LIMIT = 10
_DEPOSITS_PENDING_RATE_LIMIT = 10
_SUPPORTED_DEPOSIT_ADDRESS_VERSIONS = {0}


def _validated_onramp_pending_intents(
    intent_ids: list[str] | None,
    *,
    user_address: str,
    deposit_address: str,
) -> list[tuple[str, dict[str, object]]]:
    if not intent_ids:
        return []
    deduped = list(dict.fromkeys(intent_ids))
    if len(deduped) > _ONRAMP_PENDING_MAX_INTENT_LOOKUPS:
        raise OnRampError("Too many externalTransactionId values")
    user = Web3.to_checksum_address(user_address)
    deposit = Web3.to_checksum_address(deposit_address)
    validated: list[tuple[str, dict[str, object]]] = []
    for intent_id in deduped:
        intent = decode_intent(intent_id, allow_expired=True)
        if Web3.to_checksum_address("0x" + str(intent["u"])) != user:
            raise OnRampError("Signed on-ramp intent does not belong to the caller")
        if Web3.to_checksum_address("0x" + str(intent["w"])) != deposit:
            raise OnRampError("Signed on-ramp intent does not match the deposit address")
        validated.append((intent_id, intent))
    return validated


def _mint_private_read_token(user_address: str, *, valid_until: Optional[int] = None) -> bytes:
    settings = load_settings()
    try:
        siwe_config = get_siwe_config(settings)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if valid_until is None:
        valid_until = int(time.time()) + settings.auth_token_validity_seconds
    token = get_auth_token_service().create_and_encrypt(
        domain=siwe_config.domain,
        user_addr=user_address,
        valid_until=valid_until,
    )
    return token


def _require_private_read_auth(
    current_user: Optional[str] = Depends(get_current_user_optional),
    authorization: Optional[str] = Header(None, alias="Authorization"),
    token: Optional[str] = Header(None, alias=_SIWE_TOKEN_HEADER),
) -> PrivateReadAuth:
    """Authenticate and return a locally-decoded private-read auth value.

    The user address comes from the JWT, or is decoded locally from the SIWE
    token. The token is forwarded to the contract, which enforces the access
    check. Use for private reads. Where the resolved address itself drives a
    state change such as deposit crediting or on-ramp correlation, use
    ``_require_resolved_private_read_auth``, which resolves on-chain via
    ``authSender()``.
    """
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


def _checksum_private_read_user(user_address: str) -> str:
    if not isinstance(user_address, str) or not Web3.is_address(user_address):
        raise ValueError("Invalid user_address provided")
    return Web3.to_checksum_address(user_address)


async def _require_resolved_private_read_auth(
    current_user: Optional[str] = Depends(get_current_user_optional),
    token: Optional[str] = Header(None, alias=_SIWE_TOKEN_HEADER),
) -> PrivateReadAuth:
    """Authenticate and return private-read auth for ownership-sensitive flows.

    Use this where the resolved address drives a state change such as deposit
    crediting or on-ramp correlation.

    JWT path: address from JWT, SIWE token minted server-side.
    SIWE path: address resolved on-chain via authSender(), token used directly.
    """
    if current_user:
        return PrivateReadAuth(_mint_private_read_token(current_user), current_user)
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
    return PrivateReadAuth(raw, address)


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


def _enforce_onramp_user_rate_limit(user_address: str, bucket: str, limit: int) -> None:
    """Rate-limit authenticated on-ramp work by resolved user, not proxy/IP shape."""

    settings = load_settings()
    retry_after = get_auth_rate_limiter().hit(
        bucket=bucket,
        key=Web3.to_checksum_address(user_address).lower(),
        limit=limit,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if retry_after is not None:
        raise auth_exception(
            status_code=429,
            detail="Too many on-ramp requests. Please retry later.",
            headers={"Retry-After": str(retry_after)},
        )


async def _registered_transak_token_id(
    config: TransakConfig,
    *,
    requested_token_id: str | None = None,
    requested_chain_id: int | None = None,
) -> str:
    """Resolve and require the configured ERC-20 in Privana's token registry."""

    if requested_chain_id is not None and requested_chain_id != config.chain_id:
        raise OnRampError("chain_id does not match the configured Transak asset")
    token_id = await _service.get_token_id(config.chain_id, config.token_address)
    token_id_hex = Web3.to_hex(token_id).lower()
    if requested_token_id is not None and requested_token_id.lower() != token_id_hex:
        raise OnRampError("token_id does not match the configured Transak asset")
    if not await _service.is_token_registered(token_id):
        raise OnRampNotConfiguredError("The configured Transak asset is not registered")
    return token_id_hex


def _validate_transak_intent(
    intent: dict[str, object],
    *,
    user_address: str,
    deposit_address: str,
    token_id: str,
    config: TransakConfig,
) -> None:
    if intent.get("p") != PROVIDER_TRANSAK:
        raise OnRampError("Signed on-ramp intent is not a Transak intent")
    if Web3.to_checksum_address("0x" + str(intent["u"])) != Web3.to_checksum_address(user_address):
        raise OnRampError("Signed on-ramp intent does not belong to the caller")
    if Web3.to_checksum_address("0x" + str(intent["w"])) != Web3.to_checksum_address(
        deposit_address
    ):
        raise OnRampError("Signed on-ramp intent does not match the deposit address")
    if int(intent["c"]) != config.chain_id:
        raise OnRampError("Signed on-ramp intent does not match the configured chain")
    if "0x" + str(intent["t"]).lower() != token_id.lower():
        raise OnRampError("Signed on-ramp intent does not match the configured token")
    if str(intent["a"]).lower() != config.canonical_asset_code:
        raise OnRampError("Signed on-ramp intent does not match the configured asset")


def _dedupe_onramp_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, object]] = []
    for row in rows:
        provider = str(row.get("provider") or "")
        identifier = str(row.get("provider_transaction_id") or row.get("transaction_id") or "")
        key = (provider, identifier)
        if identifier and key in seen:
            continue
        if identifier:
            seen.add(key)
        deduped.append(row)
    deduped.sort(
        key=lambda row: (
            int(row.get("updated_at") or 0),
            int(row.get("created_at") or 0),
            str(row.get("provider_transaction_id") or ""),
        ),
        reverse=True,
    )
    return deduped


def _transak_rate_limit_exception(exc: TransakRateLimitError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
    return HTTPException(status_code=429, detail=str(exc), headers=headers)


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
        settings = load_settings()
        native_supported = {
            entry["chain_id"] for entry in settings.token_infos if not entry.get("token_address")
        }
        erc20_supported = {
            entry["chain_id"] for entry in settings.token_infos if entry.get("token_address")
        }
        min_deposit: dict[str, dict[str, str]] = {}
        for cid in MIN_DEPOSIT_NATIVE_WEI:
            chain_min: dict[str, str] = {}
            if cid in native_supported:
                chain_min["native"] = str(MIN_DEPOSIT_NATIVE_WEI.get(cid, 0))
            if cid in erc20_supported:
                chain_min["erc20"] = str(MIN_DEPOSIT_ERC20_WEI.get(cid, 0))
            min_deposit[str(cid)] = chain_min

        return DepositAddressResponse(
            deposit_address=address,
            chain_type=payload.chain_type,
            version=payload.version,
            min_deposit=min_deposit,
            finality_depth={str(cid): get_finality_depth(cid) for cid in settings.chain_rpc_urls},
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
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> DepositCheckResponse:
    """Verify a deposit and start the sweep in the background.

    Returns 200 with status="credited" for idempotent replays.
    Returns 202 with status="pending" when sweep is started or in progress.
    Clients poll GET /deposits/status/{deposit_id} for completion.
    """
    try:
        processor = get_deposit_processor()
        result = await processor.process_deposit(
            auth=auth,
            chain_type=payload.chain_type,
            chain_id=payload.chain_id,
            tx_hash=payload.tx_hash,
            amount=payload.amount,
            log_index=payload.log_index,
            version=payload.version,
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
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> DepositCheckResponse:
    """Poll deposit status by deposit_id.

    Returns credited/pending/error based on sweep state.
    Falls through to on-chain check when no in-memory record exists.
    """
    beneficiary = auth.user_address
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


@router.get("/deposits/pending", response_model=PendingDepositsResponse)
async def get_pending_deposits(
    request: Request,
    chain_id: int = Query(..., description="Source chain ID to scan"),
    version: int = Query(0, description="Deposit address derivation version"),
    token_address: Optional[str] = Query(
        None, description="Optional registered ERC20 contract to scan for (default: all)"
    ),
    lookback_blocks: Optional[int] = Query(
        None,
        gt=0,
        description=(
            "Scan window in blocks, rounded up to full scan chunks and clamped "
            "to the chain's maximum (default: ~1h)"
        ),
    ),
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> PendingDepositsResponse:
    """Discover uncredited external-wallet ERC20 deposits to the caller's deposit address.

    Read-only: scans finalized source-chain Transfer logs and returns candidates
    shaped for POST /deposits/check. Native transfers emit no logs and are not
    discoverable — submit their tx hash to /deposits/check directly.
    """
    _enforce_auth_rate_limit(request, "deposits_pending", _DEPOSITS_PENDING_RATE_LIMIT)
    if chain_id not in CHAIN_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Unsupported chain_id {chain_id}")
    if version not in _SUPPORTED_DEPOSIT_ADDRESS_VERSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported version {version}")
    if token_address is not None and not Web3.is_address(token_address):
        raise HTTPException(status_code=400, detail="Invalid token_address")
    try:
        deposit_address = await _service.get_deposit_address("evm", version, auth.token)
        result = await get_deposit_discovery_service().discover_pending_deposits(
            deposit_address=deposit_address,
            beneficiary=auth.user_address,
            chain_id=chain_id,
            version=version,
            token_address=token_address,
            lookback_blocks=lookback_blocks,
        )
        return PendingDepositsResponse(
            pending=[
                PendingDeposit(
                    chain_id=d.chain_id,
                    tx_hash=d.tx_hash,
                    log_index=d.log_index,
                    amount=str(d.amount),
                    token_address=d.token_address,
                    token_id=d.token_id_hex,
                    block_number=d.block_number,
                    version=d.version,
                    status=d.status,
                    deposit_id=d.deposit_id_hex,
                )
                for d in result.pending
            ],
            scanned_from_block=result.scanned_from_block,
            scanned_to_block=result.scanned_to_block,
        )
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in deposit discovery: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except DiscoveryRPCError as exc:
        logger.warning("Deposit discovery RPC failure: %s", exc)
        raise HTTPException(status_code=502, detail="Source-chain RPC unavailable") from exc
    except DiscoveryNotConfiguredError as exc:
        # Deployment fault (chain has no RPC URL), not caller error: 503 like
        # onramp's not-configured path, with the config detail kept server-side.
        logger.error("Deposit discovery misconfigured: %s", exc)
        raise HTTPException(
            status_code=503, detail="Deposit discovery unavailable for this chain"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to discover pending deposits")
        raise HTTPException(status_code=500, detail="Internal error") from exc


@router.post("/onramp/intent", response_model=OnRampRecord)
async def create_onramp_intent(
    payload: CreateOnRampIntentRequest,
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> OnRampRecord:
    """Create a signed Privana intent for the deployment-selected provider."""
    try:
        _enforce_onramp_user_rate_limit(
            auth.user_address,
            "onramp_intent",
            _ONRAMP_INTENT_RATE_LIMIT,
        )
        provider = configured_provider()
        deposit_address = await _service.get_deposit_address("evm", 0, auth.token)
        deposit_address = Web3.to_checksum_address(deposit_address)
        if (
            payload.wallet_address
            and Web3.to_checksum_address(payload.wallet_address) != deposit_address
        ):
            raise OnRampError("wallet_address must be the Privana deposit address")

        if provider == PROVIDER_MOONPAY:
            if not payload.moonpay_currency_code:
                raise OnRampError("moonpay_currency_code is required for MoonPay")
            record = onramp_service.create_onramp_intent(
                user_address=auth.user_address,
                wallet_address=deposit_address,
                token_id=payload.token_id,
                chain_id=payload.chain_id,
                moonpay_currency_code=payload.moonpay_currency_code,
            )
        else:
            config = load_transak_config()
            await _registered_transak_token_id(
                config,
                requested_token_id=payload.token_id,
                requested_chain_id=payload.chain_id,
            )
            record = create_transak_intent(
                user_address=auth.user_address,
                wallet_address=deposit_address,
                token_id=payload.token_id,
                chain_id=payload.chain_id,
                config=config,
            )
        logger.info("On-ramp intent created: %s", onramp_log_summary(record))
        return OnRampRecord(**record)
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in on-ramp intent lookup: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/onramp/sign-url", response_model=SignOnRampUrlResponse)
async def sign_onramp_url(
    payload: SignOnRampUrlRequest,
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> SignOnRampUrlResponse:
    """Sign a validated MoonPay widget URL for the caller's Privana deposit address."""
    try:
        if configured_provider() != PROVIDER_MOONPAY:
            raise OnRampNotConfiguredError("MoonPay URL signing is disabled")
        deposit_address = await _service.get_deposit_address("evm", 0, auth.token)
        deposit_address = Web3.to_checksum_address(deposit_address)
        intent_id = moonpay_url_external_transaction_id(payload.url)
        if not intent_id:
            raise OnRampError("MoonPay URL is missing externalTransactionId")
        intent = decode_onramp_intent(intent_id)
        if Web3.to_checksum_address("0x" + str(intent["u"])) != Web3.to_checksum_address(
            auth.user_address
        ):
            raise OnRampError("MoonPay externalTransactionId does not belong to the caller")
        if Web3.to_checksum_address("0x" + str(intent["w"])) != deposit_address:
            raise OnRampError("MoonPay externalTransactionId does not match the deposit address")

        signature = sign_moonpay_url(
            payload.url,
            expected_wallet_address=deposit_address,
            user_address=auth.user_address,
            expected_external_transaction_id=intent_id,
            expected_currency_code=str(intent["a"]),
        )
        logger.info(
            "On-ramp URL signed: user=%s deposit=%s currency=%s",
            short_address(auth.user_address),
            short_address(deposit_address),
            intent.get("a"),
        )
        return SignOnRampUrlResponse(signature=signature)
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in on-ramp deposit address lookup: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Failed to sign MoonPay URL")
        raise HTTPException(status_code=500, detail="Failed to sign MoonPay URL") from exc


@router.post("/onramp/session", response_model=OnRampSessionResponse)
async def create_onramp_session(
    payload: CreateOnRampSessionRequest,
    request: Request,
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> OnRampSessionResponse:
    """Create one short-lived Transak widget session for a caller-owned intent."""

    try:
        _enforce_onramp_user_rate_limit(
            auth.user_address,
            "onramp_session",
            _ONRAMP_SESSION_RATE_LIMIT,
        )
        if configured_provider() != PROVIDER_TRANSAK:
            raise OnRampNotConfiguredError("Transak on-ramp sessions are disabled")
        config = load_transak_config()
        intent = decode_intent(payload.transaction_id)
        deposit_address = await _service.get_deposit_address("evm", 0, auth.token)
        deposit_address = Web3.to_checksum_address(deposit_address)
        token_id = await _registered_transak_token_id(config)
        _validate_transak_intent(
            intent,
            user_address=auth.user_address,
            deposit_address=deposit_address,
            token_id=token_id,
            config=config,
        )
        user_ip = client_ip_from_values(
            request.headers.getlist(config.client_ip_header),
            header_name=config.client_ip_header,
        )
        session = await get_transak_service().create_widget_session(
            transaction_id=payload.transaction_id,
            wallet_address=deposit_address,
            user_ip=user_ip,
            config=config,
        )
        logger.info(
            "Transak on-ramp session created: user=%s deposit=%s asset=%s network=%s",
            short_address(auth.user_address),
            short_address(deposit_address),
            config.canonical_asset_code,
            config.canonical_network,
        )
        return OnRampSessionResponse(**session)
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in Transak session validation: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TransakRateLimitError as exc:
        raise _transak_rate_limit_exception(exc) from exc
    except TransakAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/onramp/pending", response_model=PendingOnRampsResponse)
async def get_pending_onramps(
    request: Request,
    external_transaction_id: list[str] | None = Query(
        default=None,
        alias="externalTransactionId",
        description=(
            "Optional signed Privana intent values to look up exactly through their "
            "encoded provider."
        ),
    ),
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> PendingOnRampsResponse:
    """Return completed provider purchases that still need deposit verification."""
    try:
        _enforce_onramp_user_rate_limit(
            auth.user_address,
            "onramp_pending",
            _ONRAMP_PENDING_RATE_LIMIT,
        )
        deposit_address = await _service.get_deposit_address("evm", 0, auth.token)
        deposit_address = Web3.to_checksum_address(deposit_address)
        exact_intents = _validated_onramp_pending_intents(
            external_transaction_id,
            user_address=auth.user_address,
            deposit_address=deposit_address,
        )
        rows: list[dict[str, object]] = []
        exact_diagnostics: list[dict[str, object]] = []
        exact_lookup_succeeded = False
        transak_config: TransakConfig | None = None
        transak_token_id: str | None = None

        for intent_id, intent in exact_intents:
            provider = str(intent["p"])
            if provider == PROVIDER_MOONPAY:
                try:
                    transactions = await fetch_moonpay_buy_transactions_by_external_id(intent_id)
                    exact_rows, diagnostics = pending_records_from_moonpay_transactions(
                        transactions,
                        expected_user_address=auth.user_address,
                        expected_wallet_address=deposit_address,
                    )
                    exact_lookup_succeeded = True
                    rows.extend(exact_rows)
                    exact_diagnostics.append(
                        {"provider": PROVIDER_MOONPAY, "diagnostics": diagnostics}
                    )
                except OnRampError as exc:
                    exact_diagnostics.append({"provider": PROVIDER_MOONPAY, "error": str(exc)})
            elif provider == PROVIDER_TRANSAK:
                try:
                    if transak_config is None or transak_token_id is None:
                        candidate_config = load_transak_config()
                        candidate_token_id = await _registered_transak_token_id(candidate_config)
                        transak_config = candidate_config
                        transak_token_id = candidate_token_id
                    _validate_transak_intent(
                        intent,
                        user_address=auth.user_address,
                        deposit_address=deposit_address,
                        token_id=transak_token_id,
                        config=transak_config,
                    )
                    orders = await get_transak_service().get_orders_by_partner_order_id(
                        intent_id,
                        issued_at=int(intent["iat"]),
                        config=transak_config,
                    )
                    exact_rows, diagnostics = pending_records_from_transak_orders(
                        orders,
                        expected_user_address=auth.user_address,
                        expected_wallet_address=deposit_address,
                        expected_token_id=transak_token_id,
                        expected_transaction_id=intent_id,
                        config=transak_config,
                    )
                    exact_lookup_succeeded = True
                    rows.extend(exact_rows)
                    exact_diagnostics.append(
                        {"provider": PROVIDER_TRANSAK, "diagnostics": diagnostics}
                    )
                except OnRampError as exc:
                    exact_diagnostics.append({"provider": PROVIDER_TRANSAK, "error": str(exc)})

        provider = configured_provider()
        wallet_diagnostics: dict[str, object]
        wallet_error: OnRampError | None = None
        wallet_lookup_succeeded = False
        if provider == PROVIDER_MOONPAY:
            try:
                customer_transactions = await fetch_moonpay_buy_transactions(
                    external_customer_id=auth.user_address
                )
                wallet_rows, wallet_diagnostics = pending_records_from_moonpay_transactions(
                    dedupe_moonpay_transactions(customer_transactions),
                    expected_user_address=auth.user_address,
                    expected_wallet_address=deposit_address,
                )
                wallet_lookup_succeeded = True
                rows.extend(wallet_rows)
            except OnRampError as exc:
                wallet_error = exc
                wallet_diagnostics = {"provider": PROVIDER_MOONPAY, "error": str(exc)}
        else:
            try:
                if transak_config is None or transak_token_id is None:
                    transak_config = load_transak_config()
                    transak_token_id = await _registered_transak_token_id(transak_config)
                wallet_orders = await get_transak_service().get_orders_by_wallet(
                    deposit_address,
                    config=transak_config,
                )
                wallet_rows, wallet_diagnostics = pending_records_from_transak_orders(
                    wallet_orders,
                    expected_user_address=auth.user_address,
                    expected_wallet_address=deposit_address,
                    expected_token_id=transak_token_id,
                    config=transak_config,
                )
                wallet_lookup_succeeded = True
                rows.extend(wallet_rows)
            except OnRampError as exc:
                wallet_error = exc
                wallet_diagnostics = {"provider": PROVIDER_TRANSAK, "error": str(exc)}

        if not wallet_lookup_succeeded and not exact_lookup_succeeded and wallet_error is not None:
            raise wallet_error

        rows = _dedupe_onramp_rows(rows)
        diagnostics = {
            "wallet_lookup": wallet_diagnostics,
            "exact_lookups": exact_diagnostics,
        }
        logger.info(
            "On-ramp pending lookup: user=%s deposit=%s returned=%d diagnostics=%s",
            short_address(auth.user_address),
            short_address(deposit_address),
            len(rows),
            diagnostics,
        )
        return PendingOnRampsResponse(pending=[OnRampRecord(**row) for row in rows])
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in pending on-ramp lookup: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TransakRateLimitError as exc:
        raise _transak_rate_limit_exception(exc) from exc
    except TransakAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except MoonPayAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/onramp/moonpay/webhook")
async def moonpay_onramp_webhook(
    request: Request,
    signature: str = Header(..., alias="Moonpay-Signature-V2"),
) -> dict[str, bool]:
    """Verify and log MoonPay buy webhooks."""
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > _ONRAMP_WEBHOOK_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="MoonPay webhook payload is too large")
        buffer.extend(chunk)
    raw_body = bytes(buffer)
    try:
        verify_moonpay_webhook(raw_body, signature)
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OnRampError as exc:
        # The signature is the webhook's authentication, so report 401 instead
        # of the 400 used for payload validation failures below.
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    try:
        payload = parse_webhook_body(raw_body)
        _transaction_id, updates = webhook_updates(payload)
        id_source = (
            "externalTransactionId" if updates.get("external_transaction_id") else "moonpay_id"
        )
        logger.info(
            "On-ramp webhook accepted: tx=%s id_source=%s updates=%s",
            short_identifier(updates.get("moonpay_transaction_id")),
            id_source,
            {
                "has_signed_intent": bool(updates.get("external_transaction_id")),
                "moonpay_transaction_id": short_identifier(updates.get("moonpay_transaction_id")),
                "status": updates.get("status"),
                "user_address": short_address(updates.get("user_address")),
                "wallet_address": short_address(updates.get("wallet_address")),
                "has_on_chain_tx_hash": bool(updates.get("on_chain_tx_hash")),
                "moonpay_currency_code": updates.get("moonpay_currency_code"),
                "failure_reason_present": bool(updates.get("failure_reason")),
            },
        )
        return {"ok": True}
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/onramp/transak/webhook")
async def transak_onramp_webhook(request: Request) -> dict[str, bool]:
    """Verify and log Transak webhooks as non-authoritative observability signals."""

    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > _ONRAMP_WEBHOOK_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Transak webhook payload is too large")
        buffer.extend(chunk)
    try:
        payload = await get_transak_service().verify_webhook(bytes(buffer))
        logger.info("Transak on-ramp webhook accepted: %s", transak_webhook_log_summary(payload))
        return {"ok": True}
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TransakWebhookVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/onramp/{transaction_id}", response_model=OnRampRecord)
async def update_onramp(
    transaction_id: str,
    payload: UpdateOnRampRequest,
    auth: PrivateReadAuth = Depends(_require_resolved_private_read_auth),
) -> OnRampRecord:
    """Validate and echo caller-owned MoonPay transaction metadata."""
    try:
        deposit_address = await _service.get_deposit_address("evm", 0, auth.token)
        deposit_address = Web3.to_checksum_address(deposit_address)
        if (
            payload.wallet_address
            and Web3.to_checksum_address(payload.wallet_address) != deposit_address
        ):
            raise OnRampError("wallet_address must be the Privana deposit address")

        beneficiary = Web3.to_checksum_address(auth.user_address)
        intent = decode_onramp_intent(transaction_id, allow_expired=True)
        if Web3.to_checksum_address("0x" + str(intent["u"])) != beneficiary:
            raise HTTPException(
                status_code=403,
                detail="on-ramp transaction does not belong to the caller",
            )
        if Web3.to_checksum_address("0x" + str(intent["w"])) != deposit_address:
            raise HTTPException(
                status_code=403,
                detail="on-ramp transaction does not match the caller deposit address",
            )

        record = onramp_record_from_intent(transaction_id, intent)
        updates = payload.model_dump(exclude_none=True)
        if payload.token_id is not None and payload.token_id != record.get("token_id"):
            raise OnRampError("token_id does not match signed on-ramp intent")
        if payload.chain_id is not None and payload.chain_id != record.get("chain_id"):
            raise OnRampError("chain_id does not match signed on-ramp intent")
        if payload.base_currency_code:
            updates["base_currency_code"] = payload.base_currency_code
        updates["wallet_address"] = deposit_address
        record.update(updates)
        if payload.deposit_tx_hash and not record.get("deposit_triggered_at"):
            record["deposit_triggered_at"] = int(time.time())
        if payload.on_chain_tx_hash:
            record["status"] = "completed"
            record["updated_at"] = int(time.time())
        logger.info(
            "On-ramp metadata accepted without persistence: record=%s",
            onramp_log_summary(record),
        )
        return OnRampRecord(**record)
    except ContractLogicError as exc:
        if "Siwe" in str(exc) or "InvalidSiwe" in str(exc):
            raise HTTPException(status_code=401, detail="Invalid or expired SIWE token") from exc
        logger.error("Contract revert in on-ramp update: %s", exc)
        raise HTTPException(status_code=422, detail="Contract call failed") from exc
    except OnRampNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OnRampError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        submission = await _service.withdraw_from_lock(payload.model_dump(), auth)
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
        user_address = _checksum_private_read_user(auth.user_address)
        result = await _service.get_locked_funds(auth.token, service_address)
        return LockedFundsResponse(
            **{
                **result,
                "user_address": user_address,
                "locks": [{**lock, "user_address": user_address} for lock in result["locks"]],
            }
        )
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
        user_address = _checksum_private_read_user(auth.user_address)
        result = await _service.get_balance(auth.token, token_id)
        return BalanceResponse(**{**result, "user_address": user_address})
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
        user_address = _checksum_private_read_user(auth.user_address)
        result = await _service.get_expired_locks(auth.token)
        return ExpiredLocksResponse(
            **{
                **result,
                "user_address": user_address,
                "expired_locks": [
                    {**lock, "user_address": user_address} for lock in result["expired_locks"]
                ],
            }
        )
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
        user_address = _checksum_private_read_user(auth.user_address)
        result = await _service.get_batch_balances(auth.token, payload.token_ids)
        return BatchBalancesResponse(**{**result, "user_address": user_address})
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
        user_address = _checksum_private_read_user(auth.user_address)
        result = await _service.get_total_locked_balance(auth.token, token_id)
        return TotalLockedBalanceResponse(**{**result, "user_address": user_address})
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


@router.post("/auth/jwt/siwe-token", response_model=JwtSiweTokenResponse)
async def exchange_jwt_for_siwe_token(
    response: Response,
    access_token: CurrentAccessToken = Depends(get_current_access_token_without_siwe_token),
) -> JwtSiweTokenResponse:
    """Mint a private-read SIWE token for the authenticated JWT subject."""
    settings = load_settings()
    now = int(time.time())
    expires_in = min(settings.auth_token_validity_seconds, access_token.expires_at - now)
    if expires_in <= 0:
        raise HTTPException(
            status_code=401,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    siwe_token = _mint_private_read_token(access_token.address, valid_until=now + expires_in)
    response.headers.update(no_store_headers())
    return JwtSiweTokenResponse(
        siwe_token="0x" + siwe_token.hex(),
        address=access_token.address,
        expires_in=expires_in,
    )


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
