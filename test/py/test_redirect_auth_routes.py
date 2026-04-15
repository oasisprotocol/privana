import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from web3 import Web3

from src.api import auth_routes, routes
from src.auth import (
    auth_token_keys,
    client_registry,
    code_store,
    jwt_keys,
    jwt_service,
    rate_limiter,
    token_store,
)
from src.auth.client_registry import ClientRegistry
from src.auth.code_store import AuthCodeRecord, get_auth_code_store
from src.auth.siwe_config import get_siwe_config
from src.config import load_settings

TEST_CLIENT_ID = "casino-web"
TEST_REDIRECT_URI = "https://casino.flexvaults.test/callback"
TEST_REDIRECT_ORIGIN = "https://casino.flexvaults.test"
TEST_DOMAIN = "auth.flexvaults.com"
TEST_ADDRESS = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
TEST_HOSTED_CHAIN_ID = 84532


def _build_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _reset_auth_state() -> None:
    if token_store._token_store_instance is not None:
        token_store._token_store_instance.close()
    if code_store._auth_code_store_instance is not None:
        code_store._auth_code_store_instance.close()
    if rate_limiter._auth_rate_limiter_instance is not None:
        rate_limiter._auth_rate_limiter_instance.close()

    token_store._token_store_instance = None
    code_store._auth_code_store_instance = None
    rate_limiter._auth_rate_limiter_instance = None
    client_registry._client_registry_instance = None
    jwt_service._jwt_service_instance = None
    jwt_keys._jwt_key_manager_instance = None
    auth_token_keys._auth_token_key_manager_instance = None


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
    monkeypatch.setenv("SIWE_DOMAIN", TEST_DOMAIN)
    monkeypatch.setenv("SIWE_ALLOWED_CHAIN_IDS", f"{TEST_HOSTED_CHAIN_ID},23295")
    monkeypatch.setenv("AUTH_TOKEN_VALIDITY_SECONDS", "600")
    monkeypatch.setenv("AUTH_CODE_TTL_SECONDS", "90")
    monkeypatch.setenv("JWT_EXPIRY_HOURS", "1")
    monkeypatch.setenv("JWT_REFRESH_EXPIRY_DAYS", "7")
    monkeypatch.setenv("JWT_ISSUER", "flexvaults-test")
    monkeypatch.setenv("JWT_AUDIENCE", "flexvaults-test")
    monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(tmp_path / "auth-store"))
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("AUTH_NONCE_RATE_LIMIT", "2")
    monkeypatch.setenv("AUTH_LOGIN_RATE_LIMIT", "5")
    monkeypatch.setenv("AUTH_AUTHORIZE_RATE_LIMIT", "5")
    monkeypatch.setenv("AUTH_TOKEN_RATE_LIMIT", "5")
    monkeypatch.setenv(
        "AUTH_CLIENTS",
        json.dumps(
            [
                {
                    "client_id": TEST_CLIENT_ID,
                    "display_name": "Casino",
                    "redirect_uris": [TEST_REDIRECT_URI],
                }
            ]
        ),
    )

    load_settings(refresh=True)
    _reset_auth_state()
    asyncio.run(jwt_keys.get_jwt_key_manager().initialize(use_rofl=False))
    asyncio.run(auth_token_keys.get_auth_token_key_manager().initialize(use_rofl=False))

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(auth_routes.auth_router)
    app.mount("/static", StaticFiles(directory=Path("src/static")), name="static")

    yield app

    _reset_auth_state()


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as test_client:
        yield test_client


def test_authorize_page_renders_registered_client(client):
    response = client.get(
        "/v1/accounting/auth/authorize",
        params={
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
            "code_challenge": _build_pkce_challenge("v" * 43),
            "code_challenge_method": "S256",
            "chain_id": TEST_HOSTED_CHAIN_ID,
            "response_mode": "web_message",
            "state": "state-123",
        },
    )

    assert response.status_code == 200
    assert "Casino" in response.text
    assert TEST_REDIRECT_ORIGIN in response.text
    assert "/static/auth.css?v=" in response.text
    assert "/static/auth.js?v=" in response.text
    assert f'"preferredChainId": {TEST_HOSTED_CHAIN_ID}' in response.text
    assert '"supportedChainIds": [23295, 84532]' in response.text
    assert response.headers["cache-control"] == "no-store"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    # Firefox needs relaxed script-src for wallet-provider injection on this page.
    assert "script-src 'self' 'unsafe-inline'; " in csp


