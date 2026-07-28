"""Tests for application startup security invariants."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.main as main


@pytest.mark.asyncio
async def test_lifespan_aborts_when_auth_token_key_sync_fails(monkeypatch) -> None:
    monkeypatch.delenv("DISABLE_ROFL_KEYS", raising=False)

    jwt_key_manager = SimpleNamespace(initialize=AsyncMock())
    auth_token_key_manager = SimpleNamespace(
        initialize=AsyncMock(),
        sync_key_to_contract=AsyncMock(side_effect=RuntimeError("sync failed")),
    )
    onramp_intent_key_manager = SimpleNamespace(initialize=AsyncMock())
    bootstrap_rofl_signer_address = AsyncMock()

    monkeypatch.setattr(main, "get_jwt_key_manager", lambda: jwt_key_manager)
    monkeypatch.setattr(main, "get_auth_token_key_manager", lambda: auth_token_key_manager)
    monkeypatch.setattr(
        main,
        "get_onramp_intent_key_manager",
        lambda: onramp_intent_key_manager,
    )
    monkeypatch.setattr(main, "bootstrap_rofl_signer_address", bootstrap_rofl_signer_address)

    with pytest.raises(RuntimeError, match="sync failed"):
        async with main.lifespan(None):
            pass

    bootstrap_rofl_signer_address.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_aborts_when_onramp_intent_key_derivation_fails(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DISABLE_ROFL_KEYS", raising=False)

    jwt_key_manager = SimpleNamespace(initialize=AsyncMock())
    auth_token_key_manager = SimpleNamespace(
        initialize=AsyncMock(),
        sync_key_to_contract=AsyncMock(),
    )
    onramp_intent_key_manager = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError("intent key derivation failed"))
    )

    monkeypatch.setattr(main, "get_jwt_key_manager", lambda: jwt_key_manager)
    monkeypatch.setattr(main, "get_auth_token_key_manager", lambda: auth_token_key_manager)
    monkeypatch.setattr(
        main,
        "get_onramp_intent_key_manager",
        lambda: onramp_intent_key_manager,
    )

    with pytest.raises(RuntimeError, match="intent key derivation failed"):
        async with main.lifespan(None):
            pass

    auth_token_key_manager.sync_key_to_contract.assert_not_awaited()
