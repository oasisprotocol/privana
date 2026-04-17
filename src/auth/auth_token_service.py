"""AuthToken generation and encryption service.

This service generates and encrypts AuthTokens using Deoxys-II,
matching the format expected by the Sapphire contract.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from eth_abi import decode, encode
from web3 import Web3

from src.crypto.deoxysii import AEAD, NONCE_SIZE, DecryptionError

logger = logging.getLogger(__name__)

# Zero nonce (15 bytes) - matches contract's Sapphire.encrypt(key, 0, data, "")
ZERO_NONCE = b"\x00" * NONCE_SIZE

_AUTH_TOKEN_ABI_SCHEMA = "(string,address,uint256,string,string[])"


@dataclass
class AuthToken:
    """AuthToken structure matching Solidity contract.

    struct AuthToken {
        string domain;
        address userAddr;
        uint256 validUntil;
        string statement;
        string[] resources;
    }
    """

    domain: str
    user_addr: str  # Checksummed Ethereum address
    valid_until: int  # Unix timestamp
    statement: str
    resources: list[str]

    def to_abi_encoded(self) -> bytes:
        """Encode the AuthToken as a Solidity struct tuple."""
        user_addr_bytes = Web3.to_bytes(hexstr=self.user_addr)
        return encode(
            [_AUTH_TOKEN_ABI_SCHEMA],
            [(self.domain, user_addr_bytes, self.valid_until, self.statement, self.resources)],
        )


class AuthTokenService:
    """Service for generating and encrypting AuthTokens.

    This service creates AuthTokens that can be decrypted by the
    Sapphire contract using the shared encryption key.
    """

    def __init__(self) -> None:
        self._aead: Optional[AEAD] = None

    def initialize(self) -> None:
        """Initialize the service with the encryption key.

        Note: AuthTokenKeyManager must be initialized first via await initialize().
        """
        if self._aead is not None:
            logger.debug("AuthTokenService already initialized")
            return

        from src.auth.auth_token_keys import get_auth_token_key_manager

        key_manager = get_auth_token_key_manager()
        # key_manager should already be initialized by main.py lifespan
        enc_key = key_manager.enc_key
        self._aead = AEAD(enc_key)
        logger.info(
            "AuthTokenService initialized with key prefix: %s...",
            enc_key[:4].hex(),
        )

    @property
    def aead(self) -> AEAD:
        """Get the AEAD instance for encryption."""
        if self._aead is None:
            self.initialize()
        if self._aead is None:
            raise RuntimeError("AuthTokenService not initialized.")
        return self._aead

    def create_auth_token(
        self,
        domain: str,
        user_addr: str,
        valid_until: int,
        statement: str = "",
        resources: Optional[list[str]] = None,
    ) -> AuthToken:
        """Create an AuthToken.

        Args:
            domain: The SIWE domain (e.g., "https://api.example.com")
            user_addr: User's Ethereum address (will be checksummed)
            valid_until: Token expiration as Unix timestamp
            statement: Human-readable statement from SIWE message
            resources: List of resource URIs from SIWE message

        Returns:
            AuthToken instance.
        """
        return AuthToken(
            domain=domain,
            user_addr=Web3.to_checksum_address(user_addr),
            valid_until=valid_until,
            statement=statement,
            resources=resources or [],
        )

    def encrypt_auth_token(self, token: AuthToken) -> bytes:
        """Encrypt an AuthToken using Deoxys-II.

        Args:
            token: The AuthToken to encrypt.

        Returns:
            Encrypted token bytes (ciphertext + 16-byte tag).
        """
        encoded = token.to_abi_encoded()
        # Use zero nonce to match contract's Sapphire.encrypt(key, 0, data, "")
        ciphertext = self.aead.encrypt(ZERO_NONCE, encoded, b"")
        return ciphertext

    def decode_auth_token(self, ciphertext: bytes, *, validate_expiry: bool = True) -> AuthToken:
        """Decrypt and decode an AuthToken.

        Raises:
            ValueError: If the token cannot be decrypted, decoded, or has expired.
        """
        try:
            encoded = self.aead.decrypt(ZERO_NONCE, ciphertext, b"")
            decoded = decode([_AUTH_TOKEN_ABI_SCHEMA], encoded)[0]
            token = AuthToken(
                domain=decoded[0],
                user_addr=Web3.to_checksum_address(decoded[1]),
                valid_until=int(decoded[2]),
                statement=decoded[3],
                resources=list(decoded[4]),
            )
        except (DecryptionError, ValueError, TypeError) as exc:
            raise ValueError("Invalid auth token") from exc

        if validate_expiry and token.valid_until < int(time.time()):
            raise ValueError("Auth token has expired")
        return token

    def decrypt_auth_token(self, ciphertext: bytes) -> AuthToken:
        """Decrypt, decode, and enforce AuthToken expiry."""
        return self.decode_auth_token(ciphertext, validate_expiry=True)

    def create_and_encrypt(
        self,
        domain: str,
        user_addr: str,
        valid_until: int,
        statement: str = "",
        resources: Optional[list[str]] = None,
    ) -> bytes:
        """Create and encrypt an AuthToken in one step.

        Args:
            domain: The SIWE domain
            user_addr: User's Ethereum address
            valid_until: Token expiration as Unix timestamp
            statement: Human-readable statement from SIWE message
            resources: List of resource URIs from SIWE message

        Returns:
            Encrypted token bytes.
        """
        token = self.create_auth_token(
            domain=domain,
            user_addr=user_addr,
            valid_until=valid_until,
            statement=statement,
            resources=resources,
        )
        return self.encrypt_auth_token(token)


_auth_token_service_instance: Optional[AuthTokenService] = None


def get_auth_token_service() -> AuthTokenService:
    """Get the singleton AuthTokenService instance."""
    global _auth_token_service_instance
    if _auth_token_service_instance is None:
        _auth_token_service_instance = AuthTokenService()
    return _auth_token_service_instance
