"""Tests for accounting API routes."""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3.exceptions import ContractCustomError

import src.api.routes as routes
import src.auth.dependencies as auth_dependencies
import src.services.bridge_quote as bridge_quote_module
import src.services.bridge_submit as bridge_submit_module
from src.config.bridge import get_bridge_quote_config, quote_config_version
from src.models.private_read import PrivateReadAuth
from src.models.types import Settings
from src.services.accounting_contract import SubmissionResult
from src.services.bridge_quote import BridgeQuoteService
from src.services.bridge_submit import BridgeSubmitService
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
        return PrivateReadAuth(token=b"\x00" * 65, user_address=BENEFICIARY)

    app.dependency_overrides[routes._require_resolved_private_read_auth] = _override_auth
    return TestClient(app)


def _make_private_read_client(token: bytes = b"\x12\x34") -> TestClient:
    """Client with _require_private_read_auth overridden to a fixed token+user."""
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_private_read_auth] = lambda: PrivateReadAuth(
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
    called_auth = call_args[0][1]
    assert called_payload["signature"] == "0xabcd"
    assert called_payload["amount"] == 1000
    assert called_payload["nonce"] == 7
    assert called_auth.user_address == BENEFICIARY
    assert called_auth.token == fake_token


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
    ]
    mock_service.get_history.assert_awaited_once_with(-1, 2, b"\x12\x34")


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
    mock_service.get_history.assert_awaited_once_with(-1, 50, minted_token)


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
    mock_service.get_history.assert_awaited_once_with(9, 0, b"\x12\x34")


def test_deposit_status_route_resolves_siwe_token_user(monkeypatch) -> None:
    raw_token = b"\x12\x34\x56"
    resolved_user = "0x" + "33" * 20

    mock_service = MagicMock()
    mock_service.resolve_address_from_token = AsyncMock(return_value=resolved_user)
    monkeypatch.setattr(routes, "_service", mock_service)

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

    client = _make_client()
    response = client.get(
        f"/v1/accounting/deposits/status/{DEPOSIT_ID_HEX}",
        headers={"X-SIWE-Token": "0x123456"},
    )

    assert response.status_code == 200
    mock_service.resolve_address_from_token.assert_awaited_once_with(raw_token)
    mock_processor.get_deposit_status.assert_called_once_with(DEPOSIT_ID_HEX, resolved_user)


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


# Bridge route integration tests: quote/submit consistency.

_BRIDGE_USER = "0x1111111111111111111111111111111111111111"
_BRIDGE_TO = "0x2222222222222222222222222222222222222222"
_BRIDGE_ROUTE = "0x3333333333333333333333333333333333333333"
_BRIDGE_ZERO = "0x" + "00" * 20
_BRIDGE_SIGNATURE = "0x" + "ab" * 65
_SAPPHIRE_ID = 23295
_BASE_ID = 84532
_VALID_ADDR = "0x000000000000000000000000000000000000dEaD"


def _bridge_settings(**overrides) -> Settings:
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


def _make_bridge_services(
    settings_holder: list[Settings],
    *,
    nonce: int = 0,
    route_address: str = _BRIDGE_ROUTE,
    submission_id: str = "rofl-roundtrip",
    gas_price_wei: int = 1_000_000_000,
) -> tuple[BridgeQuoteService, BridgeSubmitService, MagicMock]:
    """Build a shared quote+submit pair backed by one mutable settings closure.

    Mutating ``settings_holder[0]`` between calls is the lever that proves
    both endpoints resolve their config through the same per-request callable.
    """
    accounting = MagicMock()
    accounting.get_withdrawal_nonce = AsyncMock(
        return_value={"user_address": _BRIDGE_USER, "nonce": nonce}
    )
    accounting.get_rofl_bridge_address = AsyncMock(return_value=route_address)
    accounting.request_bridge_withdrawal = AsyncMock(
        return_value=SubmissionResult(submission_id=submission_id, status="submitted", detail="ok")
    )
    accounting.get_gas_price = AsyncMock(return_value=gas_price_wei)

    def settings_provider() -> Settings:
        return settings_holder[0]

    quote_svc = BridgeQuoteService(settings_provider, accounting)
    submit_svc = BridgeSubmitService(settings_provider, accounting)
    return quote_svc, submit_svc, accounting


def _install_bridge_services(
    monkeypatch,
    quote_svc: BridgeQuoteService,
    submit_svc: BridgeSubmitService,
) -> None:
    monkeypatch.setattr(bridge_quote_module, "_bridge_quote_service", quote_svc)
    monkeypatch.setattr(bridge_submit_module, "_bridge_submit_service", submit_svc)


def _quote_request(*, dest_chain_id: int, gross_amount: str = "1000000000000000000") -> dict:
    return {
        "user_address": _BRIDGE_USER,
        "to_address": _BRIDGE_TO,
        "dest_chain_id": dest_chain_id,
        "gross_amount": gross_amount,
        "user_nonce": "0",
    }