def test_authorize_page_preserves_root_path_in_rendered_urls(test_app):
    with TestClient(test_app, root_path="/api") as prefixed_client:
        response = prefixed_client.get(
            "/v1/accounting/auth/authorize",
            params={
                "client_id": TEST_CLIENT_ID,
                "redirect_uri": TEST_REDIRECT_URI,
                "code_challenge": _build_pkce_challenge("v" * 43),
                "code_challenge_method": "S256",
                "chain_id": TEST_HOSTED_CHAIN_ID,
                "response_mode": "web_message",
                "state": "state-123",
            },
        )

    assert response.status_code == 200
    assert "/api/static/auth.css?v=" in response.text
    assert "/api/static/auth.js?v=" in response.text
    assert '"nonceEndpoint": "/api/v1/accounting/auth/nonce"' in response.text
    assert '"authorizeEndpoint": "/api/v1/accounting/auth/authorize"' in response.text
    assert "http://testserver" not in response.text


def test_authorize_page_rejects_unsupported_chain(client):
    response = client.get(
        "/v1/accounting/auth/authorize",
        params={
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
            "code_challenge": _build_pkce_challenge("v" * 43),
            "code_challenge_method": "S256",
            "chain_id": 23294,
            "response_mode": "web_message",
            "state": "state-123",
        },
    )

    assert response.status_code == 400
    assert "Unsupported Chain" in response.text
    assert "Chain ID 23294 is not supported for hosted sign-in." in response.text


def test_authorize_code_and_exchange_is_single_use(client, monkeypatch):
    verifier = "v" * 43
    challenge = _build_pkce_challenge(verifier)
    monkeypatch.setattr(
        auth_routes,
        "authenticate_siwe_message",
        lambda _message, _signature: SimpleNamespace(address=TEST_ADDRESS),
    )

    authorize_response = client.post(
        "/v1/accounting/auth/authorize",
        json={
            "siwe_message": "message",
            "signature": "0x1234",
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )

    assert authorize_response.status_code == 200
    assert authorize_response.headers["cache-control"] == "no-store"
    code = authorize_response.json()["code"]

    token_response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
        },
    )

    assert token_response.status_code == 200
    body = token_response.json()
    assert body["token_type"] == "Bearer"
    assert body["address"] == TEST_ADDRESS
    assert token_response.headers["access-control-allow-origin"] == TEST_REDIRECT_ORIGIN

    jwt_api = jwt_service.get_jwt_service()
    assert jwt_api.get_address_from_token(body["access_token"]) == TEST_ADDRESS
    assert jwt_api.verify_id_token(body["id_token"], TEST_CLIENT_ID) == TEST_ADDRESS
    assert jwt_api.verify_refresh_token(body["refresh_token"]) == TEST_ADDRESS

    second_exchange = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
        },
    )

    assert second_exchange.status_code == 400
    assert second_exchange.json()["error"] == "invalid_grant"


def test_token_exchange_rejects_origin_mismatch(client):
    verifier = "v" * 43
    challenge = _build_pkce_challenge(verifier)
    code = get_auth_code_store().create_code(
        AuthCodeRecord(
            user_address=TEST_ADDRESS,
            client_id=TEST_CLIENT_ID,
            redirect_uri=TEST_REDIRECT_URI,
            code_challenge=challenge,
            code_challenge_method="S256",
        )
    )

    response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": "https://attacker.flexvaults.test"},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_origin"


def test_token_exchange_accepts_default_https_port_equivalent_redirect_uri(client):
    verifier = "v" * 43
    challenge = _build_pkce_challenge(verifier)
    code = get_auth_code_store().create_code(
        AuthCodeRecord(
            user_address=TEST_ADDRESS,
            client_id=TEST_CLIENT_ID,
            redirect_uri=TEST_REDIRECT_URI,
            code_challenge=challenge,
            code_challenge_method="S256",
        )
    )

    response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": "https://casino.flexvaults.test:443/callback",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TEST_REDIRECT_ORIGIN


