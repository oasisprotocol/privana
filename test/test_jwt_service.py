"""Tests for JWT service."""

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from web3 import Web3

from src.auth.jwt_service import (
    DEFAULT_JWT_AUDIENCE,
    DEFAULT_JWT_ISSUER,
    JWTService,
    get_jwt_service,
)

TEST_ADDRESS = "0x0000000000000000000000000000000000000001"


class TestAccessTokenCreation:
    """Tests for access token creation."""

    def test_creates_valid_access_token(self, reset_auth_singletons, disable_rofl_keys):
        """Test that access tokens have correct claims."""
        jwt_service = JWTService()
        token = jwt_service.create_token(TEST_ADDRESS)

        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == TEST_ADDRESS
        assert payload["type"] == "access"
        assert payload["iss"] == DEFAULT_JWT_ISSUER
        assert payload["aud"] == DEFAULT_JWT_AUDIENCE
        assert "iat" in payload
        assert "exp" in payload
        assert "nbf" in payload
        assert payload["nbf"] == payload["iat"]

    def test_normalizes_address(self, reset_auth_singletons, disable_rofl_keys):
        """Test that addresses are normalized to checksum format."""
        jwt_service = JWTService()

        lowercase_address = "0x0000000000000000000000000000000000000001"
        token = jwt_service.create_token(lowercase_address)

        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == Web3.to_checksum_address(lowercase_address)

    def test_contains_nbf_claim(self, reset_auth_singletons, disable_rofl_keys):
        """Test that JWTs contain the nbf (not before) claim."""
        jwt_service = JWTService()
        token = jwt_service.create_token(TEST_ADDRESS)

        payload = jwt.decode(token, options={"verify_signature": False})

        assert "nbf" in payload
        assert "iat" in payload
        assert payload["nbf"] == payload["iat"]

    def test_contains_aud_claim(self, reset_auth_singletons, disable_rofl_keys):
        """Test that JWTs contain the aud (audience) claim."""
        jwt_service = JWTService()
        token = jwt_service.create_token(TEST_ADDRESS)

        payload = jwt.decode(token, options={"verify_signature": False})

        assert "aud" in payload
        assert payload["aud"] == DEFAULT_JWT_AUDIENCE

    def test_token_contains_correct_header(self, reset_auth_singletons, disable_rofl_keys):
        """Test that JWT header contains correct algorithm and key ID."""
        jwt_service = JWTService()
        token = jwt_service.create_token(TEST_ADDRESS)

        header = jwt.get_unverified_header(token)

        assert header["alg"] == "EdDSA"
        assert header["kid"] == "rofl-jwt-ed25519-1"


class TestRefreshTokenCreation:
    """Tests for refresh token creation."""

    def test_creates_valid_refresh_token(self, reset_auth_singletons, disable_rofl_keys):
        """Test that refresh tokens have correct claims."""
        jwt_service = JWTService()
        token = jwt_service.create_refresh_token(TEST_ADDRESS)

        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["sub"] == TEST_ADDRESS
        assert payload["type"] == "refresh"
        assert payload["iss"] == DEFAULT_JWT_ISSUER
        assert payload["aud"] == DEFAULT_JWT_AUDIENCE
        assert "iat" in payload
        assert "exp" in payload
        assert "nbf" in payload
        assert "jti" in payload
        assert len(payload["jti"]) > 20  # Should be a secure random ID

    def test_is_created_and_stored(self, reset_auth_singletons, disable_rofl_keys):
        """Test that refresh tokens are created and stored properly."""
        jwt_service = JWTService()
        refresh_token = jwt_service.create_refresh_token(TEST_ADDRESS)

        assert refresh_token is not None
        assert len(refresh_token) > 0

        address = jwt_service.verify_refresh_token(refresh_token)
        assert address == TEST_ADDRESS


