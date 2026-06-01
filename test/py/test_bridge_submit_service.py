"""Tests for ``src/services/bridge_submit.py`` — submit validator service."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.bridge import get_bridge_quote_config, quote_config_version
from src.models.accounting import BridgeWithdrawSubmitRequest
from src.models.types import Settings
from src.services.accounting_contract import SubmissionResult
from src.services.bridge_submit import BridgeSubmitError, BridgeSubmitService

_VALID_ADDR = "0x000000000000000000000000000000000000dEaD"
_USER = "0x1111111111111111111111111111111111111111"
_TO = "0x2222222222222222222222222222222222222222"
_ROUTE = "0x3333333333333333333333333333333333333333"
_ROTATED_ROUTE = "0x4444444444444444444444444444444444444444"
_ZERO = "0x" + "00" * 20
_SIGNATURE = "0x" + "ab" * 65

_SAPPHIRE_ID = 23295
_BASE_ID = 84532

_DEFAULT_AMOUNT = 10**18
_DEFAULT_MAX_GAS_COST = 31_500_000_000_000  # 21000 * 1 gwei * 1.5


def _settings(**overrides) -> Settings:
    base = dict(
        rofl_bridge_address=_VALID_ADDR,
        xrose_address=_VALID_ADDR,
        bridge_mint_limit_wei=10**24,
        bridge_burn_limit_wei=10**24,
        sapphire_chain_id=_SAPPHIRE_ID,
        chain_rpc_urls={
            _SAPPHIRE_ID: "https://example.invalid/sapphire",
            _BASE_ID: "https://example.invalid/base",
        },
    )
    base.update(overrides)
    return Settings(**base)


def _valid_version(settings: Settings) -> str:
    return quote_config_version(get_bridge_quote_config(settings))


def _service(
    *,
    nonce: int = 0,
    route_address: str = _ROUTE,
    settings_overrides: dict | None = None,
    submission_id: str = "rofl-abc",
) -> tuple[BridgeSubmitService, MagicMock, Settings]:
    settings = _settings(**(settings_overrides or {}))
    accounting = MagicMock()
    accounting.get_withdrawal_nonce = AsyncMock(
        return_value={"user_address": _USER, "nonce": nonce}
    )
    accounting.get_rofl_bridge_address = AsyncMock(return_value=route_address)
    accounting.request_bridge_withdrawal = AsyncMock(
        return_value=SubmissionResult(submission_id=submission_id, status="submitted", detail="ok")
    )
    return BridgeSubmitService(lambda: settings, accounting), accounting, settings


def _request(settings: Settings, **overrides) -> BridgeWithdrawSubmitRequest:
    base = dict(
        user_address=_USER,
        to_address=_TO,
        dest_chain_id=_SAPPHIRE_ID,
        route_address=_ZERO,
        amount=_DEFAULT_AMOUNT,
        max_gas_cost=_DEFAULT_MAX_GAS_COST,
        quote_config_version=_valid_version(settings),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        user_nonce=0,
        signature=_SIGNATURE,
    )
    base.update(overrides)
    return BridgeWithdrawSubmitRequest(**base)


@pytest.mark.asyncio
async def test_sapphire_branch_submits():
    svc, accounting, settings = _service(nonce=3)
    req = _request(settings, user_nonce=3)
    result = await svc.submit(req)

    assert result.submission_id == "rofl-abc"
    accounting.request_bridge_withdrawal.assert_awaited_once()
    submitted = accounting.request_bridge_withdrawal.await_args[0][0]
    expected_keys = {
        "user_address",
        "to_address",
        "dest_chain_id",
        "route_address",
        "amount",
        "max_gas_cost",
        "quote_config_version",
        "expires_at",
        "user_nonce",
        "signature",
    }
    assert expected_keys.issubset(submitted.keys())
    assert submitted["dest_chain_id"] == _SAPPHIRE_ID
    assert submitted["route_address"] == _ZERO
    assert submitted["user_nonce"] == 3
    assert submitted["signature"] == _SIGNATURE


@pytest.mark.asyncio
async def test_base_branch_submits():
    svc, accounting, settings = _service(nonce=5, route_address=_ROUTE)
    req = _request(
        settings,
        dest_chain_id=_BASE_ID,
        route_address=_ROUTE,
        max_gas_cost=0,
        user_nonce=5,
    )
    result = await svc.submit(req)

    assert result.submission_id == "rofl-abc"
    accounting.request_bridge_withdrawal.assert_awaited_once()
    submitted = accounting.request_bridge_withdrawal.await_args[0][0]
    assert submitted["dest_chain_id"] == _BASE_ID
    assert submitted["route_address"] == _ROUTE.lower()
    assert submitted["max_gas_cost"] == 0


@pytest.mark.asyncio
async def test_stale_quote_config_version_rejected():
    svc, accounting, settings = _service()
    req = _request(settings, quote_config_version="bridge-quote-v1:0x" + "00" * 32)

    with pytest.raises(BridgeSubmitError, match="quote_config_version mismatch"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_expired_envelope_rejected():
    svc, accounting, settings = _service()
    req = _request(settings, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    with pytest.raises(BridgeSubmitError, match="quote expired"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_dest_chain_rejected():
    svc, accounting, settings = _service()
    req = _request(settings, dest_chain_id=999, route_address=_ROUTE, max_gas_cost=0)

    with pytest.raises(BridgeSubmitError, match="not a registered destination"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_route_mismatch_rejected():
    # Chain reports a route different from the one the user signed against.
    svc, accounting, settings = _service(route_address=_ROTATED_ROUTE, nonce=0)
    req = _request(
        settings,
        dest_chain_id=_BASE_ID,
        route_address=_ROUTE,
        max_gas_cost=0,
    )

    with pytest.raises(BridgeSubmitError, match="route_address has rotated"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_route_not_registered_rejected():
    svc, accounting, settings = _service(route_address=_ZERO)
    req = _request(
        settings,
        dest_chain_id=_BASE_ID,
        route_address=_ROUTE,
        max_gas_cost=0,
    )

    with pytest.raises(BridgeSubmitError, match="no route registered"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_hex_route_from_chain_rejected():
    svc, accounting, settings = _service(route_address="not-a-hex-string")
    req = _request(
        settings,
        dest_chain_id=_BASE_ID,
        route_address=_ROUTE,
        max_gas_cost=0,
    )

    with pytest.raises(BridgeSubmitError, match="invalid route address"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_sapphire_route_address_must_be_zero():
    svc, accounting, settings = _service()
    req = _request(settings, route_address=_ROUTE)  # Sapphire branch, non-zero route

    with pytest.raises(BridgeSubmitError, match="route_address has rotated"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_user_nonce_mismatch_rejected():
    svc, accounting, settings = _service(nonce=5)
    req = _request(settings, user_nonce=4)

    with pytest.raises(BridgeSubmitError, match="user_nonce mismatch"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_max_gas_cost_above_cap_sapphire_rejected():
    # Cap is MAX_SAPPHIRE_RELEASE_RESERVE_WEI = 1e16 wei.
    svc, accounting, settings = _service()
    req = _request(settings, max_gas_cost=10**17)

    with pytest.raises(BridgeSubmitError, match="max_gas_cost"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_max_gas_cost_zero_on_sapphire_rejected():
    svc, accounting, settings = _service()
    req = _request(settings, max_gas_cost=0)

    with pytest.raises(BridgeSubmitError, match="max_gas_cost"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_amount_le_max_gas_cost_sapphire_rejected():
    svc, accounting, settings = _service()
    req = _request(settings, amount=_DEFAULT_MAX_GAS_COST)  # amount == max_gas_cost

    with pytest.raises(BridgeSubmitError, match="amount must exceed max_gas_cost"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()


@pytest.mark.asyncio
async def test_max_gas_cost_nonzero_on_registered_rejected():
    svc, accounting, settings = _service(nonce=0, route_address=_ROUTE)
    req = _request(
        settings,
        dest_chain_id=_BASE_ID,
        route_address=_ROUTE,
        max_gas_cost=1,
    )

    with pytest.raises(BridgeSubmitError, match="max_gas_cost must be 0"):
        await svc.submit(req)
    accounting.request_bridge_withdrawal.assert_not_called()
