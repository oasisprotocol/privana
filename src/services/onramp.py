"""MoonPay on-ramp transaction tracking and verification helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
from diskcache import Cache
from web3 import Web3

from src.config import load_settings

_ONRAMP_INTENT_PREFIX = "privana_"
_ONRAMP_LOCK_AUTHORIZATION_PREFIX = "onramp-lock-authorization:"
_ONRAMP_INTENT_VERSION = 1
_ONRAMP_INTENT_TTL_SECONDS = 24 * 60 * 60
_ONRAMP_INTENT_MAX_LENGTH = 255
_ONRAMP_INTENT_MAX_CURRENCY_CODE_BYTES = 32
_ONRAMP_INTENT_STRUCT = struct.Struct(">BIII20s20s32s8sB")
_MOONPAY_TRANSACTION_PAGE_LIMIT = 50
_MOONPAY_TRANSACTION_MAX_PAGES = 10
# Cap stored-authorization lifetime: the signed authorization_deadline is
# client-chosen and unbounded on-chain, so without a cap one intent could pin
# a cache entry indefinitely. SDK default deadline is 7 days.
_LOCK_AUTHORIZATION_MAX_TTL_SECONDS = 30 * 24 * 60 * 60

logger = logging.getLogger(__name__)

_onramp_lock_authorization_store_instance: "OnRampLockAuthorizationStore | None" = None


class OnRampError(ValueError):
    """Raised when an on-ramp request is invalid."""


class OnRampNotConfiguredError(OnRampError):
    """Raised when MoonPay config is missing."""


class MoonPayAPIError(OnRampError):
    """Raised when MoonPay's server API cannot be queried."""


class OnRampLockAuthorizationStore:
    """Disk-backed storage for signed post-deposit lock policies."""

    def __init__(self) -> None:
        settings = load_settings()
        storage_root = Path(settings.auth_token_storage_dir)
        self._cache = Cache(
            str(storage_root / "onramp_lock_authorizations"),
            disk_min_file_size=0,
            sqlite_journal_mode="WAL",
        )

    def close(self) -> None:
        """Close the underlying cache."""

        self._cache.close()

    def put(self, transaction_id: str, lock_authorization: dict[str, Any]) -> dict[str, Any]:
        if not transaction_id:
            raise OnRampError("Missing transaction id")
        normalised = _lock_authorization_or_error(lock_authorization)
        now = int(time.time())
        existing = self._cache.get(self._key(transaction_id))
        created_at = int(existing.get("created_at", now)) if isinstance(existing, dict) else now
        record = {
            "transaction_id": transaction_id,
            "lock_authorization": normalised,
            "created_at": created_at,
            "updated_at": now,
        }
        self._cache.set(
            self._key(transaction_id),
            record,
            expire=_lock_authorization_ttl_seconds(normalised, now),
        )
        return dict(normalised)

    def get(self, transaction_id: str) -> dict[str, Any] | None:
        if not transaction_id:
            return None
        record = self._cache.get(self._key(transaction_id))
        if not isinstance(record, dict):
            return None
        lock_authorization = record.get("lock_authorization")
        return dict(lock_authorization) if isinstance(lock_authorization, dict) else None

    @staticmethod
    def _key(transaction_id: str) -> str:
        return f"{_ONRAMP_LOCK_AUTHORIZATION_PREFIX}{transaction_id}"


def get_onramp_lock_authorization_store() -> OnRampLockAuthorizationStore:
    global _onramp_lock_authorization_store_instance
    if _onramp_lock_authorization_store_instance is None:
        _onramp_lock_authorization_store_instance = OnRampLockAuthorizationStore()
    return _onramp_lock_authorization_store_instance


def store_onramp_lock_authorization(
    transaction_id: str,
    lock_authorization: dict[str, Any],
) -> dict[str, Any]:
    """Persist the signed lock policy associated with a signed on-ramp intent."""

    return get_onramp_lock_authorization_store().put(transaction_id, lock_authorization)


def get_onramp_lock_authorization(transaction_id: str) -> dict[str, Any] | None:
    """Return a stored signed lock policy for an on-ramp intent, when one exists."""

    return get_onramp_lock_authorization_store().get(transaction_id)