class TestTokenVerification:
    """Tests for token verification."""

    def test_verify_token_checks_issuer(self, reset_auth_singletons, disable_rofl_keys):
        """Test that token verification checks issuer."""
        jwt_service = JWTService()

        now = int(time.time())
        payload = {
            "sub": TEST_ADDRESS,
            "iss": "wrong-issuer",
            "aud": DEFAULT_JWT_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "type": "access",
        }
        bad_token = jwt.encode(
            payload,
            jwt_service._key_manager.get_private_key_pem(),
            algorithm="EdDSA",
        )

        with pytest.raises(jwt.InvalidIssuerError):
            jwt_service.verify_token(bad_token)

    def test_verify_token_checks_audience(self, reset_auth_singletons, disable_rofl_keys):
        """Test that token verification checks audience."""
        jwt_service = JWTService()

        now = int(time.time())
        payload = {
            "sub": TEST_ADDRESS,
            "iss": DEFAULT_JWT_ISSUER,
            "aud": "wrong-audience",
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "type": "access",
        }
        bad_token = jwt.encode(
            payload,
            jwt_service._key_manager.get_private_key_pem(),
            algorithm="EdDSA",
        )

        with pytest.raises(jwt.InvalidAudienceError):
            jwt_service.verify_token(bad_token)

    def test_verify_token_checks_expiration(self, reset_auth_singletons, disable_rofl_keys):
        """Test that expired tokens are rejected."""
        jwt_service = JWTService()

        now = int(time.time())
        payload = {
            "sub": TEST_ADDRESS,
            "iss": DEFAULT_JWT_ISSUER,
            "aud": DEFAULT_JWT_AUDIENCE,
            "iat": now - 7200,
            "nbf": now - 7200,
            "exp": now - 3600,  # Expired 1 hour ago
            "type": "access",
        }
        expired_token = jwt.encode(
            payload,
            jwt_service._key_manager.get_private_key_pem(),
            algorithm="EdDSA",
        )

        with pytest.raises(jwt.ExpiredSignatureError):
            jwt_service.verify_token(expired_token)

    def test_verification_fails_with_wrong_key(self, reset_auth_singletons, disable_rofl_keys):
        """Test that token verification fails with a different key."""
        jwt_service = JWTService()
        address = "0x1234567890123456789012345678901234567890"

        token = jwt_service.create_token(address)

        wrong_key = Ed25519PrivateKey.generate()
        wrong_public_pem = wrong_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(
                token,
                wrong_public_pem,
                algorithms=["EdDSA"],
                audience=DEFAULT_JWT_AUDIENCE,
                issuer=DEFAULT_JWT_ISSUER,
            )

    def test_verification_fails_with_tampered_token(self, reset_auth_singletons, disable_rofl_keys):
        """Test that tampered tokens fail verification."""
        jwt_service = JWTService()
        address = "0x1234567890123456789012345678901234567890"

        token = jwt_service.create_token(address)

        parts = token.split(".")
        payload_bytes = base64.urlsafe_b64decode(parts[1] + "==")
        payload = json.loads(payload_bytes)
        payload["sub"] = "0x0000000000000000000000000000000000000000"
        tampered_payload = (
            base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        )
        tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

        with pytest.raises(jwt.InvalidSignatureError):
            jwt_service.verify_token(tampered_token)


class TestAddressExtraction:
    """Tests for extracting address from tokens."""

    def test_get_address_from_token(self, reset_auth_singletons, disable_rofl_keys):
        """Test extracting address from token."""
        jwt_service = JWTService()
        token = jwt_service.create_token(TEST_ADDRESS)

        address = jwt_service.get_address_from_token(token)
        assert address == TEST_ADDRESS

    def test_get_address_rejects_missing_sub(self, reset_auth_singletons, disable_rofl_keys):
        """Test that tokens without sub claim are rejected."""
        jwt_service = JWTService()

        now = int(time.time())
        payload = {
            "iss": DEFAULT_JWT_ISSUER,
            "aud": DEFAULT_JWT_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "type": "access",
        }
        bad_token = jwt.encode(
            payload,
            jwt_service._key_manager.get_private_key_pem(),
            algorithm="EdDSA",
        )

        with pytest.raises(ValueError, match="Token missing 'sub' claim"):
            jwt_service.get_address_from_token(bad_token)