def test_token_exchange_accepts_root_redirect_uri_with_trailing_slash(client):
    root_redirect_uri = "https://casino.flexvaults.test"
    verifier = "v" * 43
    challenge = _build_pkce_challenge(verifier)
    client_registry._client_registry_instance = ClientRegistry(
        json.dumps(
            [
                {
                    "client_id": TEST_CLIENT_ID,
                    "display_name": "Casino",
                    "redirect_uris": [root_redirect_uri],
                }
            ]
        )
    )
    code = get_auth_code_store().create_code(
        AuthCodeRecord(
            user_address=TEST_ADDRESS,
            client_id=TEST_CLIENT_ID,
            redirect_uri=ClientRegistry.normalize_redirect_uri(root_redirect_uri),
            code_challenge=challenge,
            code_challenge_method="S256",
        )
    )

    response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": "https://casino.flexvaults.test/",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TEST_REDIRECT_ORIGIN


def test_token_exchange_rejects_pkce_mismatch(client):
    code = get_auth_code_store().create_code(
        AuthCodeRecord(
            user_address=TEST_ADDRESS,
            client_id=TEST_CLIENT_ID,
            redirect_uri=TEST_REDIRECT_URI,
            code_challenge=_build_pkce_challenge("v" * 43),
            code_challenge_method="S256",
        )
    )

    response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "w" * 43,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert response.headers["access-control-allow-origin"] == TEST_REDIRECT_ORIGIN


def test_login_returns_siwe_token_and_jwts(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "authenticate_siwe_message",
        lambda _message, _signature: SimpleNamespace(
            siwe_token_hex="0x1234",
            address=TEST_ADDRESS,
        ),
    )

    response = client.post(
        "/v1/accounting/auth/login",
        json={"siwe_message": "message", "signature": "0x1234"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["siwe_token"] == "0x1234"
    assert body["address"] == TEST_ADDRESS

    jwt_api = jwt_service.get_jwt_service()
    assert jwt_api.get_address_from_token(body["jwt_access_token"]) == TEST_ADDRESS
    assert jwt_api.verify_refresh_token(body["jwt_refresh_token"]) == TEST_ADDRESS


def test_token_exchange_issues_id_token_with_explicit_client_audience(client, monkeypatch):
    verifier = "v" * 43
    challenge = _build_pkce_challenge(verifier)
    custom_audience = "oasis-flexvaults"
    client_registry._client_registry_instance = ClientRegistry(
        json.dumps(
            [
                {
                    "client_id": TEST_CLIENT_ID,
                    "display_name": "Casino",
                    "audience": custom_audience,
                    "redirect_uris": [TEST_REDIRECT_URI],
                }
            ]
        )
    )
    monkeypatch.setattr(
        auth_routes,
        "authenticate_siwe_message",
        lambda _message, _signature: SimpleNamespace(address=TEST_ADDRESS),
    )

    authorize_response = client.post(
        "/v1/accounting/auth/authorize",
        json={
            "siwe_message": "message",
            "signature": "0x1234",
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )

    code = authorize_response.json()["code"]

    token_response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
        },
    )

    assert token_response.status_code == 200
    body = token_response.json()
    jwt_api = jwt_service.get_jwt_service()
    assert jwt_api.verify_id_token(body["id_token"], custom_audience) == TEST_ADDRESS


def test_login_rejects_browser_requests_from_non_auth_origin(client):
    response = client.post(
        "/v1/accounting/auth/login",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={"siwe_message": "message", "signature": "0x1234"},
    )

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "Browser SIWE requests must originate from the configured auth origin"
    )


def test_authorize_code_rejects_browser_requests_from_non_auth_origin(client):
    response = client.post(
        "/v1/accounting/auth/authorize",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "siwe_message": "message",
            "signature": "0x1234",
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
            "code_challenge": _build_pkce_challenge("v" * 43),
            "code_challenge_method": "S256",
        },
    )

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == (
        "Browser authorization requests must originate from the configured auth origin"
    )


