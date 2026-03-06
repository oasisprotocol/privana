"""Tests for JWT key manager."""

import base64

import pytest
from cryptography.hazmat.primitives import serialization

import src.auth.jwt_keys as jwt_keys
from src.auth.jwt_keys import JWTKeyManager, get_jwt_key_manager


class TestJWTKeyManagerInitialization:
    """Tests for JWTKeyManager initialization."""

    def test_fails_when_rofl_unavailable(self, reset_auth_singletons, monkeypatch):
        """Test that initialization fails when ROFL seed is unavailable."""
        manager = JWTKeyManager()
        monkeypatch.delenv("DISABLE_ROFL_KEYS", raising=False)

        def _raise_missing_seed():
            raise FileNotFoundError("rofl unavailable")

        monkeypatch.setattr(manager, "_get_rofl_seed", _raise_missing_seed)

        with pytest.raises(RuntimeError, match="Failed to derive JWT signing key from ROFL seed"):
            manager.initialize()

    def test_uses_random_key_when_rofl_disabled(self, reset_auth_singletons, disable_rofl_keys):
        """Test that random key is used when ROFL is disabled."""
        manager = JWTKeyManager()
        manager.initialize()
        assert manager.private_key is not None

    def test_singleton_pattern(self, reset_auth_singletons, disable_rofl_keys):
        """Test that get_jwt_key_manager follows singleton pattern."""
        manager1 = get_jwt_key_manager()
        manager2 = get_jwt_key_manager()
        assert manager1 is manager2


class TestJWTKeyDerivation:
    """Tests for deterministic key derivation."""

    def test_deterministic_key_derivation(self, reset_auth_singletons, disable_rofl_keys):
        """Test that same seed produces same key pair."""
        seed = b"test-seed-exactly-32-bytes-long!"
        assert len(seed) == 32

        jwt_keys._jwt_key_manager_instance = None
        manager1 = JWTKeyManager()
        manager1._private_key = None
        key1 = manager1._derive_ed25519_keypair(seed)

        jwt_keys._jwt_key_manager_instance = None
        manager2 = JWTKeyManager()
        manager2._private_key = None
        key2 = manager2._derive_ed25519_keypair(seed)

        key1_bytes = key1.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key2_bytes = key2.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        assert key1_bytes == key2_bytes

    def test_different_seeds_produce_different_keys(self, reset_auth_singletons, disable_rofl_keys):
        """Test that different seeds produce different key pairs."""
        seed1 = b"test-seed-exactly-32-bytes-one!!"
        seed2 = b"test-seed-exactly-32-bytes-two!!"
        assert len(seed1) == 32
        assert len(seed2) == 32

        manager = JWTKeyManager()

        key1 = manager._derive_ed25519_keypair(seed1)
        key2 = manager._derive_ed25519_keypair(seed2)

        key1_bytes = key1.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key2_bytes = key2.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        assert key1_bytes != key2_bytes


class TestJWKS:
    """Tests for JWKS generation."""

    def test_jwks_format_is_valid(self, reset_auth_singletons, disable_rofl_keys):
        """Test that JWKS output has correct structure."""
        manager = JWTKeyManager()
        manager.initialize()

        jwks = manager.get_jwks()

        assert "keys" in jwks
        assert len(jwks["keys"]) == 1

        key = jwks["keys"][0]
        assert key["kty"] == "OKP"  # Octet Key Pair for Ed25519
        assert key["crv"] == "Ed25519"
        assert key["use"] == "sig"
        assert key["alg"] == "EdDSA"
        assert "kid" in key
        assert "x" in key  # Public key component

        # Verify x is valid base64url (no padding)
        x_bytes = base64.urlsafe_b64decode(key["x"] + "==")
        assert len(x_bytes) == 32  # Ed25519 public key is 32 bytes


class TestKeyFormat:
    """Tests for key format and properties."""

    def test_pem_format(self, reset_auth_singletons, disable_rofl_keys):
        """Test that PEM format keys are valid."""
        manager = JWTKeyManager()
        manager.initialize()

        private_pem = manager.get_private_key_pem()
        public_pem = manager.get_public_key_pem()

        assert private_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert private_pem.endswith(b"-----END PRIVATE KEY-----\n")
        assert public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")
        assert public_pem.endswith(b"-----END PUBLIC KEY-----\n")

    def test_algorithm_is_eddsa(self, reset_auth_singletons, disable_rofl_keys):
        """Test that algorithm is EdDSA."""
        manager = JWTKeyManager()
        manager.initialize()
        assert manager.algorithm == "EdDSA"

    def test_key_id_is_consistent(self, reset_auth_singletons, disable_rofl_keys):
        """Test that key ID is consistent across accesses."""
        manager = JWTKeyManager()
        manager.initialize()

        kid1 = manager.key_id
        kid2 = manager.key_id

        assert kid1 == kid2
        assert kid1 == "rofl-jwt-ed25519-1"
