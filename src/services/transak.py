"""Bounded, stateless Transak partner API integration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from web3 import Web3

from src.config import load_settings
from src.services.onramp_intent import (
    PROVIDER_TRANSAK,
    OnRampError,
    OnRampNotConfiguredError,
    create_intent,
    decode_intent,
    record_from_intent,
)

logger = logging.getLogger(__name__)

TRANSAK_ORDER_PAGE_LIMIT = 100
TRANSAK_ORDER_MAX_PAGES = 10
TRANSAK_EXACT_ORDER_WINDOW_DAYS = 31
TRANSAK_WIDGET_SESSION_TTL_SECONDS = 5 * 60
TRANSAK_WEBHOOK_PREVIOUS_TOKEN_OVERLAP_SECONDS = 15 * 60
TRANSAK_MAX_RESPONSE_BYTES = 1024 * 1024
TRANSAK_MAX_WIDGET_URL_BYTES = 8192
TRANSAK_MAX_WEBHOOK_JWT_BYTES = 256 * 1024

TRANSAK_CLIENT_IP_MODE_HEADER = "header"
TRANSAK_CLIENT_IP_MODE_ATTESTED = "attested"
TRANSAK_IP_ATTESTATION_VERSION = 1
TRANSAK_IP_ATTESTATION_MAX_WINDOW_SECONDS = 90
TRANSAK_IP_ATTESTATION_MAX_CLOCK_SKEW_SECONDS = 30
TRANSAK_IP_ATTESTATION_MIN_SECRET_LENGTH = 32
# Best-effort in-memory replay bound for the one-machine/one-worker deployment.
TRANSAK_IP_ATTESTATION_MAX_NONCES = 10_000

# Cloudflare rewrites CF-Connecting-IP to this range for cross-zone Worker
# subrequests; a claim carrying it was not minted for a direct browser request.
_CF_WORKER_SOURCE_NETWORK = ipaddress.ip_network("2a06:98c0::/29")
_IP_ATTESTATION_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_IP_ATTESTATION_SIG_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_PROVIDER_TEXT_BYTES = 16 * 1024
_MAX_DISPLAY_VALUE_BYTES = 256
_MAX_TIMESTAMP = 253_402_300_799  # 9999-12-31T23:59:59Z
_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_PROVIDER_CODE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,31}\Z")
_REFERRER_DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_TX_HASH_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}\Z")
_EXACT_ORDER_NOT_FOUND_MESSAGE = "Invalid partnerOrderId or order not found"


class TransakAPIError(OnRampError):
    """Raised when a Transak partner API request cannot be completed safely."""


class TransakRateLimitError(TransakAPIError):
    """Raised when Transak explicitly rate-limits a partner API request."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransakWebhookVerificationError(OnRampError):
    """Raised when a Transak webhook JWT cannot be authenticated."""


@dataclass(frozen=True, repr=False)
class TransakConfig:
    api_key: str
    api_secret: str
    api_base_url: str
    gateway_base_url: str
    referrer_domain: str
    crypto_currency_code: str
    network: str
    chain_id: int
    token_address: str

    @property
    def canonical_asset_code(self) -> str:
        return self.crypto_currency_code.lower()

    @property
    def canonical_network(self) -> str:
        return self.network.lower()


@dataclass(frozen=True, repr=False)
class TransakSessionIpConfig:
    """Trusted client-IP source required only when creating a widget session."""

    mode: str
    header: str | None
    attestation_secret: str | None


@dataclass(frozen=True, repr=False)
class _AccessToken:
    value: str
    expires_at: int


def load_transak_config() -> TransakConfig:
    """Return validated base provider settings without making startup depend on them."""

    settings = load_settings()
    api_key = _required_setting(settings.transak_api_key)
    api_secret = _required_setting(settings.transak_api_secret)
    api_base_url = _https_base_url(settings.transak_api_base_url)
    gateway_base_url = _https_base_url(settings.transak_gateway_base_url)
    referrer_domain = _referrer_domain(settings.transak_referrer_domain)
    crypto_currency_code = _provider_code(settings.transak_crypto_currency_code)
    network = _provider_code(settings.transak_network)
    chain_id = settings.transak_chain_id
    if chain_id is None or chain_id <= 0 or chain_id > 0xFFFFFFFF:
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    token_address = settings.transak_token_address
    if (
        not isinstance(token_address, str)
        or not Web3.is_address(token_address)
        or int(token_address, 16) == 0
    ):
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")

    return TransakConfig(
        api_key=api_key,
        api_secret=api_secret,
        api_base_url=api_base_url,
        gateway_base_url=gateway_base_url,
        referrer_domain=referrer_domain,
        crypto_currency_code=crypto_currency_code,
        network=network,
        chain_id=chain_id,
        token_address=Web3.to_checksum_address(token_address),
    )