def create_onramp_intent(
    *,
    user_address: str,
    wallet_address: str,
    token_id: str,
    chain_id: int,
    moonpay_currency_code: str,
) -> dict[str, Any]:
    """Create a signed, self-contained externalTransactionId record."""

    now = int(time.time())
    payload: dict[str, Any] = {
        "v": _ONRAMP_INTENT_VERSION,
        "u": _address_payload(user_address),
        "w": _address_payload(wallet_address),
        "t": _hex_payload(token_id, byte_length=32, field_name="token_id"),
        "c": chain_id,
        "m": _currency_code_or_error(moonpay_currency_code, "moonpay_currency_code"),
        "iat": now,
        "exp": now + _ONRAMP_INTENT_TTL_SECONDS,
        "n": secrets.token_hex(8),
    }

    transaction_id = _encode_intent_payload(payload)
    if len(transaction_id) > _ONRAMP_INTENT_MAX_LENGTH:
        raise OnRampError("MoonPay externalTransactionId is too large")
    return onramp_record_from_intent(transaction_id, payload)


def decode_onramp_intent(
    transaction_id: str,
    *,
    allow_expired: bool = False,
) -> dict[str, Any]:
    """Verify and decode a signed Privana on-ramp externalTransactionId."""

    if not transaction_id.startswith(_ONRAMP_INTENT_PREFIX):
        raise OnRampError("MoonPay externalTransactionId is not a Privana intent")
    token = transaction_id.removeprefix(_ONRAMP_INTENT_PREFIX)
    payload_b64, separator, signature_b64 = token.partition(".")
    if not separator or not payload_b64 or not signature_b64:
        raise OnRampError("MoonPay externalTransactionId is malformed")

    expected = _intent_signature(payload_b64)
    try:
        provided = _b64url_decode(signature_b64)
    except ValueError as exc:
        raise OnRampError("MoonPay externalTransactionId signature is malformed") from exc
    if not hmac.compare_digest(expected, provided):
        raise OnRampError("MoonPay externalTransactionId signature mismatch")

    try:
        payload = _decode_intent_payload(_b64url_decode(payload_b64))
    except ValueError:
        raise OnRampError("MoonPay externalTransactionId payload is malformed")

    _validate_intent_payload(payload)
    if not allow_expired and int(payload["exp"]) < int(time.time()):
        raise OnRampError("MoonPay externalTransactionId has expired")
    return payload


