"""Provider-neutral signed on-ramp intent codec.

The initial v1 wire format is compact and self-contained. It binds the
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
import re
import secrets
import struct
import time
from typing import Any

from web3 import Web3

from src.config import load_settings

INTENT_VERSION = 1
INTENT_PREFIX = "privana_"
INTENT_MAX_LENGTH = 255
INTENT_TTL_SECONDS = 24 * 60 * 60
INTENT_MAX_ASSET_BYTES = 32
INTENT_MIN_SIGNING_KEY_BYTES = 32

# Binary encoding keeps signed intents within Privana's shared 255-character budget.
_INTENT_STRUCT = struct.Struct(">BBIII20s20s32s8sB")
_BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_ASSET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,31}\Z")
_INVALID_SIGNING_KEY_MESSAGE = "On-ramp intent signing key configuration is invalid"

PROVIDER_MOONPAY = "moonpay"
PROVIDER_TRANSAK = "transak"

# Provider IDs are part of the signed wire format. Never renumber or reuse one.
_PROVIDER_IDS = {
    PROVIDER_MOONPAY: 1,
    PROVIDER_TRANSAK: 2,
}
_PROVIDER_NAMES = {provider_id: provider for provider, provider_id in _PROVIDER_IDS.items()}


class OnRampError(ValueError):
    """Raised when an on-ramp request or signed intent is invalid."""


class OnRampNotConfiguredError(OnRampError):
    """Raised when no usable on-ramp intent signing key is configured."""


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

    if provider not in _PROVIDER_IDS:
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
    if payload_bytes[0] != INTENT_VERSION:
        raise OnRampError("On-ramp intent version is unsupported")

    signature_matches = [
        hmac.compare_digest(_signature(payload_b64, key), provided_signature)
        for key in _verification_keys()
    ]
    if not any(signature_matches):
        raise OnRampError("On-ramp intent signature mismatch")

    payload = _unpack(payload_bytes)
    _validate(payload)
    if not allow_expired and int(payload["exp"]) < int(time.time()):
        raise OnRampError("On-ramp intent has expired")
    return payload


def _encode_intent(payload: dict[str, Any]) -> str:
    """Encode and sign a normalized payload."""

    packed = _pack(payload)
    payload_b64 = _b64url_encode(packed)
    signature_b64 = _b64url_encode(_signature(payload_b64, _signing_key()))
    token = f"{INTENT_PREFIX}{payload_b64}.{signature_b64}"
    if len(token) > INTENT_MAX_LENGTH:
        raise OnRampError("On-ramp intent is too large")
    return token


def _signature(payload_b64: str, key: str) -> bytes:
    message = f"privana:onramp:intent:v{INTENT_VERSION}:{payload_b64}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).digest()


def _signing_key() -> str:
    current, _keys = _key_ring()
    if not current:
        raise OnRampNotConfiguredError("On-ramp intents are not configured")
    return current


def _verification_keys() -> tuple[str, ...]:
    _current, keys = _key_ring()
    if not keys:
        raise OnRampNotConfiguredError("On-ramp intents are not configured")
    return keys


def _key_ring() -> tuple[str | None, tuple[str, ...]]:
    settings = load_settings()
    current = _validated_signing_key(settings.onramp_intent_signing_key)
    previous = tuple(
        _validated_signing_key(key) for key in settings.onramp_intent_previous_signing_keys
    )
    candidates = (current, *previous)
    keys = tuple(
        key for index, key in enumerate(candidates) if key and key not in candidates[:index]
    )
    return current, keys


def _validated_signing_key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OnRampNotConfiguredError(_INVALID_SIGNING_KEY_MESSAGE) from exc
    if (
        len(encoded) < INTENT_MIN_SIGNING_KEY_BYTES
        or "," in value
        or any(character.isspace() for character in value)
    ):
        raise OnRampNotConfiguredError(_INVALID_SIGNING_KEY_MESSAGE)
    return value


def _pack(payload: dict[str, Any]) -> bytes:
    try:
        version = int(payload["v"])
        provider = str(payload["p"])
        provider_id = _PROVIDER_IDS.get(provider)
        if provider_id is None:
            raise OnRampError("Unsupported on-ramp provider")
        if version != INTENT_VERSION:
            raise OnRampError("On-ramp intent version is unsupported")

        asset = _asset_code_or_error(str(payload["a"])).encode("ascii")
        packed = _INTENT_STRUCT.pack(
            version,
            provider_id,
            _uint32(payload["iat"], "issued_at"),
            _uint32(payload["exp"], "expires_at"),
            _uint32(payload["c"], "chain_id"),
            bytes.fromhex(_hex_payload(str(payload["u"]), byte_length=20, field_name="user")),
            bytes.fromhex(_hex_payload(str(payload["w"]), byte_length=20, field_name="wallet")),
            bytes.fromhex(_hex_payload(str(payload["t"]), byte_length=32, field_name="token_id")),
            bytes.fromhex(_hex_payload(str(payload["n"]), byte_length=8, field_name="nonce")),
            len(asset),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, OnRampError):
            raise
        raise OnRampError("On-ramp intent payload is incomplete") from exc
    return packed + asset


def _unpack(payload_bytes: bytes) -> dict[str, Any]:
    if len(payload_bytes) < _INTENT_STRUCT.size:
        raise OnRampError("On-ramp intent payload is malformed")

    try:
        (
            version,
            provider_id,
            issued_at,
            expires_at,
            chain_id,
            user,
            wallet,
            token_id,
            nonce,
            asset_length,
        ) = _INTENT_STRUCT.unpack_from(payload_bytes)

        provider = _PROVIDER_NAMES.get(provider_id)
        if provider is None:
            raise OnRampError("Unsupported on-ramp provider")
        expected_length = _INTENT_STRUCT.size + asset_length
        if len(payload_bytes) != expected_length:
            raise OnRampError("On-ramp intent payload is malformed")
        asset = payload_bytes[_INTENT_STRUCT.size : expected_length].decode("ascii")
    except (struct.error, UnicodeDecodeError) as exc:
        raise OnRampError("On-ramp intent payload is malformed") from exc

    return {
        "v": version,
        "p": provider,
        "u": user.hex(),
        "w": wallet.hex(),
        "t": token_id.hex(),
        "c": chain_id,
        "a": asset,
        "iat": issued_at,
        "exp": expires_at,
        "n": nonce.hex(),
    }


def _validate(payload: dict[str, Any]) -> None:
    required = ("v", "p", "u", "w", "t", "c", "a", "iat", "exp", "n")
    if any(payload.get(field) in (None, "") for field in required):
        raise OnRampError("On-ramp intent payload is incomplete")
    if int(payload["v"]) != INTENT_VERSION:
        raise OnRampError("On-ramp intent version is unsupported")
    if payload["p"] not in _PROVIDER_IDS:
        raise OnRampError("Unsupported on-ramp provider")
    if not Web3.is_address("0x" + str(payload["u"])):
        raise OnRampError("Invalid user address")
    if not Web3.is_address("0x" + str(payload["w"])):
        raise OnRampError("Invalid wallet address")
    _hex_payload(str(payload["t"]), byte_length=32, field_name="token_id")
    _hex_payload(str(payload["n"]), byte_length=8, field_name="nonce")
    _uint32(payload["c"], "chain_id")
    issued_at = _uint32(payload["iat"], "issued_at")
    expires_at = _uint32(payload["exp"], "expires_at")
    if expires_at < issued_at:
        raise OnRampError("On-ramp intent expires before it is issued")
    asset = str(payload["a"])
    if asset != _asset_code_or_error(asset):
        raise OnRampError("Invalid on-ramp asset code")


def _address_payload(value: str, field_name: str) -> str:
    if not Web3.is_address(value):
        raise OnRampError(f"Invalid {field_name}")
    return Web3.to_checksum_address(value).removeprefix("0x").lower()


def _hex_payload(value: str, *, byte_length: int, field_name: str) -> str:
    stripped = value.lower().removeprefix("0x")
    if len(stripped) != byte_length * 2:
        raise OnRampError(f"Invalid {field_name}")
    try:
        bytes.fromhex(stripped)
    except ValueError as exc:
        raise OnRampError(f"Invalid {field_name}") from exc
    return stripped


def _uint32(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OnRampError(f"Invalid {field_name}") from exc
    if parsed < 0 or parsed > 0xFFFFFFFF:
        raise OnRampError(f"Invalid {field_name}")
    return parsed


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
    "INTENT_MIN_SIGNING_KEY_BYTES",
    "INTENT_PREFIX",
    "INTENT_TTL_SECONDS",
    "INTENT_VERSION",
    "OnRampError",
    "OnRampNotConfiguredError",
    "PROVIDER_MOONPAY",
    "PROVIDER_TRANSAK",
    "create_intent",
    "decode_intent",
]
