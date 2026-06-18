"""Tests for auth API routes."""

import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from siwe import ExpiredMessage, InvalidSignature, MalformedSession, SiweMessage
from web3 import Web3

import src.api.routes as routes
import src.auth.auth_token_keys
import src.auth.auth_token_service
import src.auth.dependencies as auth_dependencies
import src.auth.jwt_keys
import src.auth.jwt_service
import src.auth.token_store
import src.config
from src.auth.jwt_service import JWTService
from src.auth.token_store import TokenStore
from src.config import load_settings
from src.models.accounting import SiweLoginRequest

TEST_ADDRESS = "0x0000000000000000000000000000000000000001"
OTHER_ADDRESS = "0x0000000000000000000000000000000000000002"

# Use the default auth token validity from Settings
AUTH_TOKEN_VALIDITY_SECONDS = load_settings().auth_token_validity_seconds

# Destination chain ID the server authenticates against (Sapphire).
SAPPHIRE_CHAIN_ID = load_settings().sapphire_chain_id


async def _init_key_managers():
    """Initialize auth key managers for testing."""
    auth_key_manager = src.auth.auth_token_keys.get_auth_token_key_manager()
    await auth_key_manager.initialize(use_rofl=False)
    jwt_key_manager = src.auth.jwt_keys.get_jwt_key_manager()
    await jwt_key_manager.initialize(use_rofl=False)


def _build_siwe_message(
    address: str,
    nonce: str,
    domain: str = "localhost:5173",
    issued_at: datetime | None = None,
    expiration_time: datetime | None = None,
    chain_id: int = 1,
    destination_chain_id: int | None = SAPPHIRE_CHAIN_ID,
) -> str:
    """Build a SIWE message for testing.

    Args:
        address: Ethereum address
        nonce: SIWE nonce
        domain: SIWE domain
        issued_at: When the message was issued (default: now)
        expiration_time: When the message expires (default: now + 24h)
        chain_id: Chain ID for the SIWE message (default: 1)
        destination_chain_id: Sapphire destination chain ID embedded in the
            statement. The server requires this and verifies it matches its
            configured ``sapphire_chain_id``. Pass ``None`` to omit it.
    """
    now = datetime.now(timezone.utc)
    if issued_at is None:
        issued_at = now
    if expiration_time is None:
        expiration_time = now + timedelta(seconds=AUTH_TOKEN_VALIDITY_SECONDS)

    if destination_chain_id is None:
        statement = "Sign in to Privana"
    else:
        statement = f"Sign in to Privana on chain {destination_chain_id}"

    return SiweMessage(
        domain=domain,
        address=address,
        uri=f"http://{domain}",
        version="1",
        chain_id=chain_id,
        issued_at=issued_at.isoformat().replace("+00:00", "Z"),
        expiration_time=expiration_time.isoformat().replace("+00:00", "Z"),
        nonce=nonce,
        statement=statement,
    ).prepare_message()


