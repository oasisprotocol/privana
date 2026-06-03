"""Tests for the POST /bridge/withdrawals/quote route handler."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes
import src.services.bridge_quote as bridge_quote_module
from src.models.accounting import (
    BridgeWithdrawAdvisory,
    BridgeWithdrawEip712Envelope,
    BridgeWithdrawEip712Message,
    BridgeWithdrawQuoteResponse,
)
from src.services.bridge_quote import BridgeQuoteError

_USER = "0x1111111111111111111111111111111111111111"
_TO = "0x2222222222222222222222222222222222222222"
_ROUTE = "0x3333333333333333333333333333333333333333"
_ZERO = "0x" + "00" * 20


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _sapphire_quote() -> BridgeWithdrawQuoteResponse:
    return BridgeWithdrawQuoteResponse(
        dest_chain_id=23295,
        route_address=_ZERO,
        fee_model="native_gas_user_paid",
        gross_amount="1000000000000000000",
        max_gas_cost="31500000000000",
        net_amount="999968500000000000",
        user_nonce="3",
        advisory=BridgeWithdrawAdvisory(
            gas_price_seen_wei="1000000000",
            recommended_gas_limit="21000",
            safety_margin="1.5",
        ),
        quote_config_version="bridge-quote-v1:0x" + "ab" * 32,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        token_symbol="ROSE",
        token_decimals=18,
        eip712=BridgeWithdrawEip712Envelope(
            type="BridgeWithdraw",
            message=BridgeWithdrawEip712Message(
                userAddress=_USER,
                toAddress=_TO,
                destChainId="23295",
                routeAddress=_ZERO,
                amount="1000000000000000000",
                maxGasCost="31500000000000",
                nonce="3",
            ),
        ),
    )


def test_quote_route_returns_envelope(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.build_quote = AsyncMock(return_value=_sapphire_quote())
    monkeypatch.setattr(bridge_quote_module, "_bridge_quote_service", mock_service)

    client = _make_client()
    response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json={
            "user_address": _USER,
            "to_address": _TO,
            "dest_chain_id": 23295,
            "gross_amount": "1000000000000000000",
            "user_nonce": "0",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route_address"] == _ZERO
    assert body["fee_model"] == "native_gas_user_paid"
    assert body["max_gas_cost"] == "31500000000000"
    assert body["user_nonce"] == "3"
    assert body["advisory"]["safety_margin"] == "1.5"
    assert body["eip712"]["type"] == "BridgeWithdraw"
    assert list(body["eip712"]["message"].keys()) == [
        "userAddress",
        "toAddress",
        "destChainId",
        "routeAddress",
        "amount",
        "maxGasCost",
        "nonce",
    ]

    call_args = mock_service.build_quote.call_args
    submitted_request = call_args[0][0]
    assert submitted_request.user_address == _USER.lower()
    assert submitted_request.dest_chain_id == 23295
    assert submitted_request.gross_amount == 10**18


def test_quote_route_rejects_bridge_quote_error(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.build_quote = AsyncMock(
        side_effect=BridgeQuoteError("no route registered for destChainId=84532")
    )
    monkeypatch.setattr(bridge_quote_module, "_bridge_quote_service", mock_service)

    client = _make_client()
    response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json={
            "user_address": _USER,
            "to_address": _TO,
            "dest_chain_id": 84532,
            "gross_amount": "1000",
            "user_nonce": "0",
        },
    )

    assert response.status_code == 400
    assert "no route registered" in response.json()["detail"]


def test_quote_route_rejects_invalid_amount(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.build_quote = AsyncMock()
    monkeypatch.setattr(bridge_quote_module, "_bridge_quote_service", mock_service)

    client = _make_client()
    response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json={
            "user_address": _USER,
            "to_address": _TO,
            "dest_chain_id": 23295,
            "gross_amount": "0",
            "user_nonce": "0",
        },
    )

    assert response.status_code == 422
    mock_service.build_quote.assert_not_called()


def test_openapi_includes_quote_route() -> None:
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    assert "/v1/accounting/bridge/withdrawals/quote" in spec["paths"]
    quote_path = spec["paths"]["/v1/accounting/bridge/withdrawals/quote"]
    assert "post" in quote_path
    schemas = spec["components"]["schemas"]
    assert "BridgeWithdrawQuoteRequest" in schemas
    assert "BridgeWithdrawQuoteResponse" in schemas
    assert "BridgeWithdrawEip712Message" in schemas
