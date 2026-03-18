"""Shared pytest fixtures for auth tests."""

import pytest

import src.auth.auth_token_keys as auth_token_keys
import src.auth.auth_token_service as auth_token_service
import src.auth.jwt_keys as jwt_keys
import src.auth.jwt_service as jwt_service
import src.auth.token_store as token_store
import src.config


@pytest.fixture
def reset_auth_singletons(monkeypatch, tmp_path):
    """Reset auth singletons so tests remain isolated."""
    jwt_keys._jwt_key_manager_instance = None
    jwt_service._jwt_service_instance = None
    token_store._token_store_instance = None
    auth_token_keys._auth_token_key_manager_instance = None
    auth_token_service._auth_token_service_instance = None
    src.config._settings = None

    # Use temp directory for token storage in tests
    monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(tmp_path / "auth_tokens"))
    # Set SIWE domain for tests
    monkeypatch.setenv("SIWE_DOMAIN", "localhost:5173")
    # Allow chain_id=1 (Ethereum mainnet) used in SIWE test messages
    monkeypatch.setenv("SIWE_ALLOWED_CHAIN_IDS", "1")

    yield

    jwt_keys._jwt_key_manager_instance = None
    jwt_service._jwt_service_instance = None
    token_store._token_store_instance = None
    auth_token_keys._auth_token_key_manager_instance = None
    auth_token_service._auth_token_service_instance = None
    src.config._settings = None


# TODO: Remove this fixture when Sapphire localnet e2e tests are available.
# Tests should run against a real Sapphire environment instead.
@pytest.fixture
def disable_rofl_keys(monkeypatch):
    """Disable ROFL keys for testing."""
    monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")