class TestTokenTypeSeparation:
    """Tests for access/refresh token type separation."""

    def test_access_token_cannot_be_used_as_refresh_token(
        self, reset_auth_singletons, disable_rofl_keys
    ):
        """Test that access tokens are rejected when used as refresh tokens."""
        jwt_service = JWTService()
        access_token = jwt_service.create_token(TEST_ADDRESS)

        with pytest.raises(ValueError, match="Expected refresh token"):
            jwt_service.verify_refresh_token(access_token)

    def test_refresh_token_cannot_be_used_as_access_token(
        self, reset_auth_singletons, disable_rofl_keys
    ):
        """Test that refresh tokens are rejected when used as access tokens."""
        jwt_service = JWTService()
        refresh_token = jwt_service.create_refresh_token(TEST_ADDRESS)

        with pytest.raises(ValueError, match="Expected access token"):
            jwt_service.get_address_from_token(refresh_token)


class TestRefreshTokenRotation:
    """Tests for refresh token rotation."""

    def test_rotation_works_correctly(self, reset_auth_singletons, disable_rofl_keys):
        """Test that refresh token rotation works correctly."""
        jwt_service = JWTService()
        refresh_token = jwt_service.create_refresh_token(TEST_ADDRESS)

        new_access_token, new_refresh_token = jwt_service.refresh_tokens(refresh_token)

        assert new_access_token is not None
        assert new_refresh_token is not None
        assert new_refresh_token != refresh_token

        # Old refresh token should be revoked
        with pytest.raises(ValueError, match="Refresh token has been revoked"):
            jwt_service.verify_refresh_token(refresh_token)

        # New refresh token should be valid
        address = jwt_service.verify_refresh_token(new_refresh_token)
        assert address == TEST_ADDRESS

    def test_returns_new_pair(self, reset_auth_singletons, disable_rofl_keys):
        """Test that refresh_tokens returns a new access/refresh token pair."""
        jwt_service = JWTService()
        original_refresh = jwt_service.create_refresh_token(TEST_ADDRESS)

        new_access, new_refresh = jwt_service.refresh_tokens(original_refresh)

        access_payload = jwt.decode(new_access, options={"verify_signature": False})
        assert access_payload["sub"] == TEST_ADDRESS
        assert access_payload["type"] == "access"

        refresh_payload = jwt.decode(new_refresh, options={"verify_signature": False})
        assert refresh_payload["sub"] == TEST_ADDRESS
        assert refresh_payload["type"] == "refresh"

        assert new_refresh != original_refresh


class TestRefreshTokenRevocation:
    """Tests for refresh token revocation."""

    def test_revocation(self, reset_auth_singletons, disable_rofl_keys):
        """Test that refresh tokens can be revoked."""
        jwt_service = JWTService()
        refresh_token = jwt_service.create_refresh_token(TEST_ADDRESS)

        result = jwt_service.revoke_refresh_token(refresh_token)
        assert result is True

        with pytest.raises(ValueError, match="Refresh token has been revoked"):
            jwt_service.verify_refresh_token(refresh_token)

    def test_revoke_all_for_address(self, reset_auth_singletons, disable_rofl_keys):
        """Test revoking all refresh tokens for an address."""
        jwt_service = JWTService()

        token1 = jwt_service.create_refresh_token(TEST_ADDRESS)
        token2 = jwt_service.create_refresh_token(TEST_ADDRESS)
        token3 = jwt_service.create_refresh_token(TEST_ADDRESS)

        # All should be valid
        assert jwt_service.verify_refresh_token(token1) == TEST_ADDRESS
        assert jwt_service.verify_refresh_token(token2) == TEST_ADDRESS
        assert jwt_service.verify_refresh_token(token3) == TEST_ADDRESS

        # Revoke all
        count = jwt_service.revoke_all_refresh_tokens(TEST_ADDRESS)
        assert count == 3

        # All should be revoked
        with pytest.raises(ValueError, match="Refresh token has been revoked"):
            jwt_service.verify_refresh_token(token1)
        with pytest.raises(ValueError, match="Refresh token has been revoked"):
            jwt_service.verify_refresh_token(token2)
        with pytest.raises(ValueError, match="Refresh token has been revoked"):
            jwt_service.verify_refresh_token(token3)