def load_transak_session_ip_config() -> TransakSessionIpConfig:
    """Return the trusted client-IP source required only for session creation."""

    settings = load_settings()
    mode, header, attestation_secret = _client_ip_source(
        mode=settings.transak_client_ip_mode,
        header=settings.transak_client_ip_header,
        secret=settings.transak_ip_attestation_secret,
    )
    return TransakSessionIpConfig(
        mode=mode,
        header=header,
        attestation_secret=attestation_secret,
    )


def create_transak_intent(
    *,
    user_address: str,
    wallet_address: str,
    token_id: str,
    chain_id: int,
    config: TransakConfig | None = None,
) -> dict[str, Any]:
    """Create a signed intent for the deployment's single configured Transak asset."""

    config = config or load_transak_config()
    transaction_id, payload = create_intent(
        provider=PROVIDER_TRANSAK,
        user_address=user_address,
        wallet_address=wallet_address,
        token_id=token_id,
        chain_id=chain_id,
        asset_code=config.canonical_asset_code,
    )
    return record_from_intent(transaction_id, payload)


def client_ip_from_values(values: Iterable[str], *, header_name: str) -> str:
    """Validate one proxy-owned original-client-IP header value."""

    candidates = list(values)
    if len(candidates) != 1:
        raise OnRampError(f"Missing or ambiguous trusted client IP header {header_name}")
    value = candidates[0].strip()
    if not value or "," in value:
        raise OnRampError(f"Invalid trusted client IP header {header_name}")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError as exc:
        raise OnRampError(f"Invalid trusted client IP header {header_name}") from exc


# nonce -> attestation expiry; pruned on insert. Replay protection is a
# best-effort bound for the one-machine/one-worker deployment; the short
# expiry, intent binding, and per-user rate limit are the primary controls.
_ip_attestation_nonces: dict[str, int] = {}


def _reserve_ip_attestation_nonce(nonce: str, expires_at: int, now: float) -> None:
    expired = [key for key, value in _ip_attestation_nonces.items() if value <= now]
    for key in expired:
        del _ip_attestation_nonces[key]
    if nonce in _ip_attestation_nonces:
        raise OnRampError("Client IP attestation was already used")
    if len(_ip_attestation_nonces) >= TRANSAK_IP_ATTESTATION_MAX_NONCES:
        raise OnRampError("Client IP attestation could not be recorded")
    _ip_attestation_nonces[nonce] = expires_at