def _build_submit_payload(
    quote_body: dict,
    *,
    signature: str = _BRIDGE_SIGNATURE,
    overrides: dict | None = None,
) -> dict:
    msg = quote_body["eip712"]["message"]
    payload = {
        "user_address": msg["userAddress"],
        "to_address": msg["toAddress"],
        "dest_chain_id": int(msg["destChainId"]),
        "route_address": msg["routeAddress"],
        "amount": msg["amount"],
        "max_gas_cost": msg["maxGasCost"],
        "quote_config_version": quote_body["quote_config_version"],
        "expires_at": quote_body["expires_at"],
        "user_nonce": msg["nonce"],
        "signature": signature,
    }
    if overrides:
        payload.update(overrides)
    return payload


def test_bridge_quote_route_sapphire_shape(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, _ = _make_bridge_services(settings_holder, nonce=3)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "dest_chain_id",
        "route_address",
        "fee_model",
        "gross_amount",
        "max_gas_cost",
        "net_amount",
        "user_nonce",
        "advisory",
        "quote_config_version",
        "expires_at",
        "token_symbol",
        "token_decimals",
        "eip712",
    }
    assert set(body["advisory"].keys()) == {
        "gas_price_seen_wei",
        "recommended_gas_limit",
        "safety_margin",
    }
    assert set(body["eip712"]["message"].keys()) == {
        "userAddress",
        "toAddress",
        "destChainId",
        "routeAddress",
        "amount",
        "maxGasCost",
        "nonce",
    }
    for key in ("gross_amount", "max_gas_cost", "net_amount", "user_nonce"):
        assert isinstance(body[key], str), f"{key} must serialize as string"
    assert body["route_address"] == _BRIDGE_ZERO
    assert body["user_nonce"] == "3"


def test_bridge_quote_route_rejects_amount_le_max_gas_cost(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, _ = _make_bridge_services(settings_holder)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID, gross_amount="1"),
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "gross_amount" in detail
    assert "max_gas_cost" in detail


def test_bridge_quote_route_base_shape(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, _ = _make_bridge_services(settings_holder)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_BASE_ID),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["max_gas_cost"] == "0"
    assert body["net_amount"] == body["gross_amount"]
    assert body["route_address"] == _BRIDGE_ROUTE.lower()
    assert re.match(r"^bridge-quote-v1:0x[0-9a-f]{64}$", body["quote_config_version"])
    datetime.fromisoformat(body["expires_at"])
    assert body["advisory"] is None
    assert set(body["eip712"]["message"].keys()) == {
        "userAddress",
        "toAddress",
        "destChainId",
        "routeAddress",
        "amount",
        "maxGasCost",
        "nonce",
    }


# Load-bearing: a quote-response payload must round-trip into a successful
# submit on both chain branches without the client mutating any field.
@pytest.mark.parametrize(
    "dest_chain_id,expected_route",
    [(_SAPPHIRE_ID, _BRIDGE_ZERO), (_BASE_ID, _BRIDGE_ROUTE.lower())],
)
def test_bridge_quote_to_submit_round_trip(monkeypatch, dest_chain_id, expected_route) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(settings_holder, nonce=0)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_response = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=dest_chain_id),
    )
    assert quote_response.status_code == 200
    quote_body = quote_response.json()
    assert quote_body["route_address"] == expected_route

    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body),
    )
    assert submit_response.status_code == 200
    body = submit_response.json()
    assert body["submission_id"] == "rofl-roundtrip"
    assert body["status"] == "submitted"

    accounting.request_bridge_withdrawal.assert_awaited_once()
    submitted = accounting.request_bridge_withdrawal.await_args[0][0]
    msg = quote_body["eip712"]["message"]
    assert submitted["user_address"] == _BRIDGE_USER
    assert submitted["to_address"] == _BRIDGE_TO
    assert submitted["dest_chain_id"] == dest_chain_id
    assert submitted["quote_config_version"] == quote_body["quote_config_version"]
    assert submitted["route_address"] == quote_body["route_address"]
    assert submitted["amount"] == int(msg["amount"])
    assert submitted["max_gas_cost"] == int(msg["maxGasCost"])
    assert submitted["user_nonce"] == int(msg["nonce"])


# Invariant: quote and submit must resolve config through the same per-request
# callable — bumping it between calls must reject.
def test_bridge_submit_rejects_after_config_bump(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(settings_holder, nonce=0)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_body = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID),
    ).json()
    original_version = quote_body["quote_config_version"]

    settings_holder[0] = _bridge_settings(
        chain_rpc_urls={
            _SAPPHIRE_ID: "https://example.invalid/sapphire",
            _BASE_ID: "https://example.invalid/base",
            11155111: "https://example.invalid/eth-sepolia",
        },
    )
    bumped_version = quote_config_version(get_bridge_quote_config(settings_holder[0]))
    assert bumped_version != original_version

    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body),
    )
    assert submit_response.status_code == 400
    assert "quote_config_version mismatch" in submit_response.json()["detail"]
    accounting.request_bridge_withdrawal.assert_not_called()


