"""MoonPay on-ramp transaction tracking and verification helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from diskcache import Cache
from web3 import Web3

from src.config import load_settings

_ONRAMP_PREFIX = "onramp:"
_COMPLETED_CONFLICT_PROTECTED_FIELDS = {
    "external_transaction_id",
    "moonpay_transaction_id",
    "user_address",
    "wallet_address",
    "token_id",
    "chain_id",
    "moonpay_currency_code",
    "base_currency_code",
    "base_currency_amount",
    "quote_currency_amount",
    "on_chain_tx_hash",
    "deposit_id",
}

logger = logging.getLogger(__name__)


class OnRampError(ValueError):
    """Raised when an on-ramp request is invalid."""


class OnRampNotConfiguredError(OnRampError):
    """Raised when MoonPay config is missing."""


class OnRampStore:
    """Disk-backed MoonPay transaction store.

    This mirrors the existing auth/rate-limit storage style and keeps the PoC
    out of a database migration until the product shape is settled.
    """

    def __init__(self) -> None:
        settings = load_settings()
        storage_root = Path(settings.auth_token_storage_dir)
        self._cache = Cache(
            str(storage_root / "onramp_transactions"),
            disk_min_file_size=0,
            sqlite_journal_mode="WAL",
        )

    def close(self) -> None:
        self._cache.close()

    def upsert(self, transaction_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if not transaction_id:
            raise OnRampError("Missing transaction id")

        now = int(time.time())
        key = self._key(transaction_id)
        with self._cache.transact():
            record = self._cache.get(key) or {
                "transaction_id": transaction_id,
                "status": "pending",
                "created_at": now,
            }
            completed_record = record.get("status") == "completed"
            ignore_stale_status = (
                record.get("status") == "completed"
                and updates.get("status") is not None
                and updates["status"] != "completed"
            )
            for field, value in updates.items():
                if value is not None:
                    # MoonPay webhooks can be duplicated or delivered out of order.
                    # Once delivery is completed, ignore stale non-completed state
                    # while still allowing later callbacks to fill missing metadata.
                    if ignore_stale_status and field in {"status", "failure_reason"}:
                        continue
                    if (
                        completed_record
                        and field in _COMPLETED_CONFLICT_PROTECTED_FIELDS
                        and record.get(field) is not None
                        and record.get(field) != value
                    ):
                        logger.warning(
                            "Ignoring conflicting completed on-ramp update: tx=%s field=%s",
                            short_identifier(transaction_id),
                            field,
                        )
                        continue
                    record[field] = value
            record["updated_at"] = now
            if record.get("deposit_tx_hash") and not record.get("deposit_triggered_at"):
                record["deposit_triggered_at"] = now
            self._cache.set(key, record)
            return dict(record)

    def get(self, transaction_id: str) -> Optional[dict[str, Any]]:
        record = self._cache.get(self._key(transaction_id))
        return dict(record) if isinstance(record, dict) else None

    def delete(self, transaction_id: str) -> None:
        self._cache.delete(self._key(transaction_id))

    def find_by_moonpay_transaction_id(self, moonpay_transaction_id: str) -> list[dict[str, Any]]:
        if not moonpay_transaction_id:
            return []

        matches: list[dict[str, Any]] = []
        for key in list(self._cache):
            if not isinstance(key, str) or not key.startswith(_ONRAMP_PREFIX):
                continue
            record = self._cache.get(key)
            if not isinstance(record, dict):
                continue
            if record.get("moonpay_transaction_id") == moonpay_transaction_id:
                matches.append(dict(record))
        return matches

    def mark_deposit_started_for_source_tx(
        self,
        *,
        user_address: str,
        chain_id: int,
        on_chain_tx_hash: str,
        deposit_id: str,
    ) -> list[dict[str, Any]]:
        """Attach the backend deposit id to completed on-ramp rows for a source tx."""

        user = Web3.to_checksum_address(user_address)
        source_tx_hash = _hex_or_none(on_chain_tx_hash)
        deposit_id = _hex_or_none(deposit_id)
        if source_tx_hash is None or deposit_id is None:
            return []

        now = int(time.time())
        updated: list[dict[str, Any]] = []
        with self._cache.transact():
            for key in list(self._cache):
                if not isinstance(key, str) or not key.startswith(_ONRAMP_PREFIX):
                    continue
                record = self._cache.get(key)
                if not isinstance(record, dict):
                    continue
                if record.get("user_address") != user:
                    continue
                if record.get("status") != "completed":
                    continue
                if record.get("chain_id") != chain_id:
                    continue
                if record.get("on_chain_tx_hash") != source_tx_hash:
                    continue

                existing_deposit_id = record.get("deposit_id")
                if existing_deposit_id and existing_deposit_id != deposit_id:
                    logger.warning(
                        "Skipping on-ramp deposit_id overwrite: tx=%s existing=%s incoming=%s",
                        short_identifier(record.get("transaction_id")),
                        short_identifier(existing_deposit_id),
                        short_identifier(deposit_id),
                    )
                    continue

                changed = existing_deposit_id != deposit_id
                if changed:
                    record["deposit_id"] = deposit_id
                if not record.get("deposit_triggered_at"):
                    record["deposit_triggered_at"] = now
                    changed = True
                if not changed:
                    continue
                record["updated_at"] = now
                self._cache.set(key, record)
                updated.append(dict(record))
        return updated

    def mark_deposit_credited(
        self,
        *,
        user_address: str,
        deposit_id: str,
    ) -> list[dict[str, Any]]:
        """Mark on-ramp rows credited after backend deposit status proves credit."""

        user = Web3.to_checksum_address(user_address)
        deposit_id = _hex_or_none(deposit_id)
        if deposit_id is None:
            return []

        now = int(time.time())
        updated: list[dict[str, Any]] = []
        with self._cache.transact():
            for key in list(self._cache):
                if not isinstance(key, str) or not key.startswith(_ONRAMP_PREFIX):
                    continue
                record = self._cache.get(key)
                if not isinstance(record, dict):
                    continue
                if record.get("user_address") != user:
                    continue
                if record.get("status") != "completed":
                    continue
                if record.get("deposit_id") != deposit_id:
                    continue

                if record.get("credited_at"):
                    continue
                record["credited_at"] = now
                record["updated_at"] = now
                self._cache.set(key, record)
                updated.append(dict(record))
        return updated

    def find_open_intents_for_wallet(
        self,
        *,
        wallet_address: str,
        transaction_id_prefix: str,
        moonpay_currency_code: str | None = None,
        max_age_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        """Find unbound Privana intents that can safely receive an orphan webhook."""

        wallet = Web3.to_checksum_address(wallet_address)
        currency = moonpay_currency_code.lower() if moonpay_currency_code else None
        now = int(time.time())
        matches: list[dict[str, Any]] = []

        for key in list(self._cache):
            if not isinstance(key, str) or not key.startswith(_ONRAMP_PREFIX):
                continue
            record = self._cache.get(key)
            if not isinstance(record, dict):
                continue

            transaction_id = record.get("transaction_id")
            if not isinstance(transaction_id, str) or not transaction_id.startswith(
                transaction_id_prefix
            ):
                continue
            if record.get("status") != "pending":
                continue
            if record.get("wallet_address") != wallet:
                continue
            if record.get("moonpay_transaction_id") or record.get("on_chain_tx_hash"):
                continue
            if record.get("deposit_tx_hash"):
                continue
            if currency and str(record.get("moonpay_currency_code", "")).lower() != currency:
                continue
            if max_age_seconds is not None:
                created_at = int(record.get("created_at", 0))
                if created_at <= 0 or now - created_at > max_age_seconds:
                    continue

            matches.append(dict(record))

        return matches

    def pending_for_user(self, user_address: str, deposit_address: str) -> list[dict[str, Any]]:
        user = Web3.to_checksum_address(user_address)
        deposit = Web3.to_checksum_address(deposit_address)
        pending: list[dict[str, Any]] = []
        for key in list(self._cache):
            if not isinstance(key, str) or not key.startswith(_ONRAMP_PREFIX):
                continue
            record = self._cache.get(key)
            if not isinstance(record, dict):
                continue
            if record.get("user_address") != user:
                continue
            if record.get("wallet_address") != deposit:
                continue
            if record.get("status") != "completed":
                continue
            if not record.get("on_chain_tx_hash") or record.get("deposit_tx_hash"):
                continue
            if record.get("credited_at"):
                continue
            if not record.get("token_id") or not record.get("chain_id"):
                continue
            pending.append(dict(record))
        return sorted(pending, key=lambda r: int(r.get("updated_at", 0)), reverse=True)

    def pending_filter_diagnostics(
        self, user_address: str, deposit_address: str, *, sample_limit: int = 5
    ) -> dict[str, Any]:
        """Return redacted reasons why stored on-ramp rows are not pending."""

        user = Web3.to_checksum_address(user_address)
        deposit = Web3.to_checksum_address(deposit_address)
        reason_counts: Counter[str] = Counter()
        samples: list[dict[str, Any]] = []
        total = 0

        for key in list(self._cache):
            if not isinstance(key, str) or not key.startswith(_ONRAMP_PREFIX):
                continue
            record = self._cache.get(key)
            if not isinstance(record, dict):
                continue

            total += 1
            reason = _pending_exclusion_reason(record, user, deposit)
            reason_counts[reason or "included"] += 1
            if reason and len(samples) < sample_limit:
                samples.append(_redacted_record_sample(record, reason))

        return {
            "total_records": total,
            "reason_counts": dict(reason_counts),
            "samples": samples,
        }

    @staticmethod
    def _key(transaction_id: str) -> str:
        return f"{_ONRAMP_PREFIX}{transaction_id}"


def sign_moonpay_url(
    url: str,
    *,
    expected_wallet_address: str,
    user_address: str,
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
    if wallet_address is None:
        raise OnRampError("MoonPay URL is missing walletAddress")
    if not Web3.is_address(wallet_address):
        raise OnRampError("MoonPay walletAddress is invalid")
    if Web3.to_checksum_address(wallet_address) != Web3.to_checksum_address(
        expected_wallet_address
    ):
        raise OnRampError("MoonPay walletAddress must be the Privana deposit address")

    external_customer_id = _single(params, "externalCustomerId")
    if external_customer_id is None:
        raise OnRampError("MoonPay URL is missing externalCustomerId")
    if external_customer_id.lower() != user_address.lower():
        raise OnRampError("MoonPay externalCustomerId must match authenticated user")

    external_transaction_id = _single(params, "externalTransactionId")
    if not external_transaction_id:
        raise OnRampError("MoonPay URL is missing externalTransactionId")

    currency_code = _single(params, "currencyCode")
    if currency_code is None:
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
    """Extract the transaction id and tracked fields from a MoonPay buy webhook."""

    data = payload.get("data")
    if not isinstance(data, dict):
        transaction = payload.get("transaction")
        data = transaction if isinstance(transaction, dict) else {}

    moonpay_transaction_id = str(data.get("id") or "")
    external_transaction_id = _string_or_none(
        data.get("externalTransactionId") or payload.get("externalTransactionId")
    )
    transaction_id = external_transaction_id or moonpay_transaction_id
    if not transaction_id:
        raise OnRampError("MoonPay webhook missing transaction id")

    user_address = _address_or_none(
        data.get("externalCustomerId") or payload.get("externalCustomerId")
    )
    wallet_address = _address_or_none(data.get("walletAddress"))
    on_chain_tx_hash = data.get("cryptoTransactionId")
    currency_raw = data.get("currency")
    currency: dict[str, Any] = currency_raw if isinstance(currency_raw, dict) else {}
    status = _normalise_status(data.get("status"))
    updates: dict[str, Any] = {
        "status": status,
        "external_transaction_id": external_transaction_id,
        "moonpay_transaction_id": moonpay_transaction_id or None,
        "user_address": user_address,
        "wallet_address": wallet_address,
        "base_currency_code": _currency_code(data.get("baseCurrency")),
        "base_currency_amount": _string_or_none(data.get("baseCurrencyAmount")),
        "quote_currency_amount": _string_or_none(data.get("quoteCurrencyAmount")),
        "on_chain_tx_hash": _hex_or_none(on_chain_tx_hash),
    }
    if status in {"failed", "cancelled"}:
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


def _hex_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    value = value.strip().lower()
    if not value.startswith("0x"):
        value = "0x" + value
    return value


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _currency_code(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("code") is not None:
        return str(value["code"]).lower()
    if isinstance(value, str):
        return value.lower()
    return None


def _normalise_status(value: Any) -> str:
    status = str(value or "pending").lower()
    if status in {"completed", "complete"}:
        return "completed"
    if status in {"failed", "cancelled"}:
        return status
    return "pending"


def _pending_exclusion_reason(
    record: dict[str, Any], user_address: str, deposit_address: str
) -> str | None:
    if record.get("user_address") != user_address:
        return "user_address_mismatch"
    if record.get("wallet_address") != deposit_address:
        return "wallet_address_mismatch"
    if record.get("status") != "completed":
        return "not_completed"
    if not record.get("on_chain_tx_hash"):
        return "missing_on_chain_tx_hash"
    if record.get("deposit_tx_hash"):
        return "already_deposit_triggered"
    if record.get("credited_at"):
        return "already_credited"
    if not record.get("token_id"):
        return "missing_token_id"
    if not record.get("chain_id"):
        return "missing_chain_id"
    return None


def _redacted_record_sample(record: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "transaction_id": short_identifier(record.get("transaction_id")),
        "external_transaction_id": short_identifier(record.get("external_transaction_id")),
        "moonpay_transaction_id": short_identifier(record.get("moonpay_transaction_id")),
        "status": record.get("status"),
        "user_address": short_address(record.get("user_address")),
        "wallet_address": short_address(record.get("wallet_address")),
        "has_on_chain_tx_hash": bool(record.get("on_chain_tx_hash")),
        "has_deposit_tx_hash": bool(record.get("deposit_tx_hash")),
        "has_deposit_id": bool(record.get("deposit_id")),
        "has_credited_at": bool(record.get("credited_at")),
        "has_token_id": bool(record.get("token_id")),
        "chain_id": record.get("chain_id"),
        "moonpay_currency_code": record.get("moonpay_currency_code"),
    }


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
        "user_address": short_address(record.get("user_address")),
        "wallet_address": short_address(record.get("wallet_address")),
        "has_on_chain_tx_hash": bool(record.get("on_chain_tx_hash")),
        "has_deposit_tx_hash": bool(record.get("deposit_tx_hash")),
        "has_deposit_id": bool(record.get("deposit_id")),
        "has_credited_at": bool(record.get("credited_at")),
        "has_token_id": bool(record.get("token_id")),
        "chain_id": record.get("chain_id"),
        "moonpay_currency_code": record.get("moonpay_currency_code"),
    }


_onramp_store_instance: OnRampStore | None = None


def get_onramp_store() -> OnRampStore:
    global _onramp_store_instance
    if _onramp_store_instance is None:
        _onramp_store_instance = OnRampStore()
    return _onramp_store_instance