def onramp_record_from_intent(transaction_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an API-visible on-ramp record from a signed intent payload."""

    created_at = int(payload["iat"])
    record: dict[str, Any] = {
        "transaction_id": transaction_id,
        "external_transaction_id": transaction_id,
        "status": "pending",
        "wallet_address": Web3.to_checksum_address("0x" + str(payload["w"])),
        "token_id": "0x" + str(payload["t"]).lower(),
        "chain_id": int(payload["c"]),
        "moonpay_currency_code": str(payload["m"]).lower(),
        "created_at": created_at,
        "updated_at": created_at,
    }
    return record


def sign_moonpay_url(
    url: str,
    *,
    expected_wallet_address: str,
    user_address: str,
    expected_external_transaction_id: str,
    expected_currency_code: str | None = None,
) -> str:
    """Validate and sign an unsigned MoonPay widget URL."""

    settings = load_settings()
    if not settings.moonpay_api_key or not settings.moonpay_secret_key:
        raise OnRampNotConfiguredError("MoonPay URL signing is not configured")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise OnRampError("MoonPay URL must use https")
    if parsed.hostname not in settings.moonpay_allowed_hosts:
        raise OnRampError("MoonPay host is not allowed")
    if not parsed.query:
        raise OnRampError("MoonPay URL is missing query parameters")

    params = parse_qs(parsed.query, keep_blank_values=True)
    if "signature" in params:
        raise OnRampError("MoonPay URL must be unsigned")

    _require_single(params, "apiKey", settings.moonpay_api_key)
    wallet_address = _single(params, "walletAddress")
    if not wallet_address:
        raise OnRampError("MoonPay URL is missing walletAddress")
    if not Web3.is_address(wallet_address):
        raise OnRampError("MoonPay walletAddress is invalid")
    if Web3.to_checksum_address(wallet_address) != Web3.to_checksum_address(
        expected_wallet_address
    ):
        raise OnRampError("MoonPay walletAddress must be the Privana deposit address")

    external_customer_id = _single(params, "externalCustomerId")
    if not external_customer_id:
        raise OnRampError("MoonPay URL is missing externalCustomerId")
    if external_customer_id != Web3.to_checksum_address(user_address):
        raise OnRampError("MoonPay externalCustomerId must match authenticated user")

    external_transaction_id = _single(params, "externalTransactionId")
    if not external_transaction_id:
        raise OnRampError("MoonPay URL is missing externalTransactionId")
    if external_transaction_id != expected_external_transaction_id:
        raise OnRampError("MoonPay externalTransactionId does not match the Privana intent")

    currency_code = _single(params, "currencyCode")
    if not currency_code:
        raise OnRampError("MoonPay URL is missing currencyCode")
    allowed = {code.lower() for code in settings.moonpay_allowed_currency_codes}
    if allowed and currency_code.lower() not in allowed:
        raise OnRampError("MoonPay currencyCode is not allowed")
    if expected_currency_code and currency_code.lower() != expected_currency_code.lower():
        raise OnRampError("MoonPay currencyCode does not match the Privana intent")

    return base64.b64encode(
        hmac.new(
            settings.moonpay_secret_key.encode("utf-8"),
            f"?{parsed.query}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")


def moonpay_url_external_transaction_id(url: str) -> str | None:
    """Return the unsigned MoonPay URL's external transaction id."""

    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    return _single(params, "externalTransactionId")


async def fetch_moonpay_buy_transactions(
    *,
    external_customer_id: str | None = None,
    limit: int = _MOONPAY_TRANSACTION_PAGE_LIMIT,
    max_pages: int = _MOONPAY_TRANSACTION_MAX_PAGES,
) -> list[dict[str, Any]]:
    """Fetch MoonPay buy transactions, optionally scoped by external customer id."""

    settings = load_settings()
    if not settings.moonpay_secret_key:
        raise OnRampNotConfiguredError("MoonPay transaction lookup is not configured")

    base_url = settings.moonpay_api_base_url.rstrip("/")
    headers = {"Authorization": f"Api-Key {settings.moonpay_secret_key}"}
    try:
        transactions: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=10) as client:
            for page in range(max_pages):
                offset = page * limit
                params: dict[str, Any] = {
                    "limit": limit,
                    "offset": offset,
                }
                if external_customer_id:
                    params["externalCustomerId"] = external_customer_id
                response = await client.get(
                    f"{base_url}/v1/transactions",
                    params=params,
                    headers=headers,
                )
                page_items = _moonpay_response_items(response)
                transactions.extend(page_items)
                if len(page_items) < limit:
                    break
            else:
                logger.warning(
                    "MoonPay transaction lookup hit page cap: external_customer_id=%s "
                    "limit=%d max_pages=%d returned=%d",
                    short_address(external_customer_id),
                    limit,
                    max_pages,
                    len(transactions),
                )
        return transactions
    except httpx.HTTPError as exc:
        raise MoonPayAPIError("MoonPay transaction lookup failed") from exc


async def fetch_moonpay_buy_transactions_by_external_id(
    external_transaction_id: str,
) -> list[dict[str, Any]]:
    """Fetch MoonPay buy transactions for one external transaction id."""

    settings = load_settings()
    if not settings.moonpay_secret_key:
        raise OnRampNotConfiguredError("MoonPay transaction lookup is not configured")

    base_url = settings.moonpay_api_base_url.rstrip("/")
    headers = {"Authorization": f"Api-Key {settings.moonpay_secret_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{base_url}/v1/transactions/ext/{quote(external_transaction_id, safe='')}",
                headers=headers,
            )
            return _moonpay_response_items(response)
    except httpx.HTTPError as exc:
        raise MoonPayAPIError("MoonPay transaction lookup failed") from exc