# Hash scoping: mutating a Settings field that is NOT in BridgeQuoteConfig
# must not flip quote_config_version, and submit must still round-trip.
# Pins that the hash inputs are narrowly the fields enumerated in
# get_bridge_quote_config(), not the whole Settings dataclass.
def test_bridge_non_config_settings_change_does_not_invalidate_quote(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(settings_holder, nonce=0)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_body = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID),
    ).json()
    original_version = quote_body["quote_config_version"]

    settings_holder[0] = _bridge_settings(bridge_mint_limit_wei=10**25)
    unchanged_version = quote_config_version(get_bridge_quote_config(settings_holder[0]))
    assert unchanged_version == original_version

    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body),
    )
    assert submit_response.status_code == 200
    accounting.request_bridge_withdrawal.assert_awaited_once()


def test_bridge_submit_route_rejects_expired_envelope(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(settings_holder, nonce=0)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_body = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID),
    ).json()

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body, overrides={"expires_at": expired}),
    )
    assert submit_response.status_code == 400
    assert "quote expired" in submit_response.json()["detail"]
    accounting.request_bridge_withdrawal.assert_not_called()


def test_bridge_submit_route_rejects_user_nonce_mismatch(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(settings_holder, nonce=0)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_body = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID),
    ).json()

    accounting.get_withdrawal_nonce = AsyncMock(
        return_value={"user_address": _BRIDGE_USER, "nonce": 7}
    )

    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body),
    )
    assert submit_response.status_code == 400
    assert "user_nonce mismatch" in submit_response.json()["detail"]
    accounting.request_bridge_withdrawal.assert_not_called()


def test_bridge_submit_route_rejects_unsupported_chain(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(settings_holder, nonce=0)
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_body = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_SAPPHIRE_ID),
    ).json()

    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body, overrides={"dest_chain_id": 999}),
    )
    assert submit_response.status_code == 400
    assert "not a registered destination" in submit_response.json()["detail"]
    accounting.request_bridge_withdrawal.assert_not_called()


def test_bridge_submit_route_rejects_missing_route(monkeypatch) -> None:
    settings_holder = [_bridge_settings()]
    quote_svc, submit_svc, accounting = _make_bridge_services(
        settings_holder, nonce=0, route_address=_BRIDGE_ROUTE
    )
    _install_bridge_services(monkeypatch, quote_svc, submit_svc)

    client = _make_client()
    quote_body = client.post(
        "/v1/accounting/bridge/withdrawals/quote",
        json=_quote_request(dest_chain_id=_BASE_ID),
    ).json()

    accounting.get_rofl_bridge_address = AsyncMock(return_value=_BRIDGE_ZERO)

    submit_response = client.post(
        "/v1/accounting/bridge/withdrawals/submit",
        json=_build_submit_payload(quote_body),
    )
    assert submit_response.status_code == 400
    assert "no route registered" in submit_response.json()["detail"]
    accounting.request_bridge_withdrawal.assert_not_called()


# Contract: bridge routes are public — EIP-712 signature is the auth mechanism.
# A future "require auth on all withdraw routes" change must not silently rewire this.
def test_bridge_routes_are_public() -> None:
    client = _make_client()
    quote_resp = client.post("/v1/accounting/bridge/withdrawals/quote", json={})
    submit_resp = client.post("/v1/accounting/bridge/withdrawals/submit", json={})
    assert quote_resp.status_code == 422
    assert submit_resp.status_code == 422


def test_bridge_openapi_contract() -> None:
    app = FastAPI()
    app.include_router(routes.router)
    spec = app.openapi()

    paths = spec["paths"]
    assert "/v1/accounting/bridge/withdrawals/quote" in paths
    assert "/v1/accounting/bridge/withdrawals/submit" in paths
    assert "post" in paths["/v1/accounting/bridge/withdrawals/quote"]
    assert "post" in paths["/v1/accounting/bridge/withdrawals/submit"]

    schemas = spec["components"]["schemas"]
    for name in (
        "BridgeWithdrawQuoteRequest",
        "BridgeWithdrawQuoteResponse",
        "BridgeWithdrawSubmitRequest",
        "BridgeWithdrawAdvisory",
        "BridgeWithdrawEip712Envelope",
        "TransactionSubmissionResponse",
    ):
        assert name in schemas, f"{name} missing from OpenAPI schema components"

    quote_resp_props = schemas["BridgeWithdrawQuoteResponse"]["properties"]
    for str_field in ("gross_amount", "max_gas_cost", "net_amount", "user_nonce"):
        assert quote_resp_props[str_field]["type"] == "string", (
            f"{str_field} must serialize as string"
        )
    assert "quote_config_version" in quote_resp_props

    advisory_props = schemas["BridgeWithdrawAdvisory"]["properties"]
    assert set(advisory_props.keys()) == {
        "gas_price_seen_wei",
        "recommended_gas_limit",
        "safety_margin",
    }
