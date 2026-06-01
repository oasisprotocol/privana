"""Tests for ``src/services/bridge_quote.py`` — quote builder service."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.bridge import (
    MAX_SAPPHIRE_RELEASE_RESERVE_WEI,
    SAPPHIRE_RELEASE_GAS_LIMIT,
    SAPPHIRE_RELEASE_GAS_SAFETY_MARGIN_BPS,
)
from src.config.bridge_validation import BASIS_POINTS_DENOMINATOR
from src.models.accounting import BridgeWithdrawQuoteRequest
from src.models.types import Settings
from src.services.bridge_quote import BridgeQuoteError, BridgeQuoteService

_VALID_ADDR = "0x000000000000000000000000000000000000dEaD"
_USER = "0x1111111111111111111111111111111111111111"
_TO = "0x2222222222222222222222222222222222222222"
_ROUTE = "0x3333333333333333333333333333333333333333"
_ZERO = "0x" + "00" * 20

_SAPPHIRE_ID = 23295
_BASE_ID = 84532


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


def _service(
    *,
    nonce: int = 0,
    route_address: str = _ROUTE,
    gas_price_wei: int = 1_000_000_000,
    settings_overrides: dict | None = None,
) -> BridgeQuoteService:
    settings = _settings(**(settings_overrides or {}))
    accounting = MagicMock()
    accounting.get_withdrawal_nonce = AsyncMock(
        return_value={"user_address": _USER, "nonce": nonce}
    )
    accounting.get_rofl_bridge_address = AsyncMock(return_value=route_address)
    accounting.get_gas_price = AsyncMock(return_value=gas_price_wei)
    return BridgeQuoteService(lambda: settings, accounting)


def _request(**overrides) -> BridgeWithdrawQuoteRequest:
    base = dict(
        user_address=_USER,
        to_address=_TO,
        dest_chain_id=_SAPPHIRE_ID,
        gross_amount=10**18,
        user_nonce=0,
    )
    base.update(overrides)
    return BridgeWithdrawQuoteRequest(**base)


@pytest.mark.asyncio
async def test_sapphire_branch_envelope():
    svc = _service(nonce=3)
    quote = await svc.build_quote(_request(dest_chain_id=_SAPPHIRE_ID))

    assert quote.route_address == _ZERO
    assert quote.fee_model == "native_gas_user_paid"
    assert int(quote.max_gas_cost) > 0
    assert int(quote.net_amount) == int(quote.gross_amount) - int(quote.max_gas_cost)
    assert quote.advisory is not None
    assert quote.advisory.gas_price_seen_wei == "1000000000"
    assert quote.advisory.recommended_gas_limit == "25000"
    assert quote.advisory.safety_margin == "1.1"
    assert quote.token_symbol == "ROSE"
    assert quote.token_decimals == 18


@pytest.mark.asyncio
async def test_registered_branch_envelope():
    svc = _service(nonce=7, route_address=_ROUTE)
    quote = await svc.build_quote(_request(dest_chain_id=_BASE_ID))

    assert quote.route_address == _ROUTE.lower()
    assert quote.fee_model == "foreign_gas_operator_paid"
    assert quote.max_gas_cost == "0"
    assert quote.net_amount == quote.gross_amount
    assert quote.advisory is None
    assert quote.user_nonce == "7"


@pytest.mark.asyncio
async def test_unknown_dest_chain_rejected():
    svc = _service()
    with pytest.raises(BridgeQuoteError, match="not a registered destination"):
        await svc.build_quote(_request(dest_chain_id=999))


@pytest.mark.asyncio
async def test_route_not_registered_rejected():
    svc = _service(route_address=_ZERO)
    with pytest.raises(BridgeQuoteError, match="no route registered"):
        await svc.build_quote(_request(dest_chain_id=_BASE_ID))


@pytest.mark.asyncio
async def test_gross_le_max_gas_cost_rejected():
    # Reserve at default settings = 25000 * 1 gwei * 1.1 = 2.75e13 wei
    svc = _service(gas_price_wei=1_000_000_000)
    with pytest.raises(BridgeQuoteError, match="gross_amount must exceed"):
        await svc.build_quote(_request(dest_chain_id=_SAPPHIRE_ID, gross_amount=10_000))


@pytest.mark.asyncio
async def test_reserve_capped_at_max():
    # Safe-but-tight zone: just below 400 gwei the bare release cost
    # (25000 * observed) still fits under the 1e16 cap, but the
    # safety-margin'd recommendation exceeds it, so min(...) clamps to MAX.
    # 25000 * 399 gwei = 9.975e15 <= 1e16 (resolvable);
    # recommended = 9.975e15 * 1.1 = 1.09725e16 > 1e16 -> clamps.
    observed = 399 * 10**9
    assert SAPPHIRE_RELEASE_GAS_LIMIT * observed <= MAX_SAPPHIRE_RELEASE_RESERVE_WEI
    recommended = (
        SAPPHIRE_RELEASE_GAS_LIMIT
        * observed
        * SAPPHIRE_RELEASE_GAS_SAFETY_MARGIN_BPS
        // BASIS_POINTS_DENOMINATOR
    )
    assert recommended > MAX_SAPPHIRE_RELEASE_RESERVE_WEI
    svc = _service(gas_price_wei=observed)
    quote = await svc.build_quote(_request(dest_chain_id=_SAPPHIRE_ID, gross_amount=10**18))
    assert int(quote.max_gas_cost) == min(recommended, MAX_SAPPHIRE_RELEASE_RESERVE_WEI)
    assert int(quote.max_gas_cost) == MAX_SAPPHIRE_RELEASE_RESERVE_WEI
    # Advisory reports the *observed* gas price, not the post-cap effective.
    assert quote.advisory is not None
    assert quote.advisory.gas_price_seen_wei == str(observed)


@pytest.mark.asyncio
async def test_bare_release_cost_above_cap_rejected():
    # Above ~400 gwei the bare native-release cost (25000 * observed) alone
    # exceeds the 1e16 reserve cap. resolveSign would then revert
    # GasBudgetExceeded forever, so the quote must be rejected up front
    # rather than debiting funds for an unresolvable withdrawal.
    observed = 401 * 10**9
    assert SAPPHIRE_RELEASE_GAS_LIMIT * observed > MAX_SAPPHIRE_RELEASE_RESERVE_WEI
    svc = _service(gas_price_wei=observed)
    with pytest.raises(BridgeQuoteError, match="never resolvable"):
        await svc.build_quote(_request(dest_chain_id=_SAPPHIRE_ID, gross_amount=10**18))


@pytest.mark.asyncio
async def test_user_nonce_from_chain_not_request():
    svc = _service(nonce=7)
    quote = await svc.build_quote(_request(user_nonce=99, dest_chain_id=_BASE_ID))
    assert quote.user_nonce == "7"
    assert quote.eip712.message.nonce == "7"


@pytest.mark.asyncio
async def test_user_nonce_from_chain_on_sapphire_branch():
    svc = _service(nonce=11)
    quote = await svc.build_quote(_request(user_nonce=99, dest_chain_id=_SAPPHIRE_ID))
    assert quote.user_nonce == "11"
    assert quote.eip712.message.nonce == "11"


@pytest.mark.asyncio
async def test_quote_config_version_stable_across_route_rotation():
    # Two quotes with different routes returned by the chain must share the
    # same quote_config_version — route rotations do not stale quotes.
    svc_a = _service(route_address=_ROUTE)
    svc_b = _service(route_address="0x" + "ab" * 20)
    quote_a = await svc_a.build_quote(_request(dest_chain_id=_BASE_ID))
    quote_b = await svc_b.build_quote(_request(dest_chain_id=_BASE_ID))
    assert quote_a.route_address != quote_b.route_address
    assert quote_a.quote_config_version == quote_b.quote_config_version


@pytest.mark.asyncio
async def test_eip712_message_field_order():
    svc = _service()
    quote = await svc.build_quote(_request())
    dumped = quote.eip712.message.model_dump()
    assert list(dumped.keys()) == [
        "userAddress",
        "toAddress",
        "destChainId",
        "routeAddress",
        "amount",
        "maxGasCost",
        "nonce",
    ]


@pytest.mark.asyncio
async def test_eip712_message_mirrors_envelope():
    svc = _service(nonce=4)
    quote = await svc.build_quote(_request(dest_chain_id=_SAPPHIRE_ID, gross_amount=10**18))
    msg = quote.eip712.message
    assert msg.userAddress == _USER.lower()
    assert msg.toAddress == _TO
    assert msg.destChainId == str(_SAPPHIRE_ID)
    assert msg.routeAddress == quote.route_address
    assert msg.amount == quote.gross_amount
    assert msg.maxGasCost == quote.max_gas_cost
    assert msg.nonce == quote.user_nonce


@pytest.mark.asyncio
async def test_quote_config_version_present():
    svc = _service()
    quote = await svc.build_quote(_request())
    assert quote.quote_config_version.startswith("bridge-quote-v1:0x")
    assert len(quote.quote_config_version) == len("bridge-quote-v1:0x") + 64
