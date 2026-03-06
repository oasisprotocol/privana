"""JWT service for token creation and verification."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from web3 import Web3

from src.auth.jwt_keys import get_jwt_key_manager
from src.auth.token_store import get_token_store

logger = logging.getLogger(__name__)

DEFAULT_JWT_EXPIRY_HOURS = 12
DEFAULT_REFRESH_TOKEN_EXPIRY_DAYS = 7
DEFAULT_JWT_ISSUER = "flexvaults"
DEFAULT_JWT_AUDIENCE = "flexvaults"

# Token types
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


class JWTService:
    """Service for creating and verifying EdDSA JWTs."""

    def __init__(self) -> None:
        self._key_manager = get_jwt_key_manager()
        self._token_store = get_token_store()
        self._expiry_hours = int(os.getenv("JWT_EXPIRY_HOURS", DEFAULT_JWT_EXPIRY_HOURS))
        self._refresh_expiry_days = int(
            os.getenv("JWT_REFRESH_EXPIRY_DAYS", DEFAULT_REFRESH_TOKEN_EXPIRY_DAYS)
        )
        self._issuer = os.getenv("JWT_ISSUER", DEFAULT_JWT_ISSUER)
        self._audience = os.getenv("JWT_AUDIENCE", DEFAULT_JWT_AUDIENCE)
        logger.info(
            f"JWT service initialized with issuer: {self._issuer}, "
            f"audience: {self._audience}, "
            f"access expiry: {self._expiry_hours}h, refresh expiry: {self._refresh_expiry_days}d"
        )

    def create_token(self, address: str) -> str:
        """Create an access JWT for an authenticated user.

        Args:
            address: Ethereum address of the user (will be checksummed).

        Returns:
            Signed JWT string.
        """
        # Normalize address to checksum format
        address = Web3.to_checksum_address(address)

        now = datetime.now(timezone.utc)
        expiry = now + timedelta(hours=self._expiry_hours)

        payload = {
            "sub": address,  # Subject: the user's Ethereum address
            "iss": self._issuer,  # Issuer
            "aud": self._audience,  # Audience
            "iat": int(now.timestamp()),  # Issued at
            "nbf": int(now.timestamp()),  # Not before (same as iat)
            "exp": int(expiry.timestamp()),  # Expiration
            "type": TOKEN_TYPE_ACCESS,  # Token type
        }

        token = jwt.encode(
            payload,
            self._key_manager.get_private_key_pem(),
            algorithm=self._key_manager.algorithm,
            headers={"kid": self._key_manager.key_id},
        )

        logger.debug(f"Created access JWT for {address}, expires at {expiry}")
        return token

    def create_refresh_token(self, address: str) -> str:
        """Create a refresh token for an authenticated user.

        Args:
            address: Ethereum address of the user (will be checksummed).

        Returns:
            Signed refresh JWT string.
        """
        # Normalize address to checksum format
        address = Web3.to_checksum_address(address)

        now = datetime.now(timezone.utc)
        expiry = now + timedelta(days=self._refresh_expiry_days)

        # Generate unique token ID for revocation support
        jti = secrets.token_urlsafe(32)

        payload = {
            "sub": address,
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),  # Not before (same as iat)
            "exp": int(expiry.timestamp()),
            "type": TOKEN_TYPE_REFRESH,
            "jti": jti,
        }

        token = jwt.encode(
            payload,
            self._key_manager.get_private_key_pem(),
            algorithm=self._key_manager.algorithm,
            headers={"kid": self._key_manager.key_id},
        )

        # Store refresh token for validation/revocation
        self._token_store.store_refresh_token(jti, address, expiry.timestamp())

        logger.debug(f"Created refresh token for {address}, expires at {expiry}")
        return token

    def verify_token(self, token: str) -> dict:
        """Verify a JWT and return its claims.

        Args:
            token: JWT string to verify.

        Returns:
            Decoded payload dict.

        Raises:
            jwt.InvalidTokenError: If verification fails.
        """
        try:
            payload = jwt.decode(
                token,
                self._key_manager.get_public_key_pem(),
                algorithms=[self._key_manager.algorithm],
                issuer=self._issuer,
                audience=self._audience,
            )
            logger.debug(f"Verified JWT for {payload.get('sub')}")
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("JWT has expired")
            raise
        except jwt.InvalidIssuerError:
            logger.warning("JWT has invalid issuer")
            raise
        except jwt.InvalidAudienceError:
            logger.warning("JWT has invalid audience")
            raise
        except jwt.InvalidTokenError as e:
            logger.warning(f"JWT verification failed: {e}")
            raise

    def get_address_from_token(self, token: str) -> str:
        """Extract the Ethereum address from a verified access JWT.

        Args:
            token: JWT string to verify and extract address from.

        Returns:
            Checksummed Ethereum address.

        Raises:
            jwt.InvalidTokenError: If verification fails.
            ValueError: If token doesn't contain valid address or is not an access token.
        """
        payload = self.verify_token(token)

        if payload.get("type") != TOKEN_TYPE_ACCESS:
            raise ValueError("Expected access token")

        address = payload.get("sub")
        if not address:
            raise ValueError("Token missing 'sub' claim")
        return Web3.to_checksum_address(address)

    def verify_refresh_token(self, token: str) -> str:
        """Verify a refresh token and return the associated address.

        Args:
            token: Refresh JWT string to verify.

        Returns:
            Checksummed Ethereum address.

        Raises:
            jwt.InvalidTokenError: If verification fails.
            ValueError: If token is invalid, revoked, or not a refresh token.
        """
        payload = self.verify_token(token)

        # Ensure this is a refresh token
        if payload.get("type") != TOKEN_TYPE_REFRESH:
            raise ValueError("Expected refresh token")

        jti = payload.get("jti")
        if not jti:
            raise ValueError("Refresh token missing 'jti' claim")

        # Check if token is in our store (not revoked)
        if not self._token_store.is_refresh_token_valid(jti):
            raise ValueError("Refresh token has been revoked")

        address = payload.get("sub")
        if not address:
            raise ValueError("Token missing 'sub' claim")

        return Web3.to_checksum_address(address)

    def refresh_tokens(self, refresh_token: str) -> tuple[str, str]:
        """Exchange a refresh token for new access and refresh tokens.

        Implements token rotation: the old refresh token is atomically
        consumed and a new one is issued.

        Args:
            refresh_token: Current refresh JWT string.

        Returns:
            Tuple of (new_access_token, new_refresh_token).

        Raises:
            jwt.InvalidTokenError: If verification fails.
            ValueError: If token is invalid or revoked.
        """
        # First verify the JWT signature and claims
        payload = self.verify_token(refresh_token)

        # Ensure this is a refresh token
        if payload.get("type") != TOKEN_TYPE_REFRESH:
            raise ValueError("Expected refresh token")

        jti = payload.get("jti")
        if not jti:
            raise ValueError("Refresh token missing 'jti' claim")

        # Atomically consume the refresh token (verify it exists and delete it)
        # This prevents TOCTOU race conditions
        address = self._token_store.consume_refresh_token(jti)
        if address is None:
            raise ValueError("Refresh token has been revoked")

        # Issue new tokens
        new_access_token = self.create_token(address)
        new_refresh_token = self.create_refresh_token(address)

        logger.info(f"Refreshed tokens for {address}")
        return new_access_token, new_refresh_token

    def revoke_refresh_token(self, token: str) -> bool:
        """Revoke a refresh token.

        Args:
            token: Refresh JWT string to revoke.

        Returns:
            True if token was revoked, False if it wasn't found.
        """
        try:
            payload = self.verify_token(token)
            jti = payload.get("jti")
            if jti and self._token_store.delete_refresh_token(jti):
                logger.info(f"Revoked refresh token for {payload.get('sub')}")
                return True
        except jwt.InvalidTokenError:
            pass
        return False

    def revoke_all_refresh_tokens(self, address: str) -> int:
        """Revoke all refresh tokens for an address.

        Args:
            address: Ethereum address (will be checksummed).

        Returns:
            Number of tokens revoked.
        """
        address = Web3.to_checksum_address(address)
        count = self._token_store.revoke_all_refresh_tokens_for_address(address)
        if count > 0:
            logger.info(f"Revoked {count} refresh token(s) for {address}")
        return count

    @property
    def access_token_expiry_seconds(self) -> int:
        """Get access token expiry duration in seconds."""
        return self._expiry_hours * 3600

    @property
    def refresh_token_expiry_seconds(self) -> int:
        """Get refresh token expiry duration in seconds."""
        return self._refresh_expiry_days * 24 * 3600

    def get_jwks(self) -> dict:
        """Get JWKS for external verification.

        Returns:
            JWKS dict with public key.
        """
        return self._key_manager.get_jwks()


_jwt_service_instance: Optional[JWTService] = None


def get_jwt_service() -> JWTService:
    """Get the singleton JWTService instance."""
    global _jwt_service_instance
    if _jwt_service_instance is None:
        _jwt_service_instance = JWTService()
    return _jwt_service_instance