def verify_ip_attestation(
    *,
    version: int,
    ip: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
    signature: str,
    transaction_id: str,
    config: TransakConfig,
    session_ip_config: TransakSessionIpConfig,
    now: float | None = None,
) -> str:
    """Verify one edge-signed client-IP claim and return the attested IP.

    The claim binds the referrer domain, the SHA-256 of the signed intent, the
    edge-observed IP, and a short validity window. Every failure is fail-closed.
    """

    secret = session_ip_config.attestation_secret
    if session_ip_config.mode != TRANSAK_CLIENT_IP_MODE_ATTESTED or not secret:
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    if version != TRANSAK_IP_ATTESTATION_VERSION:
        raise OnRampError("Unsupported client IP attestation version")
    if not _IP_ATTESTATION_NONCE_PATTERN.fullmatch(nonce):
        raise OnRampError("Invalid client IP attestation")
    if not _IP_ATTESTATION_SIG_PATTERN.fullmatch(signature):
        raise OnRampError("Invalid client IP attestation")
    if not isinstance(ip, str) or not ip or len(ip.encode()) > 64 or "%" in ip or "|" in ip:
        raise OnRampError("Invalid client IP attestation")

    current = time.time() if now is None else now
    if issued_at <= 0 or expires_at <= 0 or expires_at > _MAX_TIMESTAMP:
        raise OnRampError("Invalid client IP attestation")
    if issued_at > current + TRANSAK_IP_ATTESTATION_MAX_CLOCK_SKEW_SECONDS:
        raise OnRampError("Client IP attestation is not yet valid")
    if expires_at <= current:
        raise OnRampError("Client IP attestation has expired")
    if expires_at <= issued_at:
        raise OnRampError("Invalid client IP attestation")
    if expires_at - issued_at > TRANSAK_IP_ATTESTATION_MAX_WINDOW_SECONDS:
        raise OnRampError("Client IP attestation window is too long")

    intent_hash = hashlib.sha256(transaction_id.encode()).hexdigest()
    payload = "|".join(
        (
            f"v{TRANSAK_IP_ATTESTATION_VERSION}",
            config.referrer_domain,
            intent_hash,
            ip,
            str(issued_at),
            str(expires_at),
            nonce,
        )
    )
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise OnRampError("Invalid client IP attestation")

    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise OnRampError("Invalid client IP attestation") from exc
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        raise OnRampError("Client IP attestation must carry a public IP")
    canonical_ip = parsed.compressed.lower()
    if ip != canonical_ip:
        raise OnRampError("Client IP attestation must carry a canonical IP")
    if not parsed.is_global or parsed.is_multicast or parsed.is_reserved:
        raise OnRampError("Client IP attestation must carry a public IP")
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.is_site_local:
            raise OnRampError("Client IP attestation must carry a public IP")
        if parsed in _CF_WORKER_SOURCE_NETWORK:
            raise OnRampError("Client IP attestation must come from a direct request")

    _reserve_ip_attestation_nonce(nonce, expires_at, current)
    return canonical_ip