class TestConfiguration:
    """Tests for JWT service configuration."""

    def test_custom_expiry_from_env(self, reset_auth_singletons, monkeypatch):
        """Test that expiry can be configured via environment."""
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        monkeypatch.setenv("JWT_EXPIRY_HOURS", "24")
        monkeypatch.setenv("JWT_REFRESH_EXPIRY_DAYS", "30")

        jwt_service = JWTService()

        assert jwt_service.access_token_expiry_seconds == 24 * 3600
        assert jwt_service.refresh_token_expiry_seconds == 30 * 24 * 3600

    def test_custom_issuer_audience_from_env(
        self, reset_auth_singletons, disable_rofl_keys, monkeypatch
    ):
        """Test that issuer and audience can be configured via environment."""
        monkeypatch.setenv("JWT_ISSUER", "custom-issuer")
        monkeypatch.setenv("JWT_AUDIENCE", "custom-audience")

        jwt_service = JWTService()
        token = jwt_service.create_token(TEST_ADDRESS)

        payload = jwt.decode(token, options={"verify_signature": False})

        assert payload["iss"] == "custom-issuer"
        assert payload["aud"] == "custom-audience"

    def test_singleton_pattern(self, reset_auth_singletons, disable_rofl_keys):
        """Test that get_jwt_service follows singleton pattern."""
        service1 = get_jwt_service()
        service2 = get_jwt_service()

        assert service1 is service2


class TestExternalVerification:
    """Tests for external verification using JWKS."""

    def test_full_flow_create_and_verify_with_jwks(self, reset_auth_singletons, disable_rofl_keys):
        """Test full flow: create token, get JWKS, verify token externally."""
        jwt_service = JWTService()
        address = "0x1234567890123456789012345678901234567890"

        token = jwt_service.create_token(address)
        jwks = jwt_service.get_jwks()

        key_data = jwks["keys"][0]
        x_bytes = base64.urlsafe_b64decode(key_data["x"] + "==")
        public_key = Ed25519PublicKey.from_public_bytes(x_bytes)

        public_key_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        payload = jwt.decode(
            token,
            public_key_pem,
            algorithms=["EdDSA"],
            audience=DEFAULT_JWT_AUDIENCE,
            issuer=DEFAULT_JWT_ISSUER,
        )

        assert payload["sub"] == Web3.to_checksum_address(address)
        assert payload["type"] == "access"
        assert "iat" in payload
        assert "exp" in payload
        assert "nbf" in payload

    def test_external_verification_with_pyjwt(self, reset_auth_singletons, disable_rofl_keys):
        """Test that tokens can be verified using standard PyJWT with JWKS."""
        jwt_service = JWTService()

        access_token = jwt_service.create_token(TEST_ADDRESS)
        refresh_token = jwt_service.create_refresh_token(TEST_ADDRESS)

        public_key_pem = jwt_service._key_manager.get_public_key_pem()

        access_payload = jwt.decode(
            access_token,
            public_key_pem,
            algorithms=["EdDSA"],
            audience=DEFAULT_JWT_AUDIENCE,
            issuer=DEFAULT_JWT_ISSUER,
        )
        assert access_payload["sub"] == TEST_ADDRESS
        assert access_payload["type"] == "access"

        refresh_payload = jwt.decode(
            refresh_token,
            public_key_pem,
            algorithms=["EdDSA"],
            audience=DEFAULT_JWT_AUDIENCE,
            issuer=DEFAULT_JWT_ISSUER,
        )
        assert refresh_payload["sub"] == TEST_ADDRESS
        assert refresh_payload["type"] == "refresh"
        assert "jti" in refresh_payload
