"""Tests for accounting API routes."""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes
from src.services.accounting_contract import SubmissionResult
from src.services.deposit_processor import DepositProcessor

BENEFICIARY = "0x" + "bb" * 20
DEPOSIT_ID_HEX = "0x" + "dd" * 32


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _make_authed_client(monkeypatch) -> TestClient:
    """Create a client with auth dependency returning a fixed beneficiary."""
    app = FastAPI()
    app.include_router(routes.router)

    async def _override_auth():
        return (BENEFICIARY, b"\x00" * 65)

    app.dependency_overrides[routes._require_user_and_private_read_token] = _override_auth
    return TestClient(app)


def test_withdraw_from_lock_route_wires_to_service(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.withdraw_from_lock = AsyncMock(
        return_value=SubmissionResult(
            submission_id="sub-1", status="submitted", detail="chain_id=84532"
        )
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    fake_token = b"\xab" * 32
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_private_read_auth] = lambda: routes.PrivateReadAuth(
        token=fake_token, user_address=BENEFICIARY
    )
    client = TestClient(app)

    response = client.post(
        "/v1/accounting/funds/withdraw-from-lock",
        json={
            "to_address": "0x9876543210987654321098765432109876543210",
            "lock_id": 1,
            "amount": "1000",
            "nonce": "7",
            "signature": "abcd",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["submission_id"] == "sub-1"
    assert body["status"] == "submitted"
    assert body["detail"] == "chain_id=84532"
    call_args = mock_service.withdraw_from_lock.call_args
    called_payload = call_args[0][0]
    assert called_payload["signature"] == "0xabcd"
    assert called_payload["amount"] == 1000
    assert called_payload["nonce"] == 7
    assert call_args[0][1] == BENEFICIARY
    assert call_args[0][2] == fake_token


def test_withdraw_from_lock_route_rejects_missing_auth(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.withdraw_from_lock = AsyncMock()
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_client()
    response = client.post(
        "/v1/accounting/funds/withdraw-from-lock",
        json={
            "to_address": "0x9876543210987654321098765432109876543210",
            "lock_id": 1,
            "amount": "1000",
            "nonce": "7",
            "signature": "abcd",
        },
    )

    assert response.status_code == 401
    mock_service.withdraw_from_lock.assert_not_called()


# ── Deposit status polling endpoint ──────────────────────────────


def test_deposit_status_credited_on_chain(monkeypatch) -> None:
    """When deposit is processed on-chain and no local record, return credited."""
    mock_service = MagicMock()
    mock_service.is_deposit_processed = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "_service", mock_service)

    mock_processor = MagicMock(spec=DepositProcessor)
    mock_processor.get_deposit_status = MagicMock(return_value=None)
    monkeypatch.setattr(routes, "get_deposit_processor", lambda: mock_processor)

    client = _make_authed_client(monkeypatch)
    response = client.get(f"/v1/accounting/deposits/status/{DEPOSIT_ID_HEX}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "credited"
    assert body["deposit_id"] == DEPOSIT_ID_HEX


def test_deposit_status_pending_in_memory(monkeypatch) -> None:
    """When a sweep record exists in memory, return pending."""
    mock_processor = MagicMock(spec=DepositProcessor)
    mock_processor.get_deposit_status = MagicMock(
        return_value={
            "status": "pending",
            "deposit_id": DEPOSIT_ID_HEX,
            "amount": "50000000",
            "token_address": "0x" + "cc" * 20,
        }
    )
    monkeypatch.setattr(routes, "get_deposit_processor", lambda: mock_processor)

    client = _make_authed_client(monkeypatch)
    response = client.get(f"/v1/accounting/deposits/status/{DEPOSIT_ID_HEX}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["amount"] == "50000000"


def test_deposit_status_not_found(monkeypatch) -> None:
    """When no local record and not on-chain, return 404."""
    mock_service = MagicMock()
    mock_service.is_deposit_processed = AsyncMock(return_value=False)
    monkeypatch.setattr(routes, "_service", mock_service)

    mock_processor = MagicMock(spec=DepositProcessor)
    mock_processor.get_deposit_status = MagicMock(return_value=None)
    monkeypatch.setattr(routes, "get_deposit_processor", lambda: mock_processor)

    client = _make_authed_client(monkeypatch)
    response = client.get(f"/v1/accounting/deposits/status/{DEPOSIT_ID_HEX}")

    assert response.status_code == 404