def dedupe_moonpay_transactions(
    transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Dedupe MoonPay transactions while preserving order."""

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for transaction in transactions:
        data = _transaction_data(transaction)
        key = (
            _string_or_none(data.get("id"))
            or _string_or_none(data.get("externalTransactionId"))
            or _string_or_none(data.get("cryptoTransactionId"))
        )
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(transaction)
    return deduped


def pending_records_from_moonpay_transactions(
    transactions: list[dict[str, Any]],
    *,
    expected_user_address: str,
    expected_wallet_address: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Convert MoonPay transactions into pending Privana on-ramp rows."""

    user = Web3.to_checksum_address(expected_user_address)
    wallet = Web3.to_checksum_address(expected_wallet_address)
    pending: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}

    for transaction in transactions:
        record, reason = moonpay_transaction_to_onramp_record(
            transaction,
            expected_user_address=user,
            expected_wallet_address=wallet,
        )
        reason_counts[reason or "included"] = reason_counts.get(reason or "included", 0) + 1
        if record is not None:
            pending.append(record)

    pending.sort(key=lambda row: int(row.get("updated_at", 0)), reverse=True)
    return pending, {"total_records": len(transactions), "reason_counts": reason_counts}


def moonpay_transaction_to_onramp_record(
    transaction: dict[str, Any],
    *,
    expected_user_address: str,
    expected_wallet_address: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build a pending row from one MoonPay transaction, or return an exclusion reason."""

    data = _transaction_data(transaction)
    external_transaction_id = _string_or_none(
        data.get("externalTransactionId") or data.get("externalId")
    )
    if not external_transaction_id:
        return None, "missing_external_transaction_id"
    try:
        intent = decode_onramp_intent(external_transaction_id, allow_expired=True)
    except OnRampNotConfiguredError:
        raise
    except OnRampError:
        return None, "invalid_external_transaction_id"

    intent_user = Web3.to_checksum_address("0x" + str(intent["u"]))
    if intent_user != Web3.to_checksum_address(expected_user_address):
        return None, "user_address_mismatch"

    intent_wallet = Web3.to_checksum_address("0x" + str(intent["w"]))
    if intent_wallet != Web3.to_checksum_address(expected_wallet_address):
        return None, "wallet_address_mismatch"

    wallet_address = _address_or_none(
        data.get("walletAddress")
        or _nested(data, "wallet", "address")
        or _nested(data, "crypto", "address")
    )
    if wallet_address != intent_wallet:
        return None, "moonpay_wallet_mismatch"

    status = _normalize_status(data.get("status"))
    if status != "completed":
        return None, "not_completed"

    on_chain_tx_hash = _hex_or_none(
        data.get("cryptoTransactionId") or data.get("transactionHash") or data.get("txHash")
    )
    if not on_chain_tx_hash:
        return None, "missing_on_chain_tx_hash"

    currency_code = _currency_code(data.get("currency")) or _currency_code(data.get("crypto"))
    if currency_code and currency_code != str(intent["m"]).lower():
        return None, "currency_mismatch"

    created_at = _timestamp(data.get("createdAt"), fallback=int(intent["iat"]))
    updated_at = _timestamp(data.get("updatedAt"), fallback=created_at)
    record = onramp_record_from_intent(external_transaction_id, intent)
    lock_authorization = get_onramp_lock_authorization(external_transaction_id)
    if lock_authorization is not None:
        record["lock_authorization"] = lock_authorization
    record.update(
        {
            "moonpay_transaction_id": _string_or_none(data.get("id")),
            "status": "completed",
            "wallet_address": intent_wallet,
            "base_currency_code": _currency_code(data.get("baseCurrency"))
            or record.get("base_currency_code"),
            "base_currency_amount": _string_or_none(data.get("baseCurrencyAmount"))
            or record.get("base_currency_amount"),
            "quote_currency_amount": _string_or_none(data.get("quoteCurrencyAmount")),
            "on_chain_tx_hash": on_chain_tx_hash,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )
    return record, None


def verify_moonpay_webhook(raw_body: bytes, signature_header: str) -> None:
    """Verify MoonPay's V2 webhook signature."""

    settings = load_settings()
    if not settings.moonpay_webhook_secret_key:
        raise OnRampNotConfiguredError("MoonPay webhook verification is not configured")

    parts = {}
    for piece in signature_header.split(","):
        key, _, value = piece.partition("=")
        if key and value:
            parts[key] = value
    timestamp = parts.get("t")
    signature = parts.get("s")
    if not timestamp or not signature:
        raise OnRampError("Invalid MoonPay signature header")

    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise OnRampError("Invalid MoonPay signature timestamp") from exc

    tolerance = settings.moonpay_webhook_tolerance_seconds
    if tolerance > 0 and abs(int(time.time()) - signed_at) > tolerance:
        raise OnRampError("MoonPay signature timestamp is outside tolerance")

    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(
        settings.moonpay_webhook_secret_key.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise OnRampError("MoonPay webhook signature mismatch")


def webhook_updates(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract tracked fields from a MoonPay buy webhook for logging."""

    data = _transaction_data(payload)
    moonpay_transaction_id = str(data.get("id") or "")
    external_transaction_id = _string_or_none(
        data.get("externalTransactionId") or payload.get("externalTransactionId")
    )
    transaction_id = external_transaction_id or moonpay_transaction_id
    if not transaction_id:
        raise OnRampError("MoonPay webhook missing transaction id")

    wallet_address = _address_or_none(
        data.get("walletAddress")
        or _nested(data, "wallet", "address")
        or _nested(data, "crypto", "address")
    )
    currency_raw = data.get("currency")
    currency: dict[str, Any] = currency_raw if isinstance(currency_raw, dict) else {}
    updates: dict[str, Any] = {
        "status": _normalize_status(data.get("status")),
        "external_transaction_id": external_transaction_id,
        "moonpay_transaction_id": moonpay_transaction_id or None,
        "user_address": _address_or_none(
            data.get("externalCustomerId") or payload.get("externalCustomerId")
        ),
        "wallet_address": wallet_address,
        "base_currency_code": _currency_code(data.get("baseCurrency")),
        "base_currency_amount": _string_or_none(data.get("baseCurrencyAmount")),
        "quote_currency_amount": _string_or_none(data.get("quoteCurrencyAmount")),
        "on_chain_tx_hash": _hex_or_none(data.get("cryptoTransactionId")),
    }
    if updates["status"] in {"failed", "cancelled"}:
        updates["failure_reason"] = _string_or_none(data.get("failureReason"))
    if currency.get("code") is not None:
        updates["moonpay_currency_code"] = str(currency["code"]).lower()

    return transaction_id, updates


def parse_webhook_body(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise OnRampError("Invalid MoonPay webhook JSON") from exc
    if not isinstance(payload, dict):
        raise OnRampError("Invalid MoonPay webhook payload")
    return payload


def _encode_intent_payload(payload: dict[str, Any]) -> str:
    payload_b64 = _b64url_encode(_pack_intent_payload(payload))
    signature_b64 = _b64url_encode(_intent_signature(payload_b64))
    return f"{_ONRAMP_INTENT_PREFIX}{payload_b64}.{signature_b64}"


def _pack_intent_payload(payload: dict[str, Any]) -> bytes:
    currency = _currency_code_or_error(
        str(payload["m"]),
        "moonpay_currency_code",
    ).encode("ascii")
    if len(currency) > _ONRAMP_INTENT_MAX_CURRENCY_CODE_BYTES:
        raise OnRampError("MoonPay currency code is too large")
    chain_id = int(payload["c"])
    if chain_id < 0 or chain_id > 0xFFFFFFFF:
        raise OnRampError("Invalid chain_id")
    return (
        _ONRAMP_INTENT_STRUCT.pack(
            int(payload["v"]),
            int(payload["iat"]),
            int(payload["exp"]),
            chain_id,
            bytes.fromhex(str(payload["u"])),
            bytes.fromhex(str(payload["w"])),
            bytes.fromhex(str(payload["t"])),
            bytes.fromhex(str(payload["n"])),
            len(currency),
        )
        + currency
    )


def _decode_intent_payload(payload: bytes) -> dict[str, Any]:
    if len(payload) < _ONRAMP_INTENT_STRUCT.size:
        raise ValueError("intent payload too short")
    (
        version,
        issued_at,
        expires_at,
        chain_id,
        user,
        wallet,
        token_id,
        nonce,
        currency_length,
    ) = _ONRAMP_INTENT_STRUCT.unpack_from(payload)
    expected_length = _ONRAMP_INTENT_STRUCT.size + currency_length
    if len(payload) != expected_length:
        raise ValueError("intent payload length mismatch")
    currency = payload[_ONRAMP_INTENT_STRUCT.size : expected_length].decode("ascii")
    return {
        "v": version,
        "u": user.hex(),
        "w": wallet.hex(),
        "t": token_id.hex(),
        "c": chain_id,
        "m": currency,
        "iat": issued_at,
        "exp": expires_at,
        "n": nonce.hex(),
    }


def _intent_signature(payload_b64: str) -> bytes:
    settings = load_settings()
    if not settings.moonpay_intent_signing_key:
        raise OnRampNotConfiguredError("MoonPay on-ramp intents are not configured")
    return hmac.new(
        settings.moonpay_intent_signing_key.encode("utf-8"),
        f"privana:onramp:intent:v{_ONRAMP_INTENT_VERSION}:{payload_b64}".encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _validate_intent_payload(payload: dict[str, Any]) -> None:
    if payload.get("v") != _ONRAMP_INTENT_VERSION:
        raise OnRampError("MoonPay externalTransactionId version is unsupported")
    for field in ("u", "w", "t", "c", "m", "iat", "exp", "n"):
        if payload.get(field) is None:
            raise OnRampError("MoonPay externalTransactionId payload is incomplete")
    Web3.to_checksum_address("0x" + str(payload["u"]))
    Web3.to_checksum_address("0x" + str(payload["w"]))
    _hex_payload(str(payload["t"]), byte_length=32, field_name="token_id")
    int(payload["c"])
    int(payload["iat"])
    int(payload["exp"])
    _hex_payload(str(payload["n"]), byte_length=8, field_name="nonce")
    _currency_code_or_error(str(payload["m"]), "moonpay_currency_code")


def _lock_authorization_or_error(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnRampError("Invalid lock_authorization")

    service_address = value.get("service_address")
    if not isinstance(service_address, str) or not Web3.is_address(service_address):
        raise OnRampError("Invalid lock_authorization.service_address")
    service_address = Web3.to_checksum_address(service_address)
    if service_address.lower() == "0x0000000000000000000000000000000000000000":
        raise OnRampError("Invalid lock_authorization.service_address")

    max_amount = _positive_int(value.get("max_amount"), "lock_authorization.max_amount")
    min_amount = _non_negative_int(value.get("min_amount", 0), "lock_authorization.min_amount")
    if min_amount > max_amount:
        raise OnRampError("lock_authorization min_amount must not exceed max_amount")

    return {
        "service_address": service_address,
        "token_id": "0x"
        + _hex_payload(str(value.get("token_id")), byte_length=32, field_name="token_id"),
        "max_amount": max_amount,
        "min_amount": min_amount,
        "lock_duration": _positive_int(
            value.get("lock_duration"),
            "lock_authorization.lock_duration",
        ),
        "authorization_deadline": _positive_int(
            value.get("authorization_deadline"),
            "lock_authorization.authorization_deadline",
        ),
        "intent_id": "0x"
        + _hex_payload(str(value.get("intent_id")), byte_length=32, field_name="intent_id"),
        "signature": _hex_string_or_error(value.get("signature"), "lock_authorization.signature"),
    }


def _lock_authorization_ttl_seconds(lock_authorization: dict[str, Any], now: int) -> int:
    deadline = int(lock_authorization["authorization_deadline"])
    return max(1, min(deadline - now, _LOCK_AUTHORIZATION_MAX_TTL_SECONDS))


def _positive_int(value: Any, field_name: str) -> int:
    parsed = _int_value(value, field_name)
    if parsed <= 0:
        raise OnRampError(f"{field_name} must be positive")
    return parsed


def _non_negative_int(value: Any, field_name: str) -> int:
    parsed = _int_value(value, field_name)
    if parsed < 0:
        raise OnRampError(f"{field_name} must be non-negative")
    return parsed


def _int_value(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OnRampError(f"Invalid {field_name}") from exc


def _hex_string_or_error(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise OnRampError(f"Invalid {field_name}")
    body = value.strip().lower().removeprefix("0x")
    if not body or len(body) % 2 != 0:
        raise OnRampError(f"Invalid {field_name}")
    try:
        bytes.fromhex(body)
    except ValueError as exc:
        raise OnRampError(f"Invalid {field_name}") from exc
    return "0x" + body


def _moonpay_response_items(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code == 401:
        raise MoonPayAPIError("MoonPay transaction lookup is unauthorized")
    if response.status_code >= 400:
        raise MoonPayAPIError(
            f"MoonPay transaction lookup failed with status {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise MoonPayAPIError("MoonPay transaction lookup returned invalid JSON") from exc
    return _extract_transaction_items(payload)


def _single(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    if not values:
        return None
    if len(values) != 1:
        raise OnRampError(f"MoonPay URL has multiple {key} values")
    return values[0]


def _require_single(params: dict[str, list[str]], key: str, expected: str) -> None:
    value = _single(params, key)
    if value != expected:
        raise OnRampError(f"MoonPay {key} is not allowed")


def _address_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return Web3.to_checksum_address(value) if Web3.is_address(value) else None


def _address_payload(value: str) -> str:
    if not Web3.is_address(value):
        raise OnRampError("Invalid wallet address")
    return Web3.to_checksum_address(value).removeprefix("0x").lower()


def _hex_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    value = value.strip().lower()
    if not value.startswith("0x"):
        value = "0x" + value
    return value


def _hex_payload(value: str, *, byte_length: int, field_name: str) -> str:
    stripped = value.lower().removeprefix("0x")
    if len(stripped) != byte_length * 2:
        raise OnRampError(f"Invalid {field_name}")
    try:
        bytes.fromhex(stripped)
    except ValueError as exc:
        raise OnRampError(f"Invalid {field_name}") from exc
    return stripped


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _currency_code(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("code") is not None:
        return str(value["code"]).lower()
    if isinstance(value, str):
        return value.lower()
    return None


def _currency_code_or_error(value: str | None, field_name: str) -> str:
    if not value or not isinstance(value, str):
        raise OnRampError(f"Invalid {field_name}")
    code = value.strip().lower()
    if not code or any(ch.isspace() for ch in code):
        raise OnRampError(f"Invalid {field_name}")
    return code


def _normalize_status(value: Any) -> str:
    status = str(value or "pending").lower()
    if status in {"completed", "complete"}:
        return "completed"
    if status in {"failed", "cancelled"}:
        return status
    return "pending"


def _extract_transaction_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("transactions", "data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _transaction_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    transaction = payload.get("transaction")
    if isinstance(transaction, dict):
        return transaction
    return payload


def _nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _timestamp(value: Any, *, fallback: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return fallback
    return fallback


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ValueError("invalid base64url") from exc


def short_address(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    return f"{value[:6]}...{value[-4:]}"


def short_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= 18:
        return value
    return f"{value[:10]}...{value[-6:]}"


def onramp_log_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_id": short_identifier(record.get("transaction_id")),
        "external_transaction_id": short_identifier(record.get("external_transaction_id")),
        "moonpay_transaction_id": short_identifier(record.get("moonpay_transaction_id")),
        "status": record.get("status"),
        "wallet_address": short_address(record.get("wallet_address")),
        "has_on_chain_tx_hash": bool(record.get("on_chain_tx_hash")),
        "has_lock_authorization": bool(record.get("lock_authorization")),
        "has_token_id": bool(record.get("token_id")),
        "chain_id": record.get("chain_id"),
        "moonpay_currency_code": record.get("moonpay_currency_code"),
    }
