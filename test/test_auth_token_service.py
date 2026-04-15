"""Tests for AuthToken service."""

import os
import time

import pytest
from eth_abi import decode

from src.auth.auth_token_keys import get_auth_token_key_manager
from src.auth.auth_token_service import (
    ZERO_NONCE,
    AuthToken,
    get_auth_token_service,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singleton instances before each test."""
    import src.auth.auth_token_keys as auth_token_keys
    import src.auth.auth_token_service as auth_token_service_module

    # Reset AuthTokenKeyManager
    auth_token_keys._auth_token_key_manager_instance = None

    # Reset AuthTokenService
    auth_token_service_module._auth_token_service_instance = None

    # Ensure test mode (no ROFL)
    os.environ["DISABLE_ROFL_KEYS"] = "1"

    yield

    # Cleanup
    auth_token_keys._auth_token_key_manager_instance = None
    auth_token_service_module._auth_token_service_instance = None
    if "DISABLE_ROFL_KEYS" in os.environ:
        del os.environ["DISABLE_ROFL_KEYS"]


@pytest.fixture
async def initialized_key_manager():
    """Initialize the key manager for tests that need it."""
    manager = get_auth_token_key_manager()
    await manager.initialize(use_rofl=False)
    return manager


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

        # Decode and verify (tuple encoding for Solidity struct)
        decoded = decode(
            ["(string,address,uint256,string,string[])"],
            encoded,
        )

        assert decoded[0][0] == "https://example.com"
        # eth_abi returns addresses as checksummed hex strings
        assert decoded[0][1].lower() == "0x1234567890123456789012345678901234567890"
        assert decoded[0][2] == 1700000000
        assert decoded[0][3] == "Sign in to example.com"
        assert decoded[0][4] == ("https://example.com/api",)

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
            ["(string,address,uint256,string,string[])"],
            encoded,
        )

        assert decoded[0][4] == ()  # Empty tuple

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

    @pytest.mark.asyncio
    async def test_initialize_test_mode(self):
        """Should initialize with deterministic test key."""
        manager = get_auth_token_key_manager()
        await manager.initialize(use_rofl=False)

        # Test key should be bytes32(uint256(1))
        expected_key = (1).to_bytes(32, "big")
        assert manager.enc_key == expected_key

    def test_enc_key_raises_when_not_initialized(self):
        """Should raise error when accessing enc_key without initialization."""
        manager = get_auth_token_key_manager()
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = manager.enc_key

    @pytest.mark.asyncio
    async def test_enc_key_bytes32(self):
        """Should return key as bytes32."""
        manager = get_auth_token_key_manager()
        await manager.initialize(use_rofl=False)

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

    @pytest.mark.asyncio
    async def test_encrypt_auth_token(self):
        """Should encrypt the token."""
        # Initialize key manager first (required for service)
        key_manager = get_auth_token_key_manager()
        await key_manager.initialize(use_rofl=False)

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

    @pytest.mark.asyncio
    async def test_create_and_encrypt(self, initialized_key_manager):
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

    @pytest.mark.asyncio
    async def test_encrypted_token_can_be_decrypted(self):
        """Should be able to decrypt the token with the same key."""
        # Initialize key manager first (required for service)
        key_manager = get_auth_token_key_manager()
        await key_manager.initialize(use_rofl=False)

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

        # Verify the decoded values (tuple encoding for Solidity struct)
        decoded = decode(
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][0] == token.domain
        assert decoded[0][2] == token.valid_until
        assert decoded[0][3] == token.statement

    @pytest.mark.asyncio
    async def test_decrypt_auth_token_returns_auth_token(self, initialized_key_manager):
        """Should decode encrypted tokens back into AuthToken instances."""
        service = get_auth_token_service()
        valid_until = int(time.time()) + 600

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=valid_until,
            statement="Sign in to example",
            resources=["https://api.example.com/v1"],
        )

        token = service.decrypt_auth_token(ciphertext)

        assert token.domain == "https://example.com"
        assert token.user_addr == "0x1234567890123456789012345678901234567890"
        assert token.valid_until == valid_until
        assert token.statement == "Sign in to example"
        assert token.resources == ["https://api.example.com/v1"]

    @pytest.mark.asyncio
    async def test_decrypt_auth_token_rejects_expired_tokens(self, initialized_key_manager):
        """Should reject tokens whose validity window has elapsed."""
        service = get_auth_token_service()

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=int(time.time()) - 1,
        )

        with pytest.raises(ValueError, match="expired"):
            service.decrypt_auth_token(ciphertext)

    @pytest.mark.asyncio
    async def test_decode_auth_token_can_skip_expiry_validation(self, initialized_key_manager):
        """Should decode expired tokens when expiry validation is explicitly skipped."""
        service = get_auth_token_service()

        ciphertext = service.create_and_encrypt(
            domain="https://example.com",
            user_addr="0x1234567890123456789012345678901234567890",
            valid_until=int(time.time()) - 1,
        )

        token = service.decode_auth_token(ciphertext, validate_expiry=False)

        assert token.user_addr == "0x1234567890123456789012345678901234567890"


class TestAuthTokenServiceWithTestKey:
    """Tests verifying compatibility with contract's test key."""

    @pytest.mark.asyncio
    async def test_test_key_matches_contract(self):
        """Test key should match contract's non-Sapphire test key.

        The contract uses bytes32(uint256(1)) for non-Sapphire chains.
        """
        key_manager = get_auth_token_key_manager()
        await key_manager.initialize(use_rofl=False)

        # Contract's test key: bytes32(uint256(1))
        # In Solidity, this is 31 zero bytes followed by 0x01
        expected = (1).to_bytes(32, "big")
        assert key_manager.enc_key == expected

    @pytest.mark.asyncio
    async def test_token_format_matches_contract(self, initialized_key_manager):
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

        # Decode the struct (tuple encoding for Solidity struct)
        decoded = decode(
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][0] == "https://flexvaults.com"
        assert decoded[0][2] == valid_until


class TestAuthTokenEdgeCases:
    """Tests for AuthToken field edge cases."""

    @pytest.mark.asyncio
    async def test_handles_maximum_length_domain(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][0] == long_domain

    @pytest.mark.asyncio
    async def test_handles_many_resources(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert len(decoded[0][4]) == 1000
        assert decoded[0][4][0] == "https://api.example.com/resource/0"
        assert decoded[0][4][999] == "https://api.example.com/resource/999"

    @pytest.mark.asyncio
    async def test_handles_very_large_valid_until(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][2] == far_future

    @pytest.mark.asyncio
    async def test_handles_zero_address(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        # eth_abi returns addresses as bytes, so convert for comparison
        assert decoded[0][1] == zero_addr

    @pytest.mark.asyncio
    async def test_handles_empty_statement(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][3] == ""

    @pytest.mark.asyncio
    async def test_handles_unicode_in_statement(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][3] == unicode_statement

    @pytest.mark.asyncio
    async def test_handles_unicode_in_resources(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][4][0] == "https://example.com/api/用户"
        assert decoded[0][4][1] == "https://example.com/api/🎮"

    @pytest.mark.asyncio
    async def test_handles_long_statement(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][3] == long_statement
        assert len(decoded[0][3]) == 10000

    @pytest.mark.asyncio
    async def test_handles_special_characters_in_domain(self, initialized_key_manager):
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
            ["(string,address,uint256,string,string[])"],
            decrypted,
        )

        assert decoded[0][0] == domain

    @pytest.mark.asyncio
    async def test_consistent_encoding_across_calls(self, initialized_key_manager):
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
