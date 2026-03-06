"""Tests for AuthToken service."""

import os
import time

import pytest
from eth_abi import decode

from src.auth.auth_token_keys import AuthTokenKeyManager, get_auth_token_key_manager
from src.auth.auth_token_service import (
    ZERO_NONCE,
    AuthToken,
    AuthTokenService,
    get_auth_token_service,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singleton instances before each test."""
    # Reset AuthTokenKeyManager
    AuthTokenKeyManager._instance = None

    # Reset AuthTokenService
    AuthTokenService._instance = None

    # Ensure test mode (no ROFL)
    os.environ["DISABLE_ROFL_KEYS"] = "1"

    yield

    # Cleanup
    if "DISABLE_ROFL_KEYS" in os.environ:
        del os.environ["DISABLE_ROFL_KEYS"]


class TestAuthToken:
    """Tests for AuthToken dataclass."""

    def test_to_abi_encoded(self):
        """Should ABI-encode the token correctly."""
        token = AuthToken(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="Sign in to example.com",
            resources=["https://example.com/api"],
        )

        encoded = token.to_abi_encoded()

        # Decode and verify
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            encoded,
        )

        assert decoded[0] == "https://example.com"
        # eth_abi returns addresses as checksummed hex strings
        assert decoded[1].lower() == "0x1234567890123456789012345678901234567890"
        assert decoded[2] == 1700000000
        assert decoded[3] == "Sign in to example.com"
        assert decoded[4] == ("https://example.com/api",)

    def test_to_abi_encoded_empty_resources(self):
        """Should handle empty resources list."""
        token = AuthToken(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="",
            resources=[],
        )

        encoded = token.to_abi_encoded()
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            encoded,
        )

        assert decoded[4] == ()  # Empty tuple

    def test_address_checksum(self):
        """Should handle both checksummed and non-checksummed addresses."""
        # Non-checksummed input
        token = AuthToken(
            domain="https://example.com",
            user_addr="0xabcdef1234567890abcdef1234567890abcdef12",
            valid_until=1700000000,
            statement="",
            resources=[],
        )

        # Should encode without error
        encoded = token.to_abi_encoded()
        assert len(encoded) > 0


class TestAuthTokenKeyManager:
    """Tests for AuthTokenKeyManager."""

    def test_singleton(self):
        """Should return the same instance."""
        manager1 = get_auth_token_key_manager()
        manager2 = get_auth_token_key_manager()
        assert manager1 is manager2

    def test_initialize_test_mode(self):
        """Should initialize with deterministic test key."""
        manager = get_auth_token_key_manager()
        manager.initialize(use_rofl=False)

        # Test key should be bytes32(uint256(1))
        expected_key = (1).to_bytes(32, "big")
        assert manager.enc_key == expected_key

    def test_enc_key_auto_initializes(self):
        """Should auto-initialize when accessing enc_key."""
        manager = get_auth_token_key_manager()
        # Access enc_key without explicit initialize
        key = manager.enc_key
        assert len(key) == 32

    def test_enc_key_bytes32(self):
        """Should return key as bytes32."""
        manager = get_auth_token_key_manager()
        manager.initialize(use_rofl=False)

        key = manager.enc_key_bytes32
        assert len(key) == 32
        assert isinstance(key, bytes)


class TestAuthTokenService:
    """Tests for AuthTokenService."""

    def test_singleton(self):
        """Should return the same instance."""
        service1 = get_auth_token_service()
        service2 = get_auth_token_service()
        assert service1 is service2

    def test_create_auth_token(self):
        """Should create an AuthToken with correct fields."""
        service = get_auth_token_service()

        token = service.create_auth_token(
            domain="https://example.com",
            user_addr="0xabcdef1234567890abcdef1234567890abcdef12",
            valid_until=1700000000,
            statement="Sign in",
            resources=["https://api.example.com"],
        )

        assert token.domain == "https://example.com"
        # Address should be checksummed - use lower case comparison since checksums vary
        assert token.user_addr.lower() == "0xabcdef1234567890abcdef1234567890abcdef12"
        assert token.valid_until == 1700000000
        assert token.statement == "Sign in"
        assert token.resources == ["https://api.example.com"]

    def test_create_auth_token_default_values(self):
        """Should use default values for optional fields."""
        service = get_auth_token_service()

        token = service.create_auth_token(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
        )

        assert token.statement == ""
        assert token.resources == []

    def test_encrypt_auth_token(self):
        """Should encrypt the token."""
        service = get_auth_token_service()
        service.initialize()

        token = service.create_auth_token(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="Test",
            resources=[],
        )

        ciphertext = service.encrypt_auth_token(token)

        # Ciphertext should be encoded length + 16 bytes tag
        encoded = token.to_abi_encoded()
        assert len(ciphertext) == len(encoded) + 16

    def test_create_and_encrypt(self):
        """Should create and encrypt in one step."""
        service = get_auth_token_service()

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="Test",
            resources=["https://api.example.com"],
        )

        assert len(ciphertext) > 16  # At least tag size

    def test_encrypted_token_can_be_decrypted(self):
        """Should be able to decrypt the token with the same key."""
        service = get_auth_token_service()
        service.initialize()

        token = service.create_auth_token(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="Sign in to example",
            resources=["https://api.example.com/v1"],
        )

        ciphertext = service.encrypt_auth_token(token)

        # Decrypt with the same AEAD
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")

        # Should match the original encoding
        assert decrypted == token.to_abi_encoded()

        # Verify the decoded values
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[0] == token.domain
        assert decoded[2] == token.valid_until
        assert decoded[3] == token.statement


class TestAuthTokenServiceWithTestKey:
    """Tests verifying compatibility with contract's test key."""

    def test_test_key_matches_contract(self):
        """Test key should match contract's non-Sapphire test key.

        The contract uses bytes32(uint256(1)) for non-Sapphire chains.
        """
        key_manager = get_auth_token_key_manager()
        key_manager.initialize(use_rofl=False)

        # Contract's test key: bytes32(uint256(1))
        # In Solidity, this is 31 zero bytes followed by 0x01
        expected = (1).to_bytes(32, "big")
        assert key_manager.enc_key == expected

    def test_token_format_matches_contract(self):
        """Verify encrypted token format matches what contract expects.

        The contract decrypts using:
        Sapphire.decrypt(_authTokenEncKey, 0, token, "")

        Then decodes:
        AuthToken memory b = abi.decode(authTokenEncoded, (AuthToken));
        """
        service = get_auth_token_service()

        # Create a token similar to what the contract generates
        valid_until = int(time.time()) + 86400  # 24 hours

        ciphertext = service.create_and_encrypt(
            domain="https://flexvaults.com",
            user_addr="0xdead000000000000000000000000000000000000",
            valid_until=valid_until,
            statement="Sign in with Ethereum to flexvaults.com",
            resources=[],
        )

        # Verify it can be decrypted with the same parameters
        aead = service.aead
        decrypted = aead.decrypt(ZERO_NONCE, ciphertext, b"")

        # Decode the struct
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[0] == "https://flexvaults.com"
        assert decoded[2] == valid_until


class TestAuthTokenEdgeCases:
    """Tests for AuthToken field edge cases."""

    def test_handles_maximum_length_domain(self):
        """Should handle very long domain names."""
        service = get_auth_token_service()

        # Create a domain near practical limits (253 chars is max for DNS)
        long_domain = "https://" + "a" * 200 + ".example.com"

        ciphertext = service.create_and_encrypt(
            domain=long_domain,
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="",
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[0] == long_domain

    def test_handles_many_resources(self):
        """Should handle many resource URIs."""
        service = get_auth_token_service()

        # Create 1000 resource URIs
        resources = [f"https://api.example.com/resource/{i}" for i in range(1000)]

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="",
            resources=resources,
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert len(decoded[4]) == 1000
        assert decoded[4][0] == "https://api.example.com/resource/0"
        assert decoded[4][999] == "https://api.example.com/resource/999"

    def test_handles_very_large_valid_until(self):
        """Should handle very large validUntil timestamps."""
        service = get_auth_token_service()

        # Year 3000 timestamp (far future)
        far_future = 32503680000

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=far_future,
            statement="",
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[2] == far_future

    def test_handles_zero_address(self):
        """Should handle zero address (though unusual)."""
        service = get_auth_token_service()

        zero_addr = "0x0000000000000000000000000000000000000000"

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr=zero_addr,
            valid_until=1700000000,
            statement="",
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        # eth_abi returns addresses as bytes, so convert for comparison
        assert decoded[1] == zero_addr

    def test_handles_empty_statement(self):
        """Should handle empty statement string."""
        service = get_auth_token_service()

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="",  # Empty statement
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[3] == ""

    def test_handles_unicode_in_statement(self):
        """Should handle unicode characters in statement."""
        service = get_auth_token_service()

        # Unicode statement with various scripts
        unicode_statement = "Sign in 你好 مرحبا 🔐🔑 こんにちは"

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement=unicode_statement,
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[3] == unicode_statement

    def test_handles_unicode_in_resources(self):
        """Should handle unicode characters in resource URIs."""
        service = get_auth_token_service()

        # Resources with unicode (encoded URIs)
        resources = [
            "https://example.com/api/用户",
            "https://example.com/api/🎮",
        ]

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="",
            resources=resources,
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[4][0] == "https://example.com/api/用户"
        assert decoded[4][1] == "https://example.com/api/🎮"

    def test_handles_long_statement(self):
        """Should handle very long statement strings."""
        service = get_auth_token_service()

        # Create a very long statement (10KB)
        long_statement = "A" * 10000

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement=long_statement,
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[3] == long_statement
        assert len(decoded[3]) == 10000

    def test_handles_special_characters_in_domain(self):
        """Should handle special characters in domain."""
        service = get_auth_token_service()

        # Domain with port and path-like structure
        domain = "https://api.example.com:8443"

        ciphertext = service.create_and_encrypt(
            domain=domain,
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=1700000000,
            statement="",
            resources=[],
        )

        # Verify decryption works
        decrypted = service.aead.decrypt(ZERO_NONCE, ciphertext, b"")
        decoded = decode(
            ["string", "address", "uint256", "string", "string[]"],
            decrypted,
        )

        assert decoded[0] == domain

    def test_consistent_encoding_across_calls(self):
        """Should produce consistent encoding for same inputs."""
        service = get_auth_token_service()

        # Same parameters
        params = {
            "domain": "https://example.com",
            "user_addr": "0x1234567890123456789012345678901234567890",
            "valid_until": 1700000000,
            "statement": "Test",
            "resources": ["https://api.example.com"],
        }

        # Encrypt multiple times
        ct1 = service.create_and_encrypt(**params)
        ct2 = service.create_and_encrypt(**params)

        # Same inputs should produce same ciphertext (deterministic encryption with zero nonce)
        assert ct1 == ct2
