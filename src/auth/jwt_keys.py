"""Ed25519 key management for JWT signing, derived from ROFL TEE-bound seed."""

import base64
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

JWT_SIGNING_KEY_ID = "jwt_signing_seed.key"


class JWTKeyManager:
    """Manages Ed25519 keypair for JWT signing, derived from ROFL seed.

    The private key is derived deterministically from a ROFL-generated seed,
    ensuring the key is TEE-bound and consistent across restarts.
    """

    def __init__(self) -> None:
        self._private_key: Optional[Ed25519PrivateKey] = None
        self._public_key: Optional[Ed25519PublicKey] = None
        self._key_id: str = "rofl-jwt-ed25519-1"

    def _get_rofl_seed(self) -> bytes:
        """Get deterministic seed from ROFL daemon.

        Returns:
            32-byte seed derived from ROFL key generation.
        """
        from src.clients.rofl import RoflAppdClient

        client = RoflAppdClient()
        # generate_key returns a hex string of 32 bytes (64 chars)
        seed_hex = client._client.generate_key(JWT_SIGNING_KEY_ID)
        return bytes.fromhex(seed_hex)

    def _derive_ed25519_keypair(self, seed: bytes) -> Ed25519PrivateKey:
        """Derive Ed25519 keypair deterministically from ROFL seed.

        Args:
            seed: 32-byte seed from ROFL.

        Returns:
            Ed25519 private key.
        """
        # Use HKDF to derive 32 bytes of key material from the seed
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"flexvaults-jwt-signing-key",
            info=b"ed25519-private-key",
        )
        key_material = hkdf.derive(seed)

        # Create Ed25519 private key from the derived bytes
        private_key = Ed25519PrivateKey.from_private_bytes(key_material)

        return private_key

    def initialize(self, use_rofl: bool = True) -> None:
        """Initialize the key manager by generating or loading keys.

        Args:
            use_rofl: If True, derive keys from ROFL seed. If False, generate random keys.
        """
        if self._private_key is not None:
            logger.debug("JWTKeyManager already initialized")
            return

        # TODO: Remove DISABLE_ROFL_KEYS fallback when Sapphire localnet e2e tests are available.
        # This allows running tests without a ROFL environment.
        if use_rofl and not os.getenv("DISABLE_ROFL_KEYS"):
            try:
                logger.info("Deriving Ed25519 JWT signing key from ROFL seed...")
                seed = self._get_rofl_seed()
                self._private_key = self._derive_ed25519_keypair(seed)
                logger.info(
                    "Ed25519 JWT signing key derived from ROFL seed (TEE-bound, deterministic)"
                )
            except Exception as e:
                raise RuntimeError(
                    "Failed to derive JWT signing key from ROFL seed. "
                    "Set DISABLE_ROFL_KEYS=1 for non-TEE mode."
                ) from e
        else:
            logger.info("Generating random Ed25519 JWT signing key (non-TEE mode)")
            self._private_key = Ed25519PrivateKey.generate()

        self._public_key = self._private_key.public_key()
        logger.info(f"JWT key manager initialized with key ID: {self._key_id} (algorithm: EdDSA)")

    @property
    def private_key(self) -> Ed25519PrivateKey:
        """Get the Ed25519 private key for signing JWTs."""
        if self._private_key is None:
            self.initialize()
        assert self._private_key is not None
        return self._private_key

    @property
    def public_key(self) -> Ed25519PublicKey:
        """Get the Ed25519 public key for verifying JWTs."""
        if self._public_key is None:
            self.initialize()
        assert self._public_key is not None
        return self._public_key

    @property
    def key_id(self) -> str:
        """Get the key ID (kid) for JWKS."""
        return self._key_id

    @property
    def algorithm(self) -> str:
        """Get the JWT algorithm identifier."""
        return "EdDSA"

    def get_private_key_pem(self) -> bytes:
        """Get the private key in PEM format."""
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def get_public_key_pem(self) -> bytes:
        """Get the public key in PEM format."""
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def get_jwks(self) -> dict:
        """Get the public key in JWKS format for external verification.

        Returns:
            JWKS dict with the public key in OKP (Octet Key Pair) format.
        """
        # Get the raw public key bytes (32 bytes for Ed25519)
        public_key_bytes = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        # Base64url encode without padding
        x = base64.urlsafe_b64encode(public_key_bytes).rstrip(b"=").decode("ascii")

        return {
            "keys": [
                {
                    "kty": "OKP",  # Octet Key Pair
                    "crv": "Ed25519",  # Curve
                    "kid": self._key_id,
                    "use": "sig",
                    "alg": "EdDSA",
                    "x": x,  # Public key (base64url)
                }
            ]
        }


_jwt_key_manager_instance: Optional[JWTKeyManager] = None


def get_jwt_key_manager() -> JWTKeyManager:
    """Get the singleton JWTKeyManager instance."""
    global _jwt_key_manager_instance
    if _jwt_key_manager_instance is None:
        _jwt_key_manager_instance = JWTKeyManager()
    return _jwt_key_manager_instance