def test_private_read_token_prefers_backend_minted_auth_token(monkeypatch):
    auth_token_service = MagicMock()
    auth_token_service.create_and_encrypt.return_value = b"\xde\xad"
    monkeypatch.setattr(routes, "get_auth_token_service", lambda: auth_token_service)
    monkeypatch.setattr(
        routes,
        "load_settings",
        lambda: SimpleNamespace(
            siwe_domain=TEST_DOMAIN,
            auth_token_validity_seconds=600,
        ),
    )
    monkeypatch.setattr(routes.time, "time", lambda: 1_700_000_000)

    auth = routes._require_private_read_auth(current_user=TEST_ADDRESS, token=None)

    assert auth.token == b"\xde\xad"
    assert auth.user_address == TEST_ADDRESS
    auth_token_service.create_and_encrypt.assert_called_once_with(
        domain=TEST_DOMAIN,
        user_addr=TEST_ADDRESS,
        valid_until=1_700_000_600,
    )


def test_private_read_token_resolves_user_from_direct_header_token(monkeypatch):
    auth_token_service = MagicMock()
    auth_token_service.decode_auth_token.return_value = SimpleNamespace(user_addr=TEST_ADDRESS)
    monkeypatch.setattr(routes, "get_auth_token_service", lambda: auth_token_service)

    auth = routes._require_private_read_auth(
        current_user=None,
        authorization=None,
        token="0x1234",
    )

    assert auth.token == b"\x12\x34"
    assert auth.user_address == TEST_ADDRESS
    auth_token_service.decode_auth_token.assert_called_once_with(
        b"\x12\x34",
        validate_expiry=False,
    )


def test_private_read_token_rejects_invalid_direct_header_token(monkeypatch):
    auth_token_service = MagicMock()
    auth_token_service.decode_auth_token.side_effect = ValueError("Invalid auth token")
    monkeypatch.setattr(routes, "get_auth_token_service", lambda: auth_token_service)

    with pytest.raises(HTTPException) as exc_info:
        routes._require_private_read_auth(
            current_user=None,
            authorization=None,
            token="0x1234",
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired SIWE token"


def test_private_read_token_rejects_mixed_auth_headers():
    with pytest.raises(HTTPException) as exc_info:
        routes._require_private_read_auth(
            current_user=TEST_ADDRESS,
            authorization="Bearer stale-jwt",
            token="0x1234",
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "Provide either Authorization bearer token or X-SIWE-Token, not both"
    )


def test_private_read_route_rejects_mixed_auth_headers(client, test_app):
    test_app.dependency_overrides[routes.get_current_user_optional] = lambda: TEST_ADDRESS
    try:
        response = client.get(
            "/v1/accounting/balances/0xtoken",
            headers={
                "Authorization": "Bearer stale-jwt",
                "X-SIWE-Token": "0x1234",
            },
        )
    finally:
        test_app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Provide either Authorization bearer token or X-SIWE-Token, not both"
    )


def test_nonce_endpoint_is_rate_limited(client):
    params = {"address": TEST_ADDRESS}

    first = client.get("/v1/accounting/auth/nonce", params=params)
    second = client.get("/v1/accounting/auth/nonce", params=params)
    third = client.get("/v1/accounting/auth/nonce", params=params)

    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.headers["cache-control"] == "no-store"
    assert int(third.headers["retry-after"]) >= 1


def test_nonce_endpoint_returns_checksummed_address(client):
    response = client.get(
        "/v1/accounting/auth/nonce",
        params={"address": TEST_ADDRESS.lower()},
    )

    assert response.status_code == 200
    assert response.json()["address"] == TEST_ADDRESS


