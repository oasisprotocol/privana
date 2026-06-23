"""Tests for the POST /bridge/withdrawals/submit route handler."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3.exceptions import ContractLogicError

import src.api.routes as routes
import src.services.bridge_submit as bridge_submit_module
from src.clients.rofl import TransactionRevertedError
from src.services.accounting_contract import SubmissionResult
from src.services.bridge_submit import BridgeSubmitError

_USER = "0x1111111111111111111111111111111111111111"
_TO = "0x2222222222222222222222222222222222222222"
_ZERO = "0x" + "00" * 20
_SIGNATURE = "0x" + "ab" * 65
_VALID_VERSION = "bridge-quote-v1:0x" + "cd" * 32


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _valid_payload() -> dict:
    return {
        "user_address": _USER,
        "to_address": _TO,
        "dest_chain_id": 23295,
        "route_address": _ZERO,
        "amount": "1000000000000000000",
        "max_gas_cost": "31500000000000",
        "quote_config_version": _VALID_VERSION,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        "user_nonce": "0",
        "signature": _SIGNATURE,
    }


def test_submit_route_returns_submission(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock(
        return_value=SubmissionResult(
            submission_id="rofl-xyz", status="submitted", detail="destChainId=23295"
        )
    )
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=_valid_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["submission_id"] == "rofl-xyz"
    assert body["status"] == "submitted"
    assert body["detail"] == "destChainId=23295"

    submitted = mock_service.submit.call_args[0][0]
    assert submitted.user_address == _USER.lower()
    assert submitted.dest_chain_id == 23295
    assert submitted.amount == 10**18


def test_submit_route_rejects_bridge_submit_error(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock(
        side_effect=BridgeSubmitError("quote_config_version mismatch — re-quote required")
    )
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=_valid_payload())

    assert response.status_code == 400
    assert "quote_config_version mismatch" in response.json()["detail"]


def test_submit_route_maps_contract_revert_to_422(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock(side_effect=ContractLogicError("execution reverted: bad sig"))
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=_valid_payload())

    assert response.status_code == 422
    assert "bad sig" in response.json()["detail"]


def test_submit_route_maps_transaction_reverted_to_422(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock(
        side_effect=TransactionRevertedError("revert: amount exceeds balance")
    )
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=_valid_payload())

    assert response.status_code == 422
    assert "amount exceeds balance" in response.json()["detail"]


def test_submit_route_maps_value_error_to_400(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock(side_effect=ValueError("malformed payload"))
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=_valid_payload())

    assert response.status_code == 400
    assert response.json()["detail"] == "malformed payload"


def test_submit_route_rejects_invalid_address(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock()
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    payload = _valid_payload()
    payload["user_address"] = "0xnotahexaddress"

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=payload)

    assert response.status_code == 422
    mock_service.submit.assert_not_called()


def test_submit_route_rejects_naive_expires_at(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock()
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    payload = _valid_payload()
    payload["expires_at"] = (
        (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(tzinfo=None).isoformat()
    )

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=payload)

    assert response.status_code == 422
    mock_service.submit.assert_not_called()


def test_submit_route_rejects_short_quote_config_version(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.submit = AsyncMock()
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", mock_service)

    payload = _valid_payload()
    payload["quote_config_version"] = "bridge-quote-v1:0xdeadbeef"

    client = _make_client()
    response = client.post("/v1/accounting/bridge/withdrawals/submit", json=payload)

    assert response.status_code == 422
    mock_service.submit.assert_not_called()


def test_openapi_includes_submit_route() -> None:
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()
    assert "/v1/accounting/bridge/withdrawals/submit" in spec["paths"]
    submit_path = spec["paths"]["/v1/accounting/bridge/withdrawals/submit"]
    assert "post" in submit_path
    schemas = spec["components"]["schemas"]
    assert "BridgeWithdrawSubmitRequest" in schemas
    assert "TransactionSubmissionResponse" in schemas