class TransakService:
    """Small partner API client with an in-memory single-flight token cache."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._transport = transport
        self._now = now or time.time
        self._token_lock = asyncio.Lock()
        self._current_token: _AccessToken | None = None
        self._previous_token: _AccessToken | None = None
        self._previous_token_valid_until = 0.0

    async def create_widget_session(
        self,
        *,
        transaction_id: str,
        wallet_address: str,
        user_ip: str,
        config: TransakConfig | None = None,
    ) -> dict[str, Any]:
        """Create one backend-only, locked-address widget session."""

        config = config or load_transak_config()
        requested_at = int(self._now())
        widget_params = {
            "apiKey": config.api_key,
            "referrerDomain": config.referrer_domain,
            "productsAvailed": "BUY",
            "cryptoCurrencyCode": config.crypto_currency_code,
            "network": config.network,
            "walletAddress": Web3.to_checksum_address(wallet_address),
            "disableWalletAddressForm": True,
            "partnerOrderId": transaction_id,
        }
        payload = await self._authenticated_json_request(
            "POST",
            f"{config.gateway_base_url}/api/v2/auth/session",
            operation="widget session creation",
            config=config,
            headers={"x-user-ip": user_ip},
            json_body={"widgetParams": widget_params},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        widget_url = data.get("widgetUrl") if isinstance(data, dict) else None
        if not isinstance(widget_url, str) or not widget_url:
            raise TransakAPIError("Transak widget session response is malformed")
        if not _is_https_url(widget_url, max_bytes=TRANSAK_MAX_WIDGET_URL_BYTES):
            raise TransakAPIError("Transak widget session response is malformed")
        return {
            "provider": PROVIDER_TRANSAK,
            "url": widget_url,
            "expires_at": requested_at + TRANSAK_WIDGET_SESSION_TTL_SECONDS,
        }

    async def get_orders_by_partner_order_id(
        self,
        transaction_id: str,
        *,
        issued_at: int,
        config: TransakConfig | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a bounded exact signed-intent order result."""

        return await self._get_orders(
            {"filter[partnerOrderId]": transaction_id},
            max_pages=1,
            date_bounds=_exact_order_date_bounds(issued_at, self._now()),
            exact_not_found_is_empty=True,
            config=config or load_transak_config(),
        )

    async def get_orders_by_wallet(
        self,
        wallet_address: str,
        *,
        config: TransakConfig | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch Transak's default, capped wallet history."""

        return await self._get_orders(
            {"filter[walletAddress]": Web3.to_checksum_address(wallet_address)},
            max_pages=TRANSAK_ORDER_MAX_PAGES,
            date_bounds=None,
            exact_not_found_is_empty=False,
            config=config or load_transak_config(),
        )

    async def verify_webhook(
        self,
        raw_body: bytes,
        *,
        config: TransakConfig | None = None,
    ) -> dict[str, Any]:
        """Verify and decode Transak's body-level webhook JWT."""

        try:
            payload = _strict_json_loads(raw_body)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise OnRampError("Invalid Transak webhook JSON") from exc
        if not isinstance(payload, dict):
            raise OnRampError("Invalid Transak webhook payload")
        encoded = payload.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise OnRampError("Invalid Transak webhook payload")
        try:
            encoded_bytes = encoded.encode("ascii")
        except UnicodeEncodeError as exc:
            raise OnRampError("Invalid Transak webhook payload") from exc
        if len(encoded_bytes) > TRANSAK_MAX_WEBHOOK_JWT_BYTES:
            raise OnRampError("Invalid Transak webhook payload")

        # Validate deployment configuration, but never mint/rotate a token in
        # response to an unauthenticated webhook.
        config or load_transak_config()
        tokens = await self._cached_webhook_verification_tokens()
        for token in tokens:
            try:
                decoded = jwt.decode(
                    encoded,
                    token,
                    algorithms=["HS256"],
                    options={"verify_aud": False},
                )
            except jwt.InvalidTokenError:
                continue
            if not isinstance(decoded, dict):
                raise OnRampError("Invalid Transak webhook payload")
            webhook_data = decoded.get("webhookData")
            event_id = decoded.get("eventID")
            if not isinstance(webhook_data, dict) or not isinstance(event_id, str) or not event_id:
                raise OnRampError("Invalid Transak webhook payload")
            return decoded
        raise TransakWebhookVerificationError("Transak webhook signature mismatch")

    async def _get_orders(
        self,
        filters: Mapping[str, str],
        *,
        max_pages: int,
        date_bounds: tuple[str, str] | None,
        exact_not_found_is_empty: bool,
        config: TransakConfig,
    ) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        for page in range(max_pages):
            skip = page * TRANSAK_ORDER_PAGE_LIMIT
            params = {
                "limit": TRANSAK_ORDER_PAGE_LIMIT,
                "skip": skip,
                "filter[productsAvailed]": '["BUY"]',
                "filter[status]": "COMPLETED",
                "filter[sortOrder]": "desc",
                **filters,
            }
            if date_bounds is not None:
                params["startDate"], params["endDate"] = date_bounds
            payload = await self._authenticated_json_request(
                "GET",
                f"{config.api_base_url}/partners/api/v2/orders",
                operation="order lookup",
                config=config,
                params=params,
                exact_not_found_is_empty=exact_not_found_is_empty,
            )
            page_orders, total_count = _orders_page(payload)
            orders.extend(page_orders)
            if len(page_orders) < TRANSAK_ORDER_PAGE_LIMIT:
                break
            if total_count is not None and skip + len(page_orders) >= total_count:
                break
        else:
            logger.warning(
                "Transak order lookup hit page cap: lookup=%s pages=%d returned=%d",
                "wallet" if "filter[walletAddress]" in filters else "exact",
                max_pages,
                len(orders),
            )
        return orders

    async def _authenticated_json_request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        config: TransakConfig,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        exact_not_found_is_empty: bool = False,
    ) -> Any:
        token = await self._access_token(config)
        response = await self._json_request(
            method,
            url,
            headers={
                "access-token": token.value,
                "x-api-key": config.api_key,
                **dict(headers or {}),
            },
            params=params,
            json_body=json_body,
        )
        if response[0] == 401:
            token = await self._refresh_after_unauthorized(token, config)
            response = await self._json_request(
                method,
                url,
                headers={
                    "access-token": token.value,
                    "x-api-key": config.api_key,
                    **dict(headers or {}),
                },
                params=params,
                json_body=json_body,
            )
        if exact_not_found_is_empty and _is_exact_order_not_found(response):
            return {"data": []}
        return _successful_payload(response, operation=operation)

    async def _access_token(self, config: TransakConfig) -> _AccessToken:
        current = self._current_token
        if current is not None and self._now() < current.expires_at:
            return current
        async with self._token_lock:
            current = self._current_token
            if current is not None and self._now() < current.expires_at:
                return current
            return await self._refresh_token_locked(config)

    async def _refresh_after_unauthorized(
        self,
        used_token: _AccessToken,
        config: TransakConfig,
    ) -> _AccessToken:
        """CAS refresh: reuse a concurrent refresh or replace the rejected token once."""

        async with self._token_lock:
            current = self._current_token
            if (
                current is not None
                and current.value != used_token.value
                and self._now() < current.expires_at
            ):
                return current
            return await self._refresh_token_locked(config)

    async def _refresh_token_locked(self, config: TransakConfig) -> _AccessToken:
        response = await self._json_request(
            "POST",
            f"{config.api_base_url}/partners/api/v2/refresh-token",
            headers={
                "api-secret": config.api_secret,
                "x-api-key": config.api_key,
            },
            json_body={"apiKey": config.api_key},
        )
        payload = _successful_payload(response, operation="access-token refresh")
        data = payload.get("data") if isinstance(payload, dict) else None
        access_token = data.get("accessToken") if isinstance(data, dict) else None
        expires_at = data.get("expiresAt") if isinstance(data, dict) else None
        if (
            not isinstance(access_token, str)
            or not access_token
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or not _is_visible_ascii(access_token, max_bytes=_MAX_PROVIDER_TEXT_BYTES)
        ):
            raise TransakAPIError("Transak access-token response is malformed")
        if expires_at <= int(self._now()):
            raise TransakAPIError("Transak access-token response is malformed")

        replacement = _AccessToken(access_token, expires_at)
        previous = self._current_token
        self._current_token = replacement
        if previous is not None and previous.value != replacement.value:
            self._previous_token = previous
            self._previous_token_valid_until = min(
                previous.expires_at + TRANSAK_WEBHOOK_PREVIOUS_TOKEN_OVERLAP_SECONDS,
                self._now() + TRANSAK_WEBHOOK_PREVIOUS_TOKEN_OVERLAP_SECONDS,
            )
        return replacement

    async def _cached_webhook_verification_tokens(self) -> tuple[str, ...]:
        async with self._token_lock:
            now = self._now()
            tokens: list[str] = []
            current = self._current_token
            if (
                current is not None
                and now <= current.expires_at + TRANSAK_WEBHOOK_PREVIOUS_TOKEN_OVERLAP_SECONDS
            ):
                tokens.append(current.value)
            if (
                self._previous_token is not None
                and now <= self._previous_token_valid_until
                and self._previous_token.value not in tokens
            ):
                tokens.append(self._previous_token.value)
            elif now > self._previous_token_valid_until:
                self._previous_token = None
                self._previous_token_valid_until = 0.0
            if not tokens:
                raise OnRampNotConfiguredError("Transak webhook verification token is unavailable")
            return tuple(tokens)

    async def _json_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any, Mapping[str, str]]:
        request_headers = {"accept": "application/json", **dict(headers)}
        try:
            async with httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json_body,
                ) as response:
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            parsed_content_length = int(content_length)
                        except ValueError as exc:
                            raise TransakAPIError("Transak response is malformed") from exc
                        if parsed_content_length < 0:
                            raise TransakAPIError("Transak response is malformed")
                        if parsed_content_length > TRANSAK_MAX_RESPONSE_BYTES:
                            raise TransakAPIError("Transak response is too large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > TRANSAK_MAX_RESPONSE_BYTES:
                            raise TransakAPIError("Transak response is too large")
                        body.extend(chunk)
                    status_code = response.status_code
                    response_headers = dict(response.headers)
        except TransakAPIError:
            raise
        except httpx.HTTPError as exc:
            raise TransakAPIError("Transak partner API is unavailable") from exc

        payload: Any = None
        if body:
            try:
                payload = _strict_json_loads(body)
            except (UnicodeError, ValueError, RecursionError) as exc:
                if status_code < 400:
                    raise TransakAPIError("Transak response is malformed") from exc
        return status_code, payload, response_headers


def pending_records_from_transak_orders(
    orders: list[dict[str, Any]],
    *,
    expected_user_address: str,
    expected_wallet_address: str,
    expected_token_id: str,
    config: TransakConfig,
    expected_transaction_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Admit only completed, signed, caller-owned orders for the configured asset."""

    pending: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for order in orders:
        record, reason = transak_order_to_onramp_record(
            order,
            expected_user_address=expected_user_address,
            expected_wallet_address=expected_wallet_address,
            expected_token_id=expected_token_id,
            config=config,
            expected_transaction_id=expected_transaction_id,
        )
        reason_counts[reason or "included"] = reason_counts.get(reason or "included", 0) + 1
        if record is not None:
            pending.append(record)
    pending.sort(
        key=lambda row: (
            int(row.get("updated_at", 0)),
            int(row.get("created_at", 0)),
            str(row.get("provider_transaction_id") or ""),
        ),
        reverse=True,
    )
    return pending, {"total_records": len(orders), "reason_counts": reason_counts}


def transak_order_to_onramp_record(
    order: dict[str, Any],
    *,
    expected_user_address: str,
    expected_wallet_address: str,
    expected_token_id: str,
    config: TransakConfig,
    expected_transaction_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize one strict Transak Get Orders item or return an exclusion reason."""

    provider_transaction_id = _nonempty_string(order.get("_id"))
    if provider_transaction_id is None:
        return None, "missing_provider_transaction_id"

    transaction_id = _nonempty_string(order.get("partnerOrderId"))
    if transaction_id is None:
        return None, "missing_partner_order_id"
    if expected_transaction_id is not None and transaction_id != expected_transaction_id:
        return None, "partner_order_id_mismatch"
    try:
        intent = decode_intent(transaction_id, allow_expired=True)
    except OnRampNotConfiguredError:
        raise
    except OnRampError:
        return None, "invalid_partner_order_id"
    if intent["p"] != PROVIDER_TRANSAK:
        return None, "provider_mismatch"

    expected_user = Web3.to_checksum_address(expected_user_address)
    expected_wallet = Web3.to_checksum_address(expected_wallet_address)
    if Web3.to_checksum_address("0x" + str(intent["u"])) != expected_user:
        return None, "user_address_mismatch"
    intent_wallet = Web3.to_checksum_address("0x" + str(intent["w"]))
    if intent_wallet != expected_wallet:
        return None, "wallet_address_mismatch"
    if int(intent["c"]) != config.chain_id:
        return None, "chain_mismatch"
    if "0x" + str(intent["t"]).lower() != expected_token_id.lower():
        return None, "token_mismatch"
    if str(intent["a"]).lower() != config.canonical_asset_code:
        return None, "intent_asset_mismatch"

    wallet = _address_or_none(order.get("walletAddress"))
    if wallet != intent_wallet:
        return None, "transak_wallet_mismatch"
    if str(order.get("status") or "").upper() != "COMPLETED":
        return None, "not_completed"
    if str(order.get("isBuyOrSell") or "").upper() != "BUY":
        return None, "not_buy"
    if str(order.get("cryptoCurrency") or "").lower() != config.canonical_asset_code:
        return None, "order_asset_mismatch"
    if str(order.get("network") or "").lower() != config.canonical_network:
        return None, "network_mismatch"
    on_chain_tx_hash = _transaction_hash_or_none(order.get("transactionHash"))
    if on_chain_tx_hash is None:
        return None, "missing_on_chain_tx_hash"

    created_at = _timestamp(order.get("createdAt"), fallback=int(intent["iat"]))
    updated_at = _timestamp(order.get("updatedAt"), fallback=created_at)
    record = record_from_intent(transaction_id, intent)
    record.update(
        {
            "provider_transaction_id": provider_transaction_id,
            "status": "completed",
            "wallet_address": intent_wallet,
            "base_currency_code": _lower_string(order.get("fiatCurrency")),
            "base_currency_amount": _display_value(order.get("fiatAmount")),
            "quote_currency_amount": _display_value(order.get("cryptoAmount")),
            "on_chain_tx_hash": on_chain_tx_hash,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )
    return record, None


def transak_webhook_log_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deliberately non-sensitive webhook summary."""

    data = payload.get("webhookData")
    order = data if isinstance(data, dict) else {}
    return {
        "event_id": _short_identifier(payload.get("eventID")),
        "provider_transaction_id": _short_identifier(order.get("id") or order.get("_id")),
        "status": _short_identifier(order.get("status")),
        "product": _short_identifier(order.get("isBuyOrSell")),
        "has_partner_order_id": bool(order.get("partnerOrderId")),
        "has_transaction_hash": bool(order.get("transactionHash")),
    }


def _required_setting(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    return value


def _https_base_url(value: str | None) -> str:
    candidate = _required_setting(value).rstrip("/")
    if not _is_https_url(candidate, max_bytes=_MAX_PROVIDER_TEXT_BYTES, base_only=True):
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    return candidate


def _referrer_domain(value: str | None) -> str:
    candidate = _required_setting(value)
    # Privana is a web integration. Transak requires the exact approved domain,
    # so accept one lowercase ASCII DNS hostname with no scheme, port, or path.
    if not _REFERRER_DOMAIN_PATTERN.fullmatch(candidate) or not any(
        character.isalpha() for character in candidate.rsplit(".", 1)[-1]
    ):
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    return candidate


def _header_name(value: str | None) -> str:
    candidate = _required_setting(value)
    if not _HEADER_NAME_PATTERN.fullmatch(candidate):
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    return candidate.lower()


def _client_ip_source(
    *,
    mode: str | None,
    header: str | None,
    secret: str | None,
) -> tuple[str, str | None, str | None]:
    """Resolve session-only client-IP configuration.

    An unset mode preserves the original header-only deployments. Provider
    intent creation and order recovery deliberately do not call this loader.
    """

    resolved = mode.strip().lower() if isinstance(mode, str) and mode.strip() else None
    if resolved is None:
        resolved = TRANSAK_CLIENT_IP_MODE_HEADER
    if resolved == TRANSAK_CLIENT_IP_MODE_HEADER:
        return resolved, _header_name(header), None
    if resolved == TRANSAK_CLIENT_IP_MODE_ATTESTED:
        candidate = _required_setting(secret)
        if len(candidate) < TRANSAK_IP_ATTESTATION_MIN_SECRET_LENGTH:
            raise OnRampNotConfiguredError("Transak on-ramp is not configured")
        return resolved, None, candidate
    raise OnRampNotConfiguredError("Transak on-ramp is not configured")


def _provider_code(value: str | None) -> str:
    candidate = _required_setting(value)
    if not _PROVIDER_CODE_PATTERN.fullmatch(candidate):
        raise OnRampNotConfiguredError("Transak on-ramp is not configured")
    return candidate


def _exact_order_date_bounds(issued_at: int, now: float) -> tuple[str, str]:
    start = datetime.fromtimestamp(issued_at, tz=timezone.utc).date()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    end = min(today, start + timedelta(days=TRANSAK_EXACT_ORDER_WINDOW_DAYS))
    if end < start:
        end = start
    return start.isoformat(), end.isoformat()


def _is_exact_order_not_found(
    response: tuple[int, Any, Mapping[str, str]],
) -> bool:
    status_code, payload, _headers = response
    if status_code != 400 or not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return isinstance(error, dict) and error.get("message") == _EXACT_ORDER_NOT_FOUND_MESSAGE


def _orders_page(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TransakAPIError("Transak order response is malformed")
    raw_orders = payload["data"]
    if len(raw_orders) > TRANSAK_ORDER_PAGE_LIMIT:
        raise TransakAPIError("Transak order response is malformed")
    if any(not isinstance(order, dict) for order in raw_orders):
        raise TransakAPIError("Transak order response is malformed")
    meta = payload.get("meta")
    total_count: int | None = None
    if meta is not None:
        if not isinstance(meta, dict):
            raise TransakAPIError("Transak order response is malformed")
        raw_total_count = meta.get("totalCount")
        if raw_total_count is not None:
            if isinstance(raw_total_count, bool) or not isinstance(raw_total_count, int):
                raise TransakAPIError("Transak order response is malformed")
            if raw_total_count < 0:
                raise TransakAPIError("Transak order response is malformed")
            total_count = raw_total_count
    return list(raw_orders), total_count


def _successful_payload(
    response: tuple[int, Any, Mapping[str, str]],
    *,
    operation: str,
) -> Any:
    status_code, payload, headers = response
    if status_code == 429:
        raise TransakRateLimitError(
            f"Transak {operation} is rate limited",
            retry_after=_retry_after(headers.get("retry-after")),
        )
    if status_code < 200 or status_code >= 300:
        raise TransakAPIError(f"Transak {operation} failed")
    if payload is None:
        raise TransakAPIError(f"Transak {operation} response is malformed")
    return payload


def _retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        encoded = stripped.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_PROVIDER_TEXT_BYTES:
        return None
    return stripped


def _lower_string(value: Any) -> str | None:
    string = _nonempty_string(value)
    return string.lower() if string is not None else None


def _display_value(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    rendered = value.strip() if isinstance(value, str) else str(value)
    if not rendered:
        return None
    try:
        encoded = rendered.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return rendered if len(encoded) <= _MAX_DISPLAY_VALUE_BYTES else None


def _address_or_none(value: Any) -> str | None:
    if not isinstance(value, str) or not Web3.is_address(value):
        return None
    return Web3.to_checksum_address(value)


def _transaction_hash_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value if value.startswith("0x") else f"0x{value}"
    if not _TX_HASH_PATTERN.fullmatch(candidate):
        return None
    return candidate.lower()


def _timestamp(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_TIMESTAMP else fallback
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or value > _MAX_TIMESTAMP:
            return fallback
        return int(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.timestamp()
        except (OSError, OverflowError, ValueError):
            return fallback
        if not math.isfinite(timestamp) or timestamp < 0 or timestamp > _MAX_TIMESTAMP:
            return fallback
        return int(timestamp)
    return fallback


def _short_identifier(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or not _is_visible_ascii(value, max_bytes=_MAX_PROVIDER_TEXT_BYTES)
    ):
        return None
    if len(value) <= 18:
        return value
    return f"{value[:10]}...{value[-6:]}"


def _is_visible_ascii(value: str, *, max_bytes: int) -> bool:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        bool(encoded)
        and len(encoded) <= max_bytes
        and all(0x21 <= byte <= 0x7E for byte in encoded)
    )


def _is_https_url(value: str, *, max_bytes: int, base_only: bool = False) -> bool:
    if not _is_visible_ascii(value, max_bytes=max_bytes):
        return False
    try:
        parsed = urlparse(value)
        parsed_port = parsed.port
        normalized = httpx.URL(value)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or normalized.scheme != "https"
        or not normalized.host
        or (parsed_port is not None and parsed_port <= 0)
    ):
        return False
    if base_only and (parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return False
    return True


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _validate_json_value(value: Any) -> None:
    if isinstance(value, str):
        value.encode("utf-8")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8")
            _validate_json_value(item)


def _strict_json_loads(raw: bytes | bytearray) -> Any:
    value = json.loads(raw, parse_constant=_reject_json_constant)
    _validate_json_value(value)
    return value


_transak_service_instance: TransakService | None = None


def get_transak_service() -> TransakService:
    """Return the process-wide token-cache owner."""

    global _transak_service_instance
    if _transak_service_instance is None:
        _transak_service_instance = TransakService()
    return _transak_service_instance


__all__ = [
    "TRANSAK_EXACT_ORDER_WINDOW_DAYS",
    "TRANSAK_MAX_RESPONSE_BYTES",
    "TRANSAK_MAX_WEBHOOK_JWT_BYTES",
    "TRANSAK_ORDER_MAX_PAGES",
    "TRANSAK_ORDER_PAGE_LIMIT",
    "TRANSAK_WEBHOOK_PREVIOUS_TOKEN_OVERLAP_SECONDS",
    "TRANSAK_WIDGET_SESSION_TTL_SECONDS",
    "TransakAPIError",
    "TransakConfig",
    "TransakRateLimitError",
    "TransakSessionIpConfig",
    "TransakService",
    "TransakWebhookVerificationError",
    "client_ip_from_values",
    "create_transak_intent",
    "get_transak_service",
    "load_transak_config",
    "load_transak_session_ip_config",
    "pending_records_from_transak_orders",
    "transak_order_to_onramp_record",
    "transak_webhook_log_summary",
]
