"""Tests for accounting API routes."""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3.exceptions import ContractCustomError

import src.api.routes as routes
import src.auth.dependencies as auth_dependencies
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


def _make_private_read_client(token: bytes = b"\x12\x34") -> TestClient:
    """Client with _require_private_read_auth overridden to a fixed token+user."""
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_private_read_auth] = lambda: routes.PrivateReadAuth(
        token=token, user_address=BENEFICIARY
    )
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
    client = _make_private_read_client(token=fake_token)

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


def test_get_history_route_wires_to_service_with_private_read_auth(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_history = AsyncMock(
        return_value={
            "history": [
                {
                    "kind": "deposit",
                    "timestamp": 1710000000,
                    "token_id": "0x" + "11" * 32,
                    "amount": "42",
                    "counterparty": None,
                    "deposit_id": "0x" + "dd" * 32,
                    "chain_id": 84532,
                },
                {
                    "kind": "createLock",
                    "timestamp": 1710000001,
                    "token_id": "0x" + "22" * 32,
                    "amount": "7",
                    "counterparty": "0x1234567890123456789012345678901234567890",
                    "deposit_id": None,
                    "chain_id": 84532,
                },
            ],
            "total": 2,
        }
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_private_read_client()
    response = client.get("/v1/accounting/history?offset=-1&limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["history"] == [
        {
            "kind": "deposit",
            "timestamp": 1710000000,
            "token_id": "0x" + "11" * 32,
            "amount": "42",
            "counterparty": None,
            "from_address": None,
            "to_address": None,
            "deposit_id": "0x" + "dd" * 32,
            "chain_id": 84532,
        },
        {
            "kind": "createLock",
            "timestamp": 1710000001,
            "token_id": "0x" + "22" * 32,
            "amount": "7",
            "counterparty": "0x1234567890123456789012345678901234567890",
            "from_address": None,
            "to_address": None,
            "deposit_id": None,
            "chain_id": 84532,
        },
    ]
    mock_service.get_history.assert_awaited_once_with(-1, 2, b"\x12\x34", BENEFICIARY)


def test_get_history_route_accepts_bearer_jwt(
    monkeypatch, reset_auth_singletons, disable_rofl_keys
) -> None:
    class _JwtService:
        def get_address_from_token(self, token: str) -> str:
            assert token == "jwt-token"
            return BENEFICIARY

    mock_service = MagicMock()
    mock_service.get_history = AsyncMock(return_value={"history": [], "total": 0})
    minted_token = b"\xab\xcd"
    mint_private_read_token = MagicMock(return_value=minted_token)

    monkeypatch.setattr(auth_dependencies, "get_jwt_service", lambda: _JwtService())
    monkeypatch.setattr(routes, "_mint_private_read_token", mint_private_read_token)
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_client()
    response = client.get(
        "/v1/accounting/history",
        headers={"Authorization": "Bearer jwt-token"},
    )

    assert response.status_code == 200
    mint_private_read_token.assert_called_once_with(BENEFICIARY)
    mock_service.get_history.assert_awaited_once_with(-1, 50, minted_token, BENEFICIARY)


def test_get_history_route_rejects_limit_above_max(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_history = AsyncMock(return_value={"history": [], "total": 0})
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_private_read_client()
    response = client.get("/v1/accounting/history?limit=101")

    assert response.status_code == 422
    mock_service.get_history.assert_not_called()


def test_get_history_route_invalid_siwe_token_returns_401(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_history = AsyncMock(
        side_effect=ContractCustomError("Unauthorized", data="0x82b42900")
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_private_read_client()
    response = client.get("/v1/accounting/history")

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired SIWE token"


def test_get_history_route_preserves_empty_pages(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_history = AsyncMock(return_value={"history": [], "total": 9})
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_private_read_client()
    response = client.get("/v1/accounting/history?offset=9&limit=0")

    assert response.status_code == 200
    assert response.json() == {"history": [], "total": 9}
    mock_service.get_history.assert_awaited_once_with(9, 0, b"\x12\x34", BENEFICIARY)


def test_deposit_status_route_rejects_empty_siwe_token(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.resolve_address_from_token = AsyncMock()
    monkeypatch.setattr(routes, "_service", mock_service)

    mock_processor = MagicMock(spec=DepositProcessor)
    monkeypatch.setattr(routes, "get_deposit_processor", lambda: mock_processor)

    client = _make_client()
    response = client.get(
        f"/v1/accounting/deposits/status/{DEPOSIT_ID_HEX}",
        headers={"X-SIWE-Token": "0x"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired SIWE token"
    mock_service.resolve_address_from_token.assert_not_called()
    mock_processor.get_deposit_status.assert_not_called()


def test_deposit_status_route_rejects_zero_siwe_user(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.resolve_address_from_token = AsyncMock(
        return_value="0x0000000000000000000000000000000000000000"
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    mock_processor = MagicMock(spec=DepositProcessor)
    monkeypatch.setattr(routes, "get_deposit_processor", lambda: mock_processor)

    client = _make_client()
    response = client.get(
        f"/v1/accounting/deposits/status/{DEPOSIT_ID_HEX}",
        headers={"X-SIWE-Token": "0x1234"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired SIWE token"
    mock_service.resolve_address_from_token.assert_awaited_once_with(b"\x12\x34")
    mock_processor.get_deposit_status.assert_not_called()


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
