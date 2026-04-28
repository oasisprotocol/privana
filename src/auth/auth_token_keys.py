"""Deoxys-II key management for AuthToken encryption, derived from ROFL TEE-bound seed."""

import logging
import os
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.clients.rofl import RoflAppdClient
from src.services.accounting_contract import get_accounting_contract_service

logger = logging.getLogger(__name__)

DEFAULT_AUTH_TOKEN_KEY_ID = "auth_token_enc_seed.key"


def _get_auth_token_key_id() -> str:
    """Get the auth token key ID, configurable via AUTH_TOKEN_KEY_ID env var."""
    return os.getenv("AUTH_TOKEN_KEY_ID", DEFAULT_AUTH_TOKEN_KEY_ID)


class AuthTokenKeyManager:
    """Manages Deoxys-II encryption key for AuthToken, derived from ROFL seed.

    The encryption key is derived deterministically from a ROFL-generated seed,
    ensuring the key is TEE-bound and consistent across restarts. This key is
    shared with the smart contract via setAuthTokenEncKey() during startup.
    """

    def __init__(self) -> None:
        self._enc_key: Optional[bytes] = None

    async def _get_rofl_seed(self) -> bytes:
        """Get deterministic seed from ROFL daemon.

        Returns:
            32-byte seed derived from ROFL key generation.
        """
        client = RoflAppdClient()
        key_id = _get_auth_token_key_id()
        logger.info("Using auth token key ID: %s", key_id)
        # generate_key returns a hex string of 32 bytes (64 chars)
        seed_hex = await client._client.generate_key(key_id)
        return bytes.fromhex(seed_hex)

    def _derive_enc_key(self, seed: bytes) -> bytes:
        """Derive 32-byte Deoxys-II encryption key from ROFL seed.

        Args:
            seed: 32-byte seed from ROFL.

        Returns:
            32-byte encryption key.
        """
        # Use HKDF to derive 32 bytes of key material from the seed
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"flexvaults-auth-token-enc-key",
            info=b"deoxysii-256-128",
        )
        return hkdf.derive(seed)

    async def initialize(self, use_rofl: bool = True) -> None:
        """Initialize the key manager by generating or loading the encryption key.

        Args:
            use_rofl: If True, derive key from ROFL seed. If False, use deterministic test key.
        """
        if self._enc_key is not None:
            logger.debug("AuthTokenKeyManager already initialized")
            return

        # TODO: Remove DISABLE_ROFL_KEYS fallback when Sapphire localnet e2e tests are available.
        # This allows running tests without a ROFL environment.
        if use_rofl and not os.getenv("DISABLE_ROFL_KEYS"):
            try:
                logger.info("Deriving AuthToken encryption key from ROFL seed...")
                seed = await self._get_rofl_seed()
                self._enc_key = self._derive_enc_key(seed)
                logger.info(
                    "AuthToken encryption key derived from ROFL seed (TEE-bound, deterministic)"
                )
            except Exception as e:
                raise RuntimeError(
                    "Failed to derive AuthToken encryption key from ROFL seed. "
                    "Set DISABLE_ROFL_KEYS=1 for non-TEE mode."
                ) from e
        else:
            # Deterministic key for testing (matches contract's test mode key)
            # The contract uses bytes32(uint256(1)) for non-Sapphire chains
            logger.info("Using deterministic test key for AuthToken encryption (non-TEE mode)")
            self._enc_key = (1).to_bytes(32, "big")

    @property
    def enc_key(self) -> bytes:
        """Get the 32-byte Deoxys-II encryption key."""
        if self._enc_key is None:
            raise RuntimeError(
                "AuthTokenKeyManager not initialized. Call await initialize() first."
            )
        return self._enc_key

    @property
    def enc_key_bytes32(self) -> bytes:
        """Get the encryption key as bytes32 for contract interaction."""
        return self.enc_key

    async def sync_key_to_contract(self) -> None:
        """Sync the encryption key to the contract via ROFL-authenticated call.

        This should be called during startup after the key is initialized.
        The contract will verify the ROFL origin before accepting the key.
        """
        service = get_accounting_contract_service()
        await service.set_auth_token_enc_key(self.enc_key)
        logger.info("AuthToken encryption key synced to contract successfully")


_auth_token_key_manager_instance: Optional[AuthTokenKeyManager] = None


def get_auth_token_key_manager() -> AuthTokenKeyManager:
    """Get the singleton AuthTokenKeyManager instance."""
    global _auth_token_key_manager_instance
    if _auth_token_key_manager_instance is None:
        _auth_token_key_manager_instance = AuthTokenKeyManager()
    return _auth_token_key_manager_instance
