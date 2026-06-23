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
    bootstrap_rofl_signer_address = AsyncMock()

    # Stub the unrelated bridge-settings gate that runs first in the lifespan;
    # otherwise it aborts on a missing ROFL_BRIDGE_ADDRESS before the auth-token
    # sync step under test is reached.
    monkeypatch.setattr(main, "validate_bridge_settings", lambda s: None)
    monkeypatch.setattr(main, "get_jwt_key_manager", lambda: jwt_key_manager)
    monkeypatch.setattr(main, "get_auth_token_key_manager", lambda: auth_token_key_manager)
    monkeypatch.setattr(main, "bootstrap_rofl_signer_address", bootstrap_rofl_signer_address)

    with pytest.raises(RuntimeError, match="sync failed"):
        async with main.lifespan(None):
            pass

    bootstrap_rofl_signer_address.assert_not_awaited()