def _test_request(
    *,
    path: str,
    method: str = "POST",
    origin: str = "http://localhost:5173",
) -> Request:
    """Build a minimal FastAPI request object for direct route tests."""
    headers = []
    if origin:
        headers.append((b"origin", origin.encode("utf-8")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def _test_response() -> Response:
    """Build a response object for direct route tests."""
    return Response()


async def _call_siwe_login(payload):
    """Call the login route with explicit request/response objects."""
    return await routes.siwe_login(
        payload,
        _test_request(path="/v1/accounting/auth/login"),
        _test_response(),
    )


async def _call_get_siwe_nonce(address: str):
    """Call the nonce route with explicit request/response objects."""
    return await routes.get_siwe_nonce(
        address,
        _test_request(path="/v1/accounting/auth/nonce", method="GET"),
        _test_response(),
    )


async def _call_refresh(payload):
    """Call the refresh route with an explicit response object."""
    return await routes.refresh(payload, _test_response())


class TestSiweLogin:
    """Tests for the SIWE login endpoint."""

    @pytest.mark.asyncio
    async def test_returns_siwe_and_jwt_tokens(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test the consolidated /v1/accounting/auth/login endpoint."""
        # Set up token store for nonce validation
        storage_dir = tmp_path / "login_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        # Reset auth token singletons
        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        # Ensure routes use our token store
        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Generate a valid nonce
        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # Mock the siwe verify to pass (since we don't have a real signature)
        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        # SIWE token should be hex string starting with 0x
        assert response.siwe_token.startswith("0x")
        assert len(response.siwe_token) > 10  # Should have content
        assert response.jwt_access_token == "jwt-access-token"
        assert response.jwt_refresh_token == "jwt-refresh-token"
        assert response.address == TEST_ADDRESS
        assert response.jwt_expires_in == 12 * 3600
        assert response.jwt_refresh_expires_in == 7 * 24 * 3600

    @pytest.mark.asyncio
    async def test_rejects_replay_with_same_nonce(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login rejects replay attempts with the same nonce."""
        storage_dir = tmp_path / "replay_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        # Reset auth token singletons
        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        # Ensure routes use our token store
        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Generate a valid nonce
        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        request = SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)

        # Mock the siwe verify to pass (since we don't have a real signature)
        with patch.object(SiweMessage, "verify"):
            # First login should succeed
            response = await _call_siwe_login(request)
            assert response.jwt_access_token == "jwt-access-token"

            # Second login with same nonce (replay) should fail
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(request)

        assert exc.value.status_code == 400
        assert "Invalid or expired nonce" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_invalid_nonce(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects requests with invalid/unknown nonces."""
        storage_dir = tmp_path / "invalid_nonce_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        # Ensure routes use our token store
        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Use a nonce that looks valid (alphanumeric) but was never generated
        # SIWE requires alphanumeric nonces, so use token_hex
        fake_nonce = secrets.token_hex(32)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, fake_nonce)

        class _MockAccountingService:
            def siwe_login(self, message, signature):
                return {"token": "siwe-encrypted-token"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with pytest.raises(HTTPException) as exc:
            await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert exc.value.status_code == 400
        assert "Invalid or expired nonce" in exc.value.detail


class TestRefreshEndpoint:
    """Tests for the refresh endpoint."""

    @pytest.mark.asyncio
    async def test_refresh_endpoint(self, reset_auth_singletons, monkeypatch):
        """Test the /refresh endpoint."""

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def refresh_tokens(self, refresh_token):
                assert refresh_token == "old-refresh-token"
                return "new-access-token", "new-refresh-token"

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        response = await _call_refresh(routes.RefreshRequest(refresh_token="old-refresh-token"))

        assert response.token == "new-access-token"
        assert response.refresh_token == "new-refresh-token"
        assert response.expires_in == 12 * 3600
        assert response.refresh_expires_in == 7 * 24 * 3600

    @pytest.mark.asyncio
    async def test_refresh_endpoint_revoked_token(self, reset_auth_singletons, monkeypatch):
        """Test the /refresh endpoint with a revoked token returns 401."""

        class _JwtService:
            def refresh_tokens(self, refresh_token):
                raise ValueError("Refresh token has been revoked")

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        with pytest.raises(HTTPException) as exc:
            await _call_refresh(routes.RefreshRequest(refresh_token="revoked-token"))

        assert exc.value.status_code == 401
        assert exc.value.detail == "Refresh token has been revoked"

    @pytest.mark.asyncio
    async def test_refresh_endpoint_invalid_jwt_returns_401(
        self, reset_auth_singletons, monkeypatch
    ):
        """Test that /refresh returns 401 (not 500) for invalid JWT errors."""

        class _JwtService:
            def refresh_tokens(self, refresh_token):
                raise jwt.InvalidTokenError("Invalid token format")

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        with pytest.raises(HTTPException) as exc:
            await _call_refresh(routes.RefreshRequest(refresh_token="malformed-token"))

        assert exc.value.status_code == 401
        assert "Invalid token format" in exc.value.detail

    @pytest.mark.asyncio
    async def test_refresh_endpoint_expired_jwt_returns_401(
        self, reset_auth_singletons, monkeypatch
    ):
        """Test that /refresh returns 401 (not 500) for expired JWT errors."""

        class _JwtService:
            def refresh_tokens(self, refresh_token):
                raise jwt.ExpiredSignatureError("Signature has expired")

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        with pytest.raises(HTTPException) as exc:
            await _call_refresh(routes.RefreshRequest(refresh_token="expired-token"))

        assert exc.value.status_code == 401
        assert "Signature has expired" in exc.value.detail


class TestLogoutEndpoint:
    """Tests for the logout endpoint."""

    @pytest.mark.asyncio
    async def test_revokes_specific_refresh_token(self, reset_auth_singletons, monkeypatch):
        """Test that logout can revoke a specific refresh token owned by the user."""

        class _JwtService:
            def verify_refresh_token(self, token):
                # Token belongs to the current user
                return TEST_ADDRESS

            def revoke_refresh_token(self, token):
                assert token == "my-refresh-token"
                return True

            def revoke_all_refresh_tokens(self, address):
                raise AssertionError("Should not be called")

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        response = await routes.logout(
            payload=routes.LogoutRequest(refresh_token="my-refresh-token"),
            current_user=TEST_ADDRESS,
        )

        assert response["message"] == "Logged out successfully"
        assert response["revoked_tokens"] == 1

    @pytest.mark.asyncio
    async def test_revokes_all_refresh_tokens(self, reset_auth_singletons, monkeypatch):
        """Test that logout can revoke all refresh tokens for a user."""

        class _JwtService:
            def revoke_refresh_token(self, token):
                raise AssertionError("Should not be called")

            def revoke_all_refresh_tokens(self, address):
                assert address == TEST_ADDRESS
                return 5

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        response = await routes.logout(
            payload=routes.LogoutRequest(revoke_all=True),
            current_user=TEST_ADDRESS,
        )

        assert response["message"] == "Logged out successfully"
        assert response["revoked_tokens"] == 5

    @pytest.mark.asyncio
    async def test_rejects_revoking_other_users_token(self, reset_auth_singletons, monkeypatch):
        """Test that a user cannot revoke another user's refresh token."""

        class _JwtService:
            def verify_refresh_token(self, token):
                # Token belongs to a different user
                return OTHER_ADDRESS

            def revoke_refresh_token(self, token):
                raise AssertionError("Should not be called - token belongs to other user")

            def revoke_all_refresh_tokens(self, address):
                raise AssertionError("Should not be called")

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())

        with pytest.raises(HTTPException) as exc:
            await routes.logout(
                payload=routes.LogoutRequest(refresh_token="other-users-token"),
                current_user=TEST_ADDRESS,
            )

        assert exc.value.status_code == 403
        assert "another user" in exc.value.detail


class TestSiweValidationEdgeCases:
    """Tests for SIWE validation edge cases."""

    @pytest.mark.asyncio
    async def test_rejects_siwe_domain_mismatch(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects SIWE messages with domain mismatch."""

        storage_dir = tmp_path / "domain_mismatch_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Generate a valid nonce but use wrong domain in SIWE message
        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce, domain="wrong-domain.com")

        class _MockAccountingService:
            def get_siwe_domain(self):
                # Contract expects different domain
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # The siwe.verify() method validates domain internally
        # When domain doesn't match, it raises an exception
        def _verify_with_domain_check(signature, domain=None, nonce=None, **kwargs):
            if domain and domain != "wrong-domain.com":
                raise Exception("Domain mismatch")

        with patch.object(SiweMessage, "verify", side_effect=_verify_with_domain_check):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(
                    SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                )

        assert exc.value.status_code == 400
        assert "SIWE domain is not allowed" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_expired_siwe_message(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects expired SIWE messages."""
        storage_dir = tmp_path / "expired_siwe_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # Simulate expired message
        with patch.object(SiweMessage, "verify", side_effect=ExpiredMessage("Message has expired")):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(
                    SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                )

        assert exc.value.status_code == 400
        assert "expired" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_rejects_invalid_siwe_signature(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login rejects SIWE messages with invalid signatures."""
        storage_dir = tmp_path / "invalid_sig_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # Simulate invalid signature
        with patch.object(
            SiweMessage, "verify", side_effect=InvalidSignature("Signature verification failed")
        ):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(
                    SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                )

        assert exc.value.status_code == 400
        assert "Invalid signature" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_malformed_siwe_message(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login rejects malformed SIWE messages."""
        storage_dir = tmp_path / "malformed_siwe_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Send a completely malformed message
        with pytest.raises(HTTPException) as exc:
            await _call_siwe_login(
                SiweLoginRequest(
                    siwe_message="This is not a valid SIWE message", signature="0x" + "ab" * 65
                )
            )

        assert exc.value.status_code == 400
        assert "Invalid SIWE message" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_malformed_session(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects SIWE messages with malformed session data."""
        storage_dir = tmp_path / "malformed_session_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # Simulate malformed session
        with patch.object(
            SiweMessage, "verify", side_effect=MalformedSession("Malformed session data")
        ):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(
                    SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                )

        assert exc.value.status_code == 400
        assert "Invalid SIWE message format" in exc.value.detail


class TestSiweTimestampValidation:
    """Tests for SIWE message timestamp validation."""

    @pytest.mark.asyncio
    async def test_rejects_issued_at_too_old(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects SIWE messages with issued_at too far in the past."""
        storage_dir = tmp_path / "issued_at_old_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)

        # issued_at 10 minutes ago (beyond 5 min tolerance)
        old_issued_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        siwe_msg = _build_siwe_message(
            TEST_ADDRESS,
            test_nonce,
            issued_at=old_issued_at,
        )

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(
                    SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                )

        assert exc.value.status_code == 400
        assert "issued_at is outside acceptable time range" in exc.value.detail

    @pytest.mark.asyncio
    async def test_accepts_shorter_expiration_time(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login accepts SIWE messages with shorter expiration windows."""
        storage_dir = tmp_path / "wrong_expiry_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)

        # expiration_time only 1 hour from now (should be 24h)
        wrong_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        siwe_msg = _build_siwe_message(
            TEST_ADDRESS,
            test_nonce,
            expiration_time=wrong_expiry,
        )

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert response.siwe_token.startswith("0x")
        assert response.jwt_access_token == "jwt-access-token"

    @pytest.mark.asyncio
    async def test_rejects_expiration_too_long(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects SIWE messages with expiration time too far in future."""
        storage_dir = tmp_path / "long_expiry_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)

        # expiration_time 48 hours from now (should be 24h)
        long_expiry = datetime.now(timezone.utc) + timedelta(hours=48)
        siwe_msg = _build_siwe_message(
            TEST_ADDRESS,
            test_nonce,
            expiration_time=long_expiry,
        )

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(
                    SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                )

        assert exc.value.status_code == 400
        assert "maximum allowed validity window" in exc.value.detail

    @pytest.mark.asyncio
    async def test_accepts_valid_timestamps(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login accepts SIWE messages with valid timestamps."""
        storage_dir = tmp_path / "valid_timestamps_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)

        # Valid timestamps: issued now, expires in 24h
        now = datetime.now(timezone.utc)
        siwe_msg = _build_siwe_message(
            TEST_ADDRESS,
            test_nonce,
            issued_at=now,
            expiration_time=now + timedelta(seconds=AUTH_TOKEN_VALIDITY_SECONDS),
        )

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert response.siwe_token.startswith("0x")
        assert response.jwt_access_token == "jwt-access-token"
        assert response.address == TEST_ADDRESS

    @pytest.mark.asyncio
    async def test_accepts_timestamps_within_tolerance(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login accepts timestamps within the 5 minute tolerance."""
        storage_dir = tmp_path / "tolerance_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)

        # issued_at 3 minutes ago (within 5 min tolerance)
        # expiration slightly off but within tolerance
        now = datetime.now(timezone.utc)
        siwe_msg = _build_siwe_message(
            TEST_ADDRESS,
            test_nonce,
            issued_at=now - timedelta(minutes=3),
            expiration_time=now + timedelta(seconds=AUTH_TOKEN_VALIDITY_SECONDS - 60),
        )

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert response.siwe_token.startswith("0x")
        assert response.address == TEST_ADDRESS


class TestNonceEndpoint:
    """Tests for the /auth/nonce endpoint."""

    @pytest.mark.asyncio
    async def test_returns_nonce_for_valid_address(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that nonce endpoint returns a valid nonce for a valid address."""
        storage_dir = tmp_path / "nonce_endpoint_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None

        response = await _call_get_siwe_nonce(TEST_ADDRESS)

        assert response.address == TEST_ADDRESS
        assert response.nonce is not None
        assert len(response.nonce) > 0
        assert response.expires_in > 0

    @pytest.mark.asyncio
    async def test_returns_same_nonce_for_same_address(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that requesting nonce twice for same address returns the same nonce."""
        storage_dir = tmp_path / "nonce_same_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None

        response1 = await _call_get_siwe_nonce(TEST_ADDRESS)
        response2 = await _call_get_siwe_nonce(TEST_ADDRESS)

        assert response1.nonce == response2.nonce

    @pytest.mark.asyncio
    async def test_returns_different_nonce_for_different_address(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that different addresses get different nonces."""
        storage_dir = tmp_path / "nonce_diff_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None

        response1 = await _call_get_siwe_nonce(TEST_ADDRESS)
        response2 = await _call_get_siwe_nonce(OTHER_ADDRESS)

        assert response1.nonce != response2.nonce

    @pytest.mark.asyncio
    async def test_rejects_invalid_address(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that nonce endpoint rejects invalid Ethereum addresses."""
        storage_dir = tmp_path / "nonce_invalid_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None

        with pytest.raises(HTTPException) as exc:
            await _call_get_siwe_nonce("not-an-address")

        assert exc.value.status_code == 400
        assert "Invalid Ethereum address" in exc.value.detail

    @pytest.mark.asyncio
    async def test_normalizes_lowercase_address(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that nonce endpoint normalizes lowercase addresses to checksum."""
        storage_dir = tmp_path / "nonce_checksum_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        src.auth.token_store._token_store_instance = None

        # Use lowercase version of a valid address
        lowercase_addr = "0xabcdef1234567890abcdef1234567890abcdef12"
        checksum_addr = Web3.to_checksum_address(lowercase_addr)

        # Both should work and return the same nonce (normalized internally)
        response1 = await _call_get_siwe_nonce(lowercase_addr)
        response2 = await _call_get_siwe_nonce(checksum_addr)

        assert response1.address == checksum_addr
        assert response2.address == checksum_addr
        assert response1.nonce == response2.nonce


class TestLoginRetryBehavior:
    """Tests for login retry behavior with nonces."""

    @pytest.mark.asyncio
    async def test_allows_retry_with_same_nonce_after_signature_failure(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that the same nonce can be used again after a signature failure."""
        storage_dir = tmp_path / "retry_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        request = SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)

        # First attempt: signature fails
        with patch.object(SiweMessage, "verify", side_effect=InvalidSignature("Bad signature")):
            with pytest.raises(HTTPException) as exc:
                await _call_siwe_login(request)
            assert exc.value.status_code == 400
            assert "Invalid signature" in exc.value.detail

        # Nonce should still be valid for retry
        assert token_store.is_nonce_valid(test_nonce) is True

        # Second attempt: signature succeeds
        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(request)

        assert response.jwt_access_token == "jwt-access-token"

        # Now nonce should be consumed
        assert token_store.is_nonce_valid(test_nonce) is False

    @pytest.mark.asyncio
    async def test_consumes_nonce_only_on_successful_login(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that nonce is only consumed after successful authentication."""
        storage_dir = tmp_path / "consume_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        request = SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)

        # Verify nonce is valid before login
        assert token_store.is_nonce_valid(test_nonce) is True

        # Successful login
        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(request)

        assert response.jwt_access_token == "jwt-access-token"

        # Verify nonce is consumed after successful login
        assert token_store.is_nonce_valid(test_nonce) is False


class TestMissingTimestampFields:
    """Tests for SIWE messages with missing required timestamp fields."""

    @pytest.mark.asyncio
    async def test_rejects_missing_issued_at(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login rejects SIWE messages without issued_at field."""
        storage_dir = tmp_path / "missing_issued_at_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)

        # Build SIWE message without issued_at (manually construct to omit field)
        now = datetime.now(timezone.utc)
        siwe_msg = SiweMessage(
            domain="localhost:5173",
            address=TEST_ADDRESS,
            uri="http://localhost:5173",
            version="1",
            chain_id=1,
            issued_at=now.isoformat().replace("+00:00", "Z"),
            expiration_time=(now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            nonce=test_nonce,
            statement=f"Sign in to Privana on chain {SAPPHIRE_CHAIN_ID}",
        ).prepare_message()

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # Patch the parsed message to have no issued_at
        def _verify_no_op(*args, **kwargs):
            pass

        with patch.object(SiweMessage, "verify", _verify_no_op):
            # Manually patch the parsed message's issued_at to None
            original_from_message = SiweMessage.from_message

            def patched_from_message(msg, *args, **kwargs):
                result = original_from_message(msg, *args, **kwargs)
                object.__setattr__(result, "issued_at", None)
                return result

            with patch.object(SiweMessage, "from_message", patched_from_message):
                with pytest.raises(HTTPException) as exc:
                    await _call_siwe_login(
                        SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                    )

        assert exc.value.status_code == 400
        assert "issued_at" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_missing_expiration_time(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login rejects SIWE messages without expiration_time field."""
        storage_dir = tmp_path / "missing_expiry_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        def _verify_no_op(*args, **kwargs):
            pass

        with patch.object(SiweMessage, "verify", _verify_no_op):
            # Patch to remove expiration_time
            original_from_message = SiweMessage.from_message

            def patched_from_message(msg, *args, **kwargs):
                result = original_from_message(msg, *args, **kwargs)
                object.__setattr__(result, "expiration_time", None)
                return result

            with patch.object(SiweMessage, "from_message", patched_from_message):
                with pytest.raises(HTTPException) as exc:
                    await _call_siwe_login(
                        SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
                    )

        assert exc.value.status_code == 400
        assert "expiration_time" in exc.value.detail


class TestChainIdValidation:
    """Tests for the SIWE destination chain ID requirement.

    The server requires the SIWE message to declare a ``Destination Chain ID``
    that matches its configured ``sapphire_chain_id``. The standard SIWE
    ``Chain ID`` field (the wallet's current network) is not used for this check.
    """

    @pytest.mark.asyncio
    async def test_rejects_missing_destination_chain_id(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login rejects SIWE messages without a destination chain ID."""
        storage_dir = tmp_path / "chain_id_missing_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.config._settings = None

        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        # Omit the destination chain ID entirely.
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce, destination_chain_id=None)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with pytest.raises(HTTPException) as exc:
            await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert exc.value.status_code == 400
        assert "Destination chain in statement not defined" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_mismatched_destination_chain_id(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login rejects a destination chain ID that is not the Sapphire one."""
        storage_dir = tmp_path / "chain_id_reject_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.config._settings = None

        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        # Destination chain ID 999 does not match the configured Sapphire chain ID.
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce, destination_chain_id=999)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with pytest.raises(HTTPException) as exc:
            await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert exc.value.status_code == 400
        assert "Destination chain 999 not supported" in exc.value.detail

    @pytest.mark.asyncio
    async def test_accepts_matching_destination_chain_id(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login accepts a destination chain ID equal to the Sapphire one."""
        storage_dir = tmp_path / "chain_id_accept_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.config._settings = None

        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        # Default destination_chain_id matches the configured Sapphire chain ID.
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert response.siwe_token.startswith("0x")
        assert response.jwt_access_token == "jwt-access-token"

    @pytest.mark.asyncio
    async def test_validates_against_configured_sapphire_chain_id(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that the required destination chain ID follows SAPPHIRE_CHAIN_ID config."""
        storage_dir = tmp_path / "chain_id_config_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        # Reconfigure the Sapphire chain ID; the message must match the new value.
        monkeypatch.setenv("SAPPHIRE_CHAIN_ID", "23294")
        src.config._settings = None

        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce, destination_chain_id=23294)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600

            def create_token(self, address):
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: _JwtService())
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert response.siwe_token.startswith("0x")
        assert response.jwt_access_token == "jwt-access-token"

        # Reset config for other tests
        src.config._settings = None


class TestConfigErrors:
    """Tests for handling configuration errors during login."""

    @pytest.mark.asyncio
    async def test_rejects_missing_siwe_domain_config(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test that login fails when SIWE_DOMAINS is not configured."""
        storage_dir = tmp_path / "missing_domain_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        # Ensure SIWE_DOMAINS is not set
        monkeypatch.delenv("SIWE_DOMAINS", raising=False)

        # Reset config singleton to pick up the missing env var
        src.config._settings = None

        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        test_nonce = token_store.generate_nonce(client_id=TEST_ADDRESS)
        siwe_msg = _build_siwe_message(TEST_ADDRESS, test_nonce)

        with pytest.raises(HTTPException) as exc:
            await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        assert exc.value.status_code == 500
        assert "SIWE_DOMAINS not configured" in exc.value.detail

        # Reset config for other tests
        src.config._settings = None


class TestLoginAddressNormalization:
    """Tests for address normalization during login."""

    @pytest.mark.asyncio
    async def test_returns_checksummed_address(self, reset_auth_singletons, monkeypatch, tmp_path):
        """Test that login returns a properly checksummed address."""
        storage_dir = tmp_path / "checksum_login_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
        src.auth.token_store._token_store_instance = None
        token_store = TokenStore()

        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Use a real-looking address that has mixed case in checksum form
        test_addr = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
        test_nonce = token_store.generate_nonce(client_id=test_addr)

        # Build message with lowercase address (the SIWE library will normalize)
        siwe_msg = _build_siwe_message(test_addr, test_nonce)

        class _JwtService:
            access_token_expiry_seconds = 12 * 3600
            refresh_token_expiry_seconds = 7 * 24 * 3600
            captured_address = None

            def create_token(self, address):
                self.captured_address = address
                return "jwt-access-token"

            def create_refresh_token(self, address):
                return "jwt-refresh-token"

        jwt_service = _JwtService()

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "get_jwt_service", lambda: jwt_service)
        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        with patch.object(SiweMessage, "verify"):
            response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        # Response should have checksummed address
        assert response.address == test_addr
        # JWT service should have received checksummed address
        assert jwt_service.captured_address == test_addr


class TestLoginFlowIntegration:
    """Integration tests for the full login flow with real services."""

    @pytest.mark.asyncio
    async def test_full_login_flow_with_real_jwt_service(
        self, reset_auth_singletons, monkeypatch, tmp_path
    ):
        """Test full login flow with real JWT service (not mocked)."""
        storage_dir = tmp_path / "integration_test"
        monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(storage_dir))
        monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")

        # Reset all singletons
        src.auth.token_store._token_store_instance = None
        src.auth.jwt_keys._jwt_key_manager_instance = None
        src.auth.jwt_service._jwt_service_instance = None
        src.auth.auth_token_keys._auth_token_key_manager_instance = None
        src.auth.auth_token_service._auth_token_service_instance = None
        await _init_key_managers()

        token_store = TokenStore()
        monkeypatch.setattr(routes, "get_token_store", lambda: token_store)

        # Use real JWT service
        jwt_service = JWTService()
        monkeypatch.setattr(routes, "get_jwt_service", lambda: jwt_service)

        class _MockAccountingService:
            def get_siwe_domain(self):
                return {"domain": "localhost:5173"}

        monkeypatch.setattr(routes, "_service", _MockAccountingService())

        # Step 1: Get nonce
        nonce_response = await _call_get_siwe_nonce(TEST_ADDRESS)
        assert nonce_response.nonce is not None

        # Step 2: Build SIWE message
        siwe_msg = _build_siwe_message(TEST_ADDRESS, nonce_response.nonce)

        # Step 3: Login
        with patch.object(SiweMessage, "verify"):
            login_response = await _call_siwe_login(
                SiweLoginRequest(siwe_message=siwe_msg, signature="0x" + "ab" * 65)
            )

        # Verify response has all expected fields
        assert login_response.siwe_token.startswith("0x")
        assert login_response.jwt_access_token is not None
        assert login_response.jwt_refresh_token is not None
        assert login_response.address == TEST_ADDRESS
        assert login_response.jwt_expires_in > 0
        assert login_response.jwt_refresh_expires_in > 0

        # Step 4: Verify the JWT tokens are valid
        access_address = jwt_service.get_address_from_token(login_response.jwt_access_token)
        assert access_address == TEST_ADDRESS

        # Step 5: Refresh tokens
        new_access, new_refresh = jwt_service.refresh_tokens(login_response.jwt_refresh_token)
        assert new_access is not None
        assert new_refresh is not None

        # Verify new access token is valid
        assert jwt_service.get_address_from_token(new_access) == TEST_ADDRESS

        # Step 6: Old refresh token should be consumed (one-time use)
        with pytest.raises(ValueError, match="revoked"):
            jwt_service.refresh_tokens(login_response.jwt_refresh_token)


class TestAuthDependencies:
    """Tests for auth dependencies."""

    def test_get_current_user_returns_generic_invalid_token_error(
        self, reset_auth_singletons, monkeypatch
    ):
        """Test that get_current_user returns a generic error message."""

        class _JWTService:
            def get_access_token_payload(self, token):
                raise jwt.InvalidTokenError("too many segments")

        monkeypatch.setattr(auth_dependencies, "get_jwt_service", lambda: _JWTService())

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="not-a-jwt")
        with pytest.raises(HTTPException) as exc:
            auth_dependencies.get_current_user(credentials)

        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid token"
