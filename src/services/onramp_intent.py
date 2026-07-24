"""Provider-neutral signed on-ramp intent codec.

The initial v1 wire format is minified JSON and self-contained. It binds the
provider, authenticated user, server-derived deposit wallet, registered token,
destination chain, canonical provider asset, issue/expiry times, and nonce.

Minting uses the current provider-neutral signing key. Verification accepts
that key plus explicitly retained previous keys so rotation does not break
stateless recovery of still-relevant intents.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any

from oasis_rofl_client import KeyKind
from web3 import Web3

from src.clients.rofl import RoflAppdClient
from src.config import load_settings

logger = logging.getLogger(__name__)

INTENT_VERSION = 1
INTENT_PREFIX = "privana_"
# Defensive input bound, not a provider limit. Valid v1 intents are at most 448
# characters with the field limits below.
INTENT_MAX_LENGTH = 512
INTENT_TTL_SECONDS = 24 * 60 * 60
INTENT_MAX_ASSET_BYTES = 32

_BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_ASSET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,31}\Z")
_RAW_256_HEX_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_PAYLOAD_FIELDS = frozenset({"v", "p", "u", "w", "t", "c", "a", "iat", "exp", "n"})

PROVIDER_MOONPAY = "moonpay"
PROVIDER_TRANSAK = "transak"
_PROVIDERS = frozenset({PROVIDER_MOONPAY, PROVIDER_TRANSAK})


class OnRampError(ValueError):
    """Raised when an on-ramp request or signed intent is invalid."""


class OnRampNotConfiguredError(OnRampError):
    """Raised when no usable on-ramp intent signing key is configured."""


class OnRampIntentKeyManager:
    """Derive and retain the provider-neutral intent key ring."""

    def __init__(self) -> None:
        self._current_key: bytes | None = None
        self._verification_keys: tuple[bytes, ...] = ()

    async def initialize(self, use_rofl: bool = True) -> None:
        """Derive the configured key ring once during application startup."""

        if self._current_key is not None:
            return

        key_ids = _configured_key_ids()
        try:
            if use_rofl and not os.getenv("DISABLE_ROFL_KEYS"):
                derived_keys = []
                for key_id in key_ids:
                    derived_keys.append(await self._derive_rofl_key(key_id))
                keys = tuple(derived_keys)
            else:
                logger.warning("Using publicly derivable local on-ramp intent keys (non-TEE mode)")
                keys = tuple(_derive_local_key(key_id) for key_id in key_ids)
        except Exception as exc:
            raise RuntimeError("Failed to derive on-ramp intent signing keys") from exc

        self._current_key = keys[0]
        self._verification_keys = keys
        logger.info("On-ramp intent keys initialized for key IDs: %s", ", ".join(key_ids))

    async def _derive_rofl_key(self, key_id: str) -> bytes:
        key_hex = await RoflAppdClient()._client.generate_key(key_id, KeyKind.RAW_256)
        if not isinstance(key_hex, str) or not _RAW_256_HEX_PATTERN.fullmatch(key_hex):
            raise ValueError("ROFL returned an invalid on-ramp intent key")
        return bytes.fromhex(key_hex)

    @property
    def current_key(self) -> bytes:
        if self._current_key is None:
            raise RuntimeError("On-ramp intent key manager is not initialized")
        return self._current_key

    @property
    def verification_keys(self) -> tuple[bytes, ...]:
        if not self._verification_keys:
            raise RuntimeError("On-ramp intent key manager is not initialized")
        return self._verification_keys


_onramp_intent_key_manager_instance: OnRampIntentKeyManager | None = None


def get_onramp_intent_key_manager() -> OnRampIntentKeyManager:
    """Return the process-wide on-ramp intent key manager."""

    global _onramp_intent_key_manager_instance
    if _onramp_intent_key_manager_instance is None:
        _onramp_intent_key_manager_instance = OnRampIntentKeyManager()
    return _onramp_intent_key_manager_instance


def _configured_key_ids() -> tuple[str, ...]:
    settings = load_settings()
    current = settings.onramp_intent_signing_key_id
    if not current or current != current.strip():
        raise RuntimeError("On-ramp intent signing key ID is invalid")
    return tuple(dict.fromkeys((current, *settings.onramp_intent_previous_signing_key_ids)))


def _derive_local_key(key_id: str) -> bytes:
    """Derive a deterministic non-TEE key for local development and tests."""

    return hashlib.sha256(f"privana:onramp:intent:local:{key_id}".encode()).digest()


def create_intent(
    *,
    provider: str,
    user_address: str,
    wallet_address: str,
    token_id: str,
    chain_id: int,
    asset_code: str,
) -> tuple[str, dict[str, Any]]:
    """Create a signed provider-bound intent and its normalized payload."""

    if provider not in _PROVIDERS:
        raise OnRampError(f"Unsupported on-ramp provider {provider!r}")

    now = int(time.time())
    payload: dict[str, Any] = {
        "v": INTENT_VERSION,
        "p": provider,
        "u": _address_payload(user_address, "user_address"),
        "w": _address_payload(wallet_address, "wallet_address"),
        "t": _hex_payload(token_id, byte_length=32, field_name="token_id"),
        "c": _uint32(chain_id, "chain_id"),
        "a": _asset_code_or_error(asset_code),
        "iat": _uint32(now, "issued_at"),
        "exp": _uint32(now + INTENT_TTL_SECONDS, "expires_at"),
        "n": secrets.token_hex(8),
    }
    return _encode_intent(payload), payload


def decode_intent(token: str, *, allow_expired: bool = False) -> dict[str, Any]:
    """Verify and decode a canonical Privana on-ramp intent."""

    if not isinstance(token, str) or not token.startswith(INTENT_PREFIX):
        raise OnRampError("On-ramp intent is not a Privana intent")
    if len(token) > INTENT_MAX_LENGTH:
        raise OnRampError("On-ramp intent is too large")

    body = token.removeprefix(INTENT_PREFIX)
    if body.count(".") != 1:
        raise OnRampError("On-ramp intent is malformed")
    payload_b64, signature_b64 = body.split(".")
    if not payload_b64 or not signature_b64:
        raise OnRampError("On-ramp intent is malformed")

    try:
        payload_bytes = _b64url_decode(payload_b64)
    except ValueError as exc:
        raise OnRampError("On-ramp intent payload is malformed") from exc
    try:
        provided_signature = _b64url_decode(signature_b64)
    except ValueError as exc:
        raise OnRampError("On-ramp intent signature is malformed") from exc
    if not payload_bytes:
        raise OnRampError("On-ramp intent payload is malformed")
    if len(provided_signature) != hashlib.sha256().digest_size:
        raise OnRampError("On-ramp intent signature is malformed")

    signature_matches = [
        hmac.compare_digest(_signature(payload_b64, key), provided_signature)
        for key in _verification_keys()
    ]
    if not any(signature_matches):
        raise OnRampError("On-ramp intent signature mismatch")

    payload = _decode_payload(payload_bytes)
    _validate(payload)
    if payload_bytes != _json_payload(payload):
        raise OnRampError("On-ramp intent payload is malformed")
    if not allow_expired and payload["exp"] < int(time.time()):
        raise OnRampError("On-ramp intent has expired")
    return payload


def configured_provider() -> str:
    """Return the deployment-selected provider, failing closed on invalid values."""

    provider = load_settings().onramp_provider
    if provider not in _PROVIDERS:
        raise OnRampNotConfiguredError("On-ramp provider configuration is invalid")
    return provider


def record_from_intent(transaction_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build the provider-neutral API record represented by a signed intent."""

    provider = str(payload["p"])
    asset_code = str(payload["a"]).lower()
    created_at = int(payload["iat"])
    return {
        "transaction_id": transaction_id,
        "external_transaction_id": transaction_id,
        "provider": provider,
        "provider_transaction_id": None,
        "provider_asset_code": asset_code,
        "moonpay_transaction_id": None,
        "status": "pending",
        "wallet_address": Web3.to_checksum_address("0x" + str(payload["w"])),
        "token_id": "0x" + str(payload["t"]).lower(),
        "chain_id": int(payload["c"]),
        # Retain the MoonPay field during the SDK migration.
        "moonpay_currency_code": asset_code if provider == PROVIDER_MOONPAY else None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def _encode_intent(payload: dict[str, Any]) -> str:
    """Encode and sign a normalized payload."""

    _validate(payload)
    payload_b64 = _b64url_encode(_json_payload(payload))
    signature_b64 = _b64url_encode(_signature(payload_b64, _signing_key()))
    token = f"{INTENT_PREFIX}{payload_b64}.{signature_b64}"
    if len(token) > INTENT_MAX_LENGTH:
        raise OnRampError("On-ramp intent is too large")
    return token


def _signature(payload_b64: str, key: bytes) -> bytes:
    message = f"privana:onramp:intent:v{INTENT_VERSION}:{payload_b64}".encode()
    return hmac.new(key, message, hashlib.sha256).digest()


def _signing_key() -> bytes:
    current, _keys = _key_ring()
    return current


def _verification_keys() -> tuple[bytes, ...]:
    _current, keys = _key_ring()
    return keys


def _key_ring() -> tuple[bytes, tuple[bytes, ...]]:
    try:
        manager = get_onramp_intent_key_manager()
        return manager.current_key, manager.verification_keys
    except RuntimeError as exc:
        raise OnRampNotConfiguredError("On-ramp intents are not configured") from exc


def _json_payload(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise OnRampError("On-ramp intent payload is malformed") from exc


def _decode_payload(payload_bytes: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OnRampError("On-ramp intent payload is malformed") from exc
    if not isinstance(payload, dict):
        raise OnRampError("On-ramp intent payload is malformed")
    return payload


def _validate(payload: dict[str, Any]) -> None:
    if set(payload) != _PAYLOAD_FIELDS:
        raise OnRampError("On-ramp intent payload is malformed")
    if type(payload["v"]) is not int or payload["v"] != INTENT_VERSION:
        raise OnRampError("On-ramp intent version is unsupported")
    if not isinstance(payload["p"], str) or payload["p"] not in _PROVIDERS:
        raise OnRampError("Unsupported on-ramp provider")
    if payload["u"] != _hex_payload(payload["u"], byte_length=20, field_name="user"):
        raise OnRampError("Invalid user address")
    if payload["w"] != _hex_payload(payload["w"], byte_length=20, field_name="wallet"):
        raise OnRampError("Invalid wallet address")
    if payload["t"] != _hex_payload(payload["t"], byte_length=32, field_name="token_id"):
        raise OnRampError("Invalid token_id")
    if payload["n"] != _hex_payload(payload["n"], byte_length=8, field_name="nonce"):
        raise OnRampError("Invalid nonce")
    _uint32(payload["c"], "chain_id")
    issued_at = _uint32(payload["iat"], "issued_at")
    expires_at = _uint32(payload["exp"], "expires_at")
    if expires_at < issued_at:
        raise OnRampError("On-ramp intent expires before it is issued")
    asset = payload["a"]
    if asset != _asset_code_or_error(asset):
        raise OnRampError("Invalid on-ramp asset code")


def _address_payload(value: str, field_name: str) -> str:
    if not Web3.is_address(value):
        raise OnRampError(f"Invalid {field_name}")
    return Web3.to_checksum_address(value).removeprefix("0x").lower()


def _hex_payload(value: Any, *, byte_length: int, field_name: str) -> str:
    if not isinstance(value, str):
        raise OnRampError(f"Invalid {field_name}")
    stripped = value.lower().removeprefix("0x")
    if len(stripped) != byte_length * 2:
        raise OnRampError(f"Invalid {field_name}")
    try:
        bytes.fromhex(stripped)
    except ValueError as exc:
        raise OnRampError(f"Invalid {field_name}") from exc
    return stripped


def _uint32(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0 or value > 0xFFFFFFFF:
        raise OnRampError(f"Invalid {field_name}")
    return value


def _asset_code_or_error(value: str | None) -> str:
    if not isinstance(value, str):
        raise OnRampError("Invalid on-ramp asset code")
    code = value.strip().lower()
    if not code or any(ord(character) > 0x7F for character in code):
        raise OnRampError("Invalid on-ramp asset code")
    if len(code.encode("ascii")) > INTENT_MAX_ASSET_BYTES:
        raise OnRampError("On-ramp asset code is too large")
    if not _ASSET_PATTERN.fullmatch(code):
        raise OnRampError("Invalid on-ramp asset code")
    return code


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not value or not _BASE64URL_PATTERN.fullmatch(value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url") from exc
    if _b64url_encode(decoded) != value:
        raise ValueError("noncanonical base64url")
    return decoded


__all__ = [
    "INTENT_MAX_ASSET_BYTES",
    "INTENT_MAX_LENGTH",
    "INTENT_PREFIX",
    "INTENT_TTL_SECONDS",
    "INTENT_VERSION",
    "OnRampIntentKeyManager",
    "OnRampError",
    "OnRampNotConfiguredError",
    "PROVIDER_MOONPAY",
    "PROVIDER_TRANSAK",
    "configured_provider",
    "create_intent",
    "decode_intent",
    "get_onramp_intent_key_manager",
    "record_from_intent",
]