def test_nonce_rate_limit_does_not_trust_x_forwarded_for_by_default(client):
    params = {"address": TEST_ADDRESS}

    first = client.get(
        "/v1/accounting/auth/nonce", params=params, headers={"X-Forwarded-For": "1.1.1.1"}
    )
    second = client.get(
        "/v1/accounting/auth/nonce",
        params=params,
        headers={"X-Forwarded-For": "2.2.2.2"},
    )
    third = client.get(
        "/v1/accounting/auth/nonce", params=params, headers={"X-Forwarded-For": "3.3.3.3"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


def test_refresh_sets_no_store_headers(client):
    refresh_token = jwt_service.get_jwt_service().create_refresh_token(TEST_ADDRESS)

    response = client.post(
        "/v1/accounting/auth/jwt/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_token_exchange_rejects_non_ascii_pkce_verifier(client):
    code = get_auth_code_store().create_code(
        AuthCodeRecord(
            user_address=TEST_ADDRESS,
            client_id=TEST_CLIENT_ID,
            redirect_uri=TEST_REDIRECT_URI,
            code_challenge=_build_pkce_challenge("v" * 43),
            code_challenge_method="S256",
        )
    )

    response = client.post(
        "/v1/accounting/auth/token",
        headers={"Origin": TEST_REDIRECT_ORIGIN},
        json={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": "ü" * 43,
            "client_id": TEST_CLIENT_ID,
            "redirect_uri": TEST_REDIRECT_URI,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_client_registry_rejects_insecure_non_loopback_http_redirect_uri():
    with pytest.raises(ValueError, match="https unless it targets localhost"):
        ClientRegistry(
            json.dumps(
                [
                    {
                        "client_id": "casino-web",
                        "display_name": "Casino",
                        "redirect_uris": ["http://casino.flexvaults.test/callback"],
                    }
                ]
            )
        )


def test_client_registry_normalizes_default_https_port():
    registry = ClientRegistry(
        json.dumps(
            [
                {
                    "client_id": "casino-web",
                    "display_name": "Casino",
                    "redirect_uris": ["https://casino.flexvaults.test:443/callback"],
                }
            ]
        )
    )

    assert registry.validate_redirect_uri("casino-web", "https://casino.flexvaults.test/callback")
    assert registry.validate_redirect_uri(
        "casino-web", "https://casino.flexvaults.test:443/callback"
    )
    assert ClientRegistry.redirect_origin("https://casino.flexvaults.test:443/callback") == (
        TEST_REDIRECT_ORIGIN
    )


def test_client_registry_normalizes_root_redirect_uri():
    registry = ClientRegistry(
        json.dumps(
            [
                {
                    "client_id": "casino-web",
                    "display_name": "Casino",
                    "redirect_uris": ["https://casino.flexvaults.test"],
                }
            ]
        )
    )

    assert registry.validate_redirect_uri("casino-web", "https://casino.flexvaults.test")
    assert registry.validate_redirect_uri("casino-web", "https://casino.flexvaults.test/")
    assert ClientRegistry.normalize_redirect_uri("https://casino.flexvaults.test") == (
        "https://casino.flexvaults.test/"
    )


def test_client_registry_defaults_audience_to_client_id():
    registry = ClientRegistry(
        json.dumps(
            [
                {
                    "client_id": "casino-web",
                    "display_name": "Casino",
                    "redirect_uris": ["https://casino.flexvaults.test/callback"],
                }
            ]
        )
    )

    assert registry.require_client("casino-web").audience == "casino-web"


def test_client_registry_accepts_explicit_audience():
    registry = ClientRegistry(
        json.dumps(
            [
                {
                    "client_id": "casino-web",
                    "display_name": "Casino",
                    "audience": "oasis-flexvaults",
                    "redirect_uris": ["https://casino.flexvaults.test/callback"],
                }
            ]
        )
    )

    assert registry.require_client("casino-web").audience == "oasis-flexvaults"


def test_build_cors_origins_raises_on_invalid_auth_client_registry(monkeypatch):
    import src.main as main

    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(
            cors_allowed_origins="",
            environment="production",
            siwe_domain="https://auth.flexvaults.com",
        ),
    )
    monkeypatch.setattr(
        main,
        "get_siwe_config",
        lambda _settings: SimpleNamespace(origin="https://auth.flexvaults.com"),
    )
    monkeypatch.setattr(
        main,
        "get_client_registry",
        MagicMock(side_effect=ValueError("Invalid AUTH_CLIENTS JSON")),
    )

    with pytest.raises(ValueError, match="Invalid AUTH_CLIENTS JSON"):
        main._build_cors_origins()


def test_siwe_config_normalizes_default_https_port():
    config = get_siwe_config(
        SimpleNamespace(
            siwe_domain="https://auth.flexvaults.com:443",
            environment="production",
        )
    )

    assert config.domain == TEST_DOMAIN
    assert config.origin == "https://auth.flexvaults.com"
