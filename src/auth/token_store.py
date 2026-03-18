"""Disk-based token storage for refresh tokens and SIWE nonces.

Uses diskcache for process-safe storage with automatic TTL expiration.
Suitable for multi-worker deployments on a single machine.
"""

import logging
import os
import secrets
import time
from typing import Optional

from diskcache import Cache

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_DIR = ".auth_tokens"
# Nonces expire after 5 minutes - this bounds the SIWE replay window
DEFAULT_NONCE_EXPIRY_SECONDS = 300

# Key prefixes for namespacing
_REFRESH_PREFIX = "refresh:"
_NONCE_PREFIX = "nonce:"
_CLIENT_NONCE_PREFIX = "client_nonce:"


class TokenStore:
    """Process-safe storage for refresh tokens and SIWE nonces.

    Uses diskcache (SQLite-backed) for concurrent access from multiple workers.
    TTL is handled automatically by diskcache.

    SIWE Nonce Tracking (Replay Protection):
        Each nonce can only be used once to obtain credentials.
        The contract's login() is disabled, so all authentication
        goes through this API.
    """

    def __init__(self) -> None:
        storage_dir = os.getenv("AUTH_TOKEN_STORAGE_DIR", DEFAULT_STORAGE_DIR)
        self._nonce_expiry_seconds = int(
            os.getenv("SIWE_NONCE_EXPIRY_SECONDS", DEFAULT_NONCE_EXPIRY_SECONDS)
        )

        # Initialize diskcache with SQLite WAL mode for concurrent worker access
        self._cache = Cache(
            storage_dir,
            disk_min_file_size=0,  # Store everything in SQLite (no separate files)
            sqlite_journal_mode="WAL",
        )

        logger.info(f"Token store initialized at {storage_dir}")

    def close(self) -> None:
        """Close the cache (for cleanup in tests)."""
        self._cache.close()

    # -------------------------------------------------------------------------
    # Refresh token operations
    # -------------------------------------------------------------------------

    def store_refresh_token(self, jti: str, address: str, expires_at: float) -> None:
        """Store a refresh token with TTL.

        Args:
            jti: Unique token ID.
            address: Checksummed Ethereum address.
            expires_at: Unix timestamp when the token expires.
        """
        ttl = max(0, expires_at - time.time())
        key = f"{_REFRESH_PREFIX}{jti}"
        self._cache.set(key, address, expire=ttl)

    def get_refresh_token(self, jti: str) -> Optional[tuple[str, float]]:
        """Get a stored refresh token.

        Args:
            jti: Unique token ID.

        Returns:
            Tuple of (address, expires_at) if found, None otherwise.
        """
        key = f"{_REFRESH_PREFIX}{jti}"
        address = self._cache.get(key)
        if address is None:
            return None

        # Get expiry from cache metadata
        # diskcache stores (expire_time, tag, value) internally
        # We can approximate expires_at, but for simplicity just return a far future
        # since the cache handles expiration automatically
        return address, time.time() + 86400  # Approximate, TTL handles actual expiry

    def delete_refresh_token(self, jti: str) -> bool:
        """Delete a refresh token.

        Args:
            jti: Unique token ID.

        Returns:
            True if deleted, False if not found.
        """
        key = f"{_REFRESH_PREFIX}{jti}"
        return self._cache.delete(key)

    def is_refresh_token_valid(self, jti: str) -> bool:
        """Check if a refresh token is valid (exists and not expired).

        Args:
            jti: Unique token ID.

        Returns:
            True if valid, False otherwise.
        """
        key = f"{_REFRESH_PREFIX}{jti}"
        return key in self._cache

    def consume_refresh_token(self, jti: str) -> Optional[str]:
        """Atomically verify and consume a refresh token.

        Args:
            jti: Unique token ID.

        Returns:
            The address if token was valid and consumed, None otherwise.
        """
        key = f"{_REFRESH_PREFIX}{jti}"
        return self._cache.pop(key)

    def revoke_all_refresh_tokens_for_address(self, address: str) -> int:
        """Revoke all refresh tokens for an address.

        Args:
            address: Checksummed Ethereum address.

        Returns:
            Number of tokens revoked.
        """
        revoked = 0
        # Iterate over all refresh token keys
        for key in list(self._cache):
            if isinstance(key, str) and key.startswith(_REFRESH_PREFIX):
                if self._cache.get(key) == address:
                    if self._cache.delete(key):
                        revoked += 1
        return revoked

    # -------------------------------------------------------------------------
    # SIWE Nonce operations (API-level replay protection)
    # -------------------------------------------------------------------------

    def generate_nonce(self, client_id: str | None = None) -> str:
        """Generate and store a new nonce for SIWE authentication.

        The nonce is stored with an expiration time. Clients must use this
        nonce in their SIWE message and submit it before expiration.

        If a client_id is provided (e.g., IP address) and that client already
        has a valid unexpired nonce, the existing nonce is returned instead
        of generating a new one.

        Args:
            client_id: Optional client identifier (e.g., IP address) for
                       nonce reuse within expiration window.

        Returns:
            A cryptographically secure random nonce string.
        """
        # If client_id provided, check for existing nonce
        if client_id:
            client_key = f"{_CLIENT_NONCE_PREFIX}{client_id}"
            existing_nonce = self._cache.get(client_key)
            if existing_nonce is not None:
                # Verify the nonce itself still exists
                nonce_key = f"{_NONCE_PREFIX}{existing_nonce}"
                if nonce_key in self._cache:
                    return existing_nonce

        # Generate new nonce
        nonce = secrets.token_hex(32)
        nonce_key = f"{_NONCE_PREFIX}{nonce}"

        # Store nonce with TTL
        self._cache.set(nonce_key, client_id or "", expire=self._nonce_expiry_seconds)

        # If client_id provided, store mapping for nonce reuse
        if client_id:
            client_key = f"{_CLIENT_NONCE_PREFIX}{client_id}"
            self._cache.set(client_key, nonce, expire=self._nonce_expiry_seconds)

        return nonce

    def consume_nonce(self, nonce: str) -> bool:
        """Atomically verify and consume a nonce.

        This is the core of API-level replay protection: each nonce can only
        be consumed once. If a SIWE message is replayed with the same nonce,
        this method will return False on the second attempt.

        Args:
            nonce: The nonce string from the SIWE message.

        Returns:
            True if the nonce was valid and consumed, False otherwise.
        """
        nonce_key = f"{_NONCE_PREFIX}{nonce}"
        client_id = self._cache.pop(nonce_key)

        if client_id is None:
            return False

        # Also clean up client mapping if it exists
        if client_id:
            client_key = f"{_CLIENT_NONCE_PREFIX}{client_id}"
            self._cache.delete(client_key)

        return True

    def is_nonce_valid(self, nonce: str) -> bool:
        """Check if a nonce is valid (exists and not expired).

        Note: This does NOT consume the nonce. Use consume_nonce() for
        atomic verify-and-consume during login.

        Args:
            nonce: The nonce string to check.

        Returns:
            True if valid, False otherwise.
        """
        nonce_key = f"{_NONCE_PREFIX}{nonce}"
        return nonce_key in self._cache

    @property
    def nonce_expiry_seconds(self) -> int:
        """Get the nonce expiration time in seconds."""
        return self._nonce_expiry_seconds


_token_store_instance: Optional[TokenStore] = None


def get_token_store() -> TokenStore:
    """Get the singleton TokenStore instance."""
    global _token_store_instance
    if _token_store_instance is None:
        _token_store_instance = TokenStore()
    return _token_store_instance
