"""Shared pytest fixtures for auth tests."""

import asyncio

import pytest
from dotenv import load_dotenv

import src.auth.auth_token_keys as auth_token_keys
import src.auth.auth_token_service as auth_token_service
import src.auth.jwt_keys as jwt_keys
import src.auth.jwt_service as jwt_service
import src.auth.rate_limiter as rate_limiter
import src.auth.token_store as token_store
import src.config
import src.services.onramp_intent as onramp_intent
import src.services.rpc_identity as rpc_identity

load_dotenv(".env.localnet")


def _run_async(coro):
    """Run an async coroutine in sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # If there's already a running loop, create a task
        return asyncio.ensure_future(coro)
    else:
        # Create new event loop for sync context
        return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def reset_rpc_identity():
    """Drop the process-wide verified-RPC set between tests.

    A test that runs the identity check would otherwise leave every later test
    serving only the chains that test verified.
    """
    rpc_identity.reset_verified_chain_rpc_urls()
    yield
    rpc_identity.reset_verified_chain_rpc_urls()


@pytest.fixture
def reset_auth_singletons(monkeypatch, tmp_path):
    """Reset auth singletons so tests remain isolated."""
    jwt_keys._jwt_key_manager_instance = None
    jwt_service._jwt_service_instance = None
    token_store._token_store_instance = None
    auth_token_keys._auth_token_key_manager_instance = None
    auth_token_service._auth_token_service_instance = None
    onramp_intent._onramp_intent_key_manager_instance = None
    if rate_limiter._auth_rate_limiter_instance is not None:
        rate_limiter._auth_rate_limiter_instance.close()
    rate_limiter._auth_rate_limiter_instance = None
    src.config._settings = None

    # Use temp directory for token storage in tests
    monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(tmp_path / "auth_tokens"))
    # Set SIWE domain allow-list for tests
    monkeypatch.setenv("SIWE_DOMAINS", "localhost:5173")

    yield

    jwt_keys._jwt_key_manager_instance = None
    jwt_service._jwt_service_instance = None
    token_store._token_store_instance = None
    auth_token_keys._auth_token_key_manager_instance = None
    auth_token_service._auth_token_service_instance = None
    onramp_intent._onramp_intent_key_manager_instance = None
    if rate_limiter._auth_rate_limiter_instance is not None:
        rate_limiter._auth_rate_limiter_instance.close()
    rate_limiter._auth_rate_limiter_instance = None
    src.config._settings = None


# TODO: Remove this fixture when Sapphire localnet e2e tests are available.
# Tests should run against a real Sapphire environment instead.
@pytest.fixture
def disable_rofl_keys(monkeypatch):
    """Disable ROFL keys for testing and initialize key managers."""
    monkeypatch.setenv("DISABLE_ROFL_KEYS", "1")

    # Initialize JWT key manager (required before JWTService can be used)
    jwt_key_manager = jwt_keys.get_jwt_key_manager()
    _run_async(jwt_key_manager.initialize(use_rofl=False))

    # Initialize AuthToken key manager
    auth_key_manager = auth_token_keys.get_auth_token_key_manager()
    _run_async(auth_key_manager.initialize(use_rofl=False))

    # Initialize provider-neutral on-ramp intent keys
    intent_key_manager = onramp_intent.get_onramp_intent_key_manager()
    _run_async(intent_key_manager.initialize(use_rofl=False))
