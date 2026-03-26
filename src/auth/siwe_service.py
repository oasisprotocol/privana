"""Reusable server-side SIWE authentication flow."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from siwe import ExpiredMessage, InvalidSignature, MalformedSession, SiweMessage
from web3 import Web3

from src.auth.auth_token_service import get_auth_token_service
from src.auth.siwe_config import get_siwe_config
from src.auth.token_store import get_token_store
from src.config import load_settings

logger = logging.getLogger(__name__)


class SiweAuthError(Exception):
    """Structured SIWE authentication error."""

    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class AuthenticatedSiwe:
    """Server-side result of a successful SIWE authentication."""

    address: str
    valid_until: int
    statement: str
    resources: list[str]
    siwe_token_bytes: bytes
    siwe_token_hex: str


def authenticate_siwe_message(siwe_message_text: str, signature: str) -> AuthenticatedSiwe:
    """Verify a SIWE message and mint a Sapphire-compatible AuthToken."""
    settings = load_settings()
    try:
        siwe_config = get_siwe_config(settings)
    except ValueError as exc:
        raise SiweAuthError(str(exc), status_code=500) from exc

    token_store = get_token_store()
    auth_token_service = get_auth_token_service()

    try:
        siwe_message = SiweMessage.from_message(siwe_message_text)
    except Exception as exc:
        logger.warning("Invalid SIWE message format: %s", exc)
        raise SiweAuthError("Invalid SIWE message") from exc

    nonce = siwe_message.nonce
    if not nonce:
        raise SiweAuthError("Invalid or expired nonce. Request a new nonce from /auth/nonce")

    try:
        address = Web3.to_checksum_address(siwe_message.address)
    except Exception as exc:
        raise SiweAuthError("Invalid SIWE address") from exc

    if not token_store.is_nonce_valid(nonce):
        raise SiweAuthError("Invalid or expired nonce. Request a new nonce from /auth/nonce")

    allowed_chains = settings.siwe_allowed_chain_ids
    if not allowed_chains:
        allowed_chains = set(settings.chain_rpc_urls.keys()) | {settings.sapphire_chain_id}
    if not siwe_message.chain_id or siwe_message.chain_id not in allowed_chains:
        logger.warning(
            "SIWE chain_id %s not in allowed chains %s for %s",
            siwe_message.chain_id,
            allowed_chains,
            address,
        )
        raise SiweAuthError(f"Chain ID {siwe_message.chain_id} is not supported")

    try:
        provider = None
        if siwe_message.chain_id in settings.chain_rpc_urls:
            provider = Web3.HTTPProvider(settings.chain_rpc_urls[siwe_message.chain_id])
        siwe_message.verify(
            signature,
            domain=siwe_config.domain,
            nonce=nonce,
            provider=provider,
        )
    except InvalidSignature as exc:
        raise SiweAuthError("Invalid signature") from exc
    except ExpiredMessage as exc:
        raise SiweAuthError("SIWE message expired") from exc
    except MalformedSession as exc:
        raise SiweAuthError("Invalid SIWE message format") from exc
    except Exception as exc:
        logger.warning("SIWE verification failed for %s: %s", address, exc)
        raise SiweAuthError("SIWE verification failed") from exc

    tolerance_seconds = 5 * 60
    now = int(time.time())

    if not siwe_message.issued_at:
        raise SiweAuthError("SIWE message must include issued_at")

    try:
        issued_at_dt = datetime.fromisoformat(str(siwe_message.issued_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SiweAuthError("SIWE message issued_at is invalid") from exc
    issued_at = int(issued_at_dt.timestamp())
    if abs(now - issued_at) > tolerance_seconds:
        raise SiweAuthError("SIWE message issued_at is outside acceptable time range")

    if not siwe_message.expiration_time:
        raise SiweAuthError("SIWE message must include expiration_time")

    try:
        expiration_dt = datetime.fromisoformat(
            str(siwe_message.expiration_time).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise SiweAuthError("SIWE message expiration_time is invalid") from exc
    valid_until = int(expiration_dt.timestamp())
    if valid_until <= now:
        raise SiweAuthError("SIWE message expired")
    max_valid_until = now + settings.auth_token_validity_seconds + tolerance_seconds
    if valid_until > max_valid_until:
        raise SiweAuthError("SIWE expiration_time exceeds the maximum allowed validity window")

    statement = siwe_message.statement or ""
    resources = list(siwe_message.resources) if siwe_message.resources else []

    try:
        siwe_token_bytes = auth_token_service.create_and_encrypt(
            domain=siwe_config.domain,
            user_addr=address,
            valid_until=valid_until,
            statement=statement,
            resources=resources,
        )
    except Exception as exc:
        logger.exception("Failed to generate AuthToken")
        raise SiweAuthError("Failed to generate auth token", status_code=500) from exc

    if not token_store.consume_nonce(nonce):
        raise SiweAuthError("Nonce already used. Request a new nonce from /auth/nonce")

    siwe_token_hex = "0x" + siwe_token_bytes.hex()
    return AuthenticatedSiwe(
        address=address,
        valid_until=valid_until,
        statement=statement,
        resources=resources,
        siwe_token_bytes=siwe_token_bytes,
        siwe_token_hex=siwe_token_hex,
    )
