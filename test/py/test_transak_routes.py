"""Route coverage for provider selection and the Transak backend integration."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3

import src.api.routes as routes
import src.auth.rate_limiter as rate_limiter
import src.config
import src.services.onramp as onramp
import src.services.onramp_intent as onramp_intent
import src.services.transak as transak
from src.models.private_read import PrivateReadAuth

BENEFICIARY = Web3.to_checksum_address("0x" + "bb" * 20)
OTHER_BENEFICIARY = Web3.to_checksum_address("0x" + "dd" * 20)
DEPOSIT_ADDRESS = Web3.to_checksum_address("0x" + "aa" * 20)
TOKEN_ADDRESS = Web3.to_checksum_address("0x" + "cc" * 20)
TOKEN_ID = "0x" + "11" * 32
TX_HASH = "0x" + "22" * 32
PRIVATE_READ_TOKEN = b"\x12" * 65
RESOLVED_PRIVATE_READ_TOKEN = b"\x34" * 65
INTENT_SIGNING_KEY = b"route-transak-intent-signing-key-0001"
INTENT_SIGNING_KEY_ID = "onramp_intent_signing_key.v1.key"


def _set_required_env(monkeypatch, tmp_path, *, provider: str = "transak") -> None:
    env = {
        "API_HOST": "0.0.0.0",
        "API_PORT": "8000",
        "LOG_LEVEL": "INFO",
        "ENVIRONMENT": "test",
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
        "ACCOUNTING_CONTRACT_ADDRESS": "0x" + "01" * 20,
        "SAPPHIRE_CHAIN_ID": "23295",
        "SAPPHIRE_RPC_URL": "https://testnet.sapphire.oasis.io",
        "ACCOUNTING_GAS_LIMIT": "500000",
        "WITHDRAWAL_POLL_INTERVAL": "12",
        "WITHDRAWAL_RESOLUTION_TIMEOUT": "60",
        "MIN_WITHDRAWAL_GAS_BALANCE": "10000000000000",
        "AUTH_TOKEN_VALIDITY_SECONDS": "86400",
        "AUTH_TOKEN_STORAGE_DIR": str(tmp_path / "auth_store"),
        "SIWE_DOMAINS": "http://localhost:3000",
        "AUTH_CLIENTS": "[]",
        "AUTH_CODE_TTL_SECONDS": "120",
        "AUTH_RATE_LIMIT_WINDOW_SECONDS": "60",
        "AUTH_NONCE_RATE_LIMIT": "30",
        "AUTH_LOGIN_RATE_LIMIT": "10",
        "AUTH_AUTHORIZE_RATE_LIMIT": "10",
        "AUTH_TOKEN_RATE_LIMIT": "20",
        "TRUST_X_FORWARDED_FOR": "false",
        "ONRAMP_PROVIDER": provider,
        "ONRAMP_INTENT_SIGNING_KEY_ID": INTENT_SIGNING_KEY_ID,
        "ONRAMP_INTENT_PREVIOUS_SIGNING_KEY_IDS": "",
        "MOONPAY_API_KEY": "pk_test_key",
        "MOONPAY_SECRET_KEY": "sk_test_key",
        "MOONPAY_API_BASE_URL": "https://api.moonpay.com",
        "MOONPAY_WEBHOOK_SECRET_KEY": "wh_test_key",
        "MOONPAY_ALLOWED_HOSTS": "buy.moonpay.com,buy-sandbox.moonpay.com",
        "MOONPAY_ALLOWED_CURRENCY_CODES": "usdc",
        "MOONPAY_WEBHOOK_TOLERANCE_SECONDS": "300",
        "TRANSAK_API_KEY": "transak-api-key",
        "TRANSAK_API_SECRET": "transak-api-secret",
        "TRANSAK_API_BASE_URL": "https://api-stg.transak.test",
        "TRANSAK_GATEWAY_BASE_URL": "https://gateway-stg.transak.test",
        "TRANSAK_REFERRER_DOMAIN": "app.testnet.privana.finance",
        "TRANSAK_CLIENT_IP_HEADER": "x-original-user-ip",
        "TRANSAK_CRYPTO_CURRENCY_CODE": "USDC",
        "TRANSAK_NETWORK": "base",
        "TRANSAK_CHAIN_ID": "84532",
        "TRANSAK_TOKEN_ADDRESS": TOKEN_ADDRESS,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    src.config._settings = None
    if rate_limiter._auth_rate_limiter_instance is not None:
        rate_limiter._auth_rate_limiter_instance.close()
    rate_limiter._auth_rate_limiter_instance = None
    transak._transak_service_instance = None
    manager = onramp_intent.OnRampIntentKeyManager()
    manager._current_key = INTENT_SIGNING_KEY
    manager._verification_keys = (INTENT_SIGNING_KEY,)
    onramp_intent._onramp_intent_key_manager_instance = manager


def _make_client(
    monkeypatch,
    tmp_path,
    *,
    provider: str = "transak",
    user_address: str = BENEFICIARY,
) -> tuple[TestClient, MagicMock, SimpleNamespace]:
    _set_required_env(monkeypatch, tmp_path, provider=provider)
    accounting = MagicMock()
    accounting.get_deposit_address = AsyncMock(return_value=DEPOSIT_ADDRESS)
    accounting.get_token_id = AsyncMock(return_value=bytes.fromhex(TOKEN_ID.removeprefix("0x")))
    accounting.is_token_registered = AsyncMock(return_value=True)
    monkeypatch.setattr(routes, "_service", accounting)
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        AsyncMock(return_value=[]),
    )
    provider_service = SimpleNamespace(
        create_widget_session=AsyncMock(
            return_value={
                "provider": "transak",
                "url": "https://global-stg.transak.test?sessionId=opaque",
                "expires_at": 1_800_000_300,
            }
        ),
        get_orders_by_partner_order_id=AsyncMock(return_value=[]),
        get_orders_by_wallet=AsyncMock(return_value=[]),
        verify_webhook=AsyncMock(
            return_value={"eventID": "ORDER_COMPLETED", "webhookData": {"id": "order-1"}}
        ),
    )
    monkeypatch.setattr(routes, "get_transak_service", lambda: provider_service)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_private_read_auth] = lambda: PrivateReadAuth(
        token=PRIVATE_READ_TOKEN,
        user_address=user_address,
    )
    app.dependency_overrides[routes._require_resolved_private_read_auth] = lambda: PrivateReadAuth(
        token=RESOLVED_PRIVATE_READ_TOKEN,
        user_address=user_address,
    )
    return TestClient(app), accounting, provider_service


def _create_intent(client: TestClient, **overrides) -> dict:
    payload = {"token_id": TOKEN_ID, "chain_id": 84532}
    payload.update(overrides)
    response = client.post("/v1/accounting/onramp/intent", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _order(transaction_id: str, **overrides) -> dict:
    order = {
        "_id": "transak-order-1",
        "partnerOrderId": transaction_id,
        "walletAddress": DEPOSIT_ADDRESS,
        "status": "COMPLETED",
        "cryptoCurrency": "USDC",
        "network": "base",
        "isBuyOrSell": "BUY",
        "transactionHash": TX_HASH,
        "createdAt": "2026-07-23T10:00:00.000Z",
        "updatedAt": "2026-07-23T10:01:00.000Z",
        "fiatCurrency": "USD",
        "fiatAmount": 100,
        "cryptoAmount": "99.5",
    }
    order.update(overrides)
    return order


def _moonpay_transaction(transaction_id: str) -> dict:
    return {
        "id": "moonpay-order-1",
        "externalTransactionId": transaction_id,
        "walletAddress": DEPOSIT_ADDRESS,
        "status": "completed",
        "currency": {"code": "usdc"},
        "cryptoTransactionId": TX_HASH,
        "createdAt": "2026-07-23T10:00:00.000Z",
        "updatedAt": "2026-07-23T10:01:00.000Z",
    }


def test_transak_intent_is_provider_selected_and_registry_bound(monkeypatch, tmp_path) -> None:
    client, accounting, _provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    decoded = onramp_intent.decode_intent(intent["transaction_id"])

    assert intent["provider"] == "transak"
    assert intent["provider_asset_code"] == "usdc"
    assert intent["moonpay_currency_code"] is None
    assert decoded["p"] == "transak"
    assert decoded["a"] == "usdc"
    accounting.get_token_id.assert_awaited_once_with(84532, TOKEN_ADDRESS)
    accounting.is_token_registered.assert_awaited_once()


def test_transak_intent_rejects_wrong_or_unregistered_configured_token(
    monkeypatch, tmp_path
) -> None:
    client, accounting, _provider_service = _make_client(monkeypatch, tmp_path)
    wrong = client.post(
        "/v1/accounting/onramp/intent",
        json={"token_id": "0x" + "33" * 32, "chain_id": 84532},
    )
    assert wrong.status_code == 400

    accounting.is_token_registered.return_value = False
    unregistered = client.post(
        "/v1/accounting/onramp/intent",
        json={"token_id": TOKEN_ID, "chain_id": 84532},
    )
    assert unregistered.status_code == 503


def test_invalid_transak_config_does_not_block_startup_and_returns_503(
    monkeypatch, tmp_path
) -> None:
    client, _accounting, _provider_service = _make_client(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSAK_CHAIN_ID", "not-an-integer")
    src.config._settings = None
    assert src.config.load_settings().transak_chain_id is None

    response = client.post(
        "/v1/accounting/onramp/intent",
        json={"token_id": TOKEN_ID, "chain_id": 84532},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Transak on-ramp is not configured"


def test_missing_provider_config_fails_closed(monkeypatch, tmp_path) -> None:
    client, _accounting, _provider_service = _make_client(monkeypatch, tmp_path)
    monkeypatch.delenv("ONRAMP_PROVIDER")
    src.config._settings = None

    assert src.config.load_settings().onramp_provider is None
    response = client.post(
        "/v1/accounting/onramp/intent",
        json={"token_id": TOKEN_ID, "chain_id": 84532},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "On-ramp provider configuration is invalid"


def test_session_uses_trusted_ip_and_returns_only_opaque_state(monkeypatch, tmp_path) -> None:
    client, accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    response = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"]},
        headers={"x-original-user-ip": "203.0.113.4"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "transak",
        "url": "https://global-stg.transak.test?sessionId=opaque",
        "expires_at": 1_800_000_300,
    }
    provider_service.create_widget_session.assert_awaited_once()
    kwargs = provider_service.create_widget_session.await_args.kwargs
    assert kwargs["transaction_id"] == intent["transaction_id"]
    assert kwargs["wallet_address"] == DEPOSIT_ADDRESS
    assert kwargs["user_ip"] == "203.0.113.4"
    assert accounting.get_deposit_address.await_count == 2


def test_session_rejects_missing_or_ambiguous_trusted_ip(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    missing = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"]},
    )
    ambiguous = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"]},
        headers={"x-original-user-ip": "203.0.113.4, 198.51.100.2"},
    )
    assert missing.status_code == 400
    assert ambiguous.status_code == 400
    provider_service.create_widget_session.assert_not_awaited()


ATTESTATION_SECRET = "route-attestation-shared-secret-0123456789"


def _make_attested_client(monkeypatch, tmp_path):
    client, accounting, provider_service = _make_client(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSAK_CLIENT_IP_MODE", "attested")
    monkeypatch.setenv("TRANSAK_IP_ATTESTATION_SECRET", ATTESTATION_SECRET)
    monkeypatch.delenv("TRANSAK_CLIENT_IP_HEADER")
    src.config._settings = None
    monkeypatch.setattr(transak, "_ip_attestation_nonces", {})
    return client, accounting, provider_service


def _ip_attestation(
    transaction_id: str,
    ip: str = "8.8.8.8",
    *,
    nonce: str = "0123456789abcdef0123456789abcdef",
) -> dict:
    issued_at = int(time.time())
    expires_at = issued_at + 60
    intent_hash = hashlib.sha256(transaction_id.encode()).hexdigest()
    payload = "|".join(
        (
            "v1",
            "app.testnet.privana.finance",
            intent_hash,
            ip,
            str(issued_at),
            str(expires_at),
            nonce,
        )
    )
    signature = hmac.new(ATTESTATION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {
        "v": 1,
        "ip": ip,
        "iat": issued_at,
        "exp": expires_at,
        "nonce": nonce,
        "sig": signature,
    }


def test_attested_session_verifies_edge_claim_without_header(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_attested_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    response = client.post(
        "/v1/accounting/onramp/session",
        json={
            "transaction_id": intent["transaction_id"],
            "ip_attestation": _ip_attestation(intent["transaction_id"]),
        },
    )
    assert response.status_code == 200, response.text
    kwargs = provider_service.create_widget_session.await_args.kwargs
    assert kwargs["user_ip"] == "8.8.8.8"


def test_attested_session_rejects_missing_tampered_or_replayed_claims(
    monkeypatch, tmp_path
) -> None:
    client, _accounting, provider_service = _make_attested_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    missing = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"]},
        headers={"x-original-user-ip": "8.8.8.8"},
    )
    assert missing.status_code == 400
    assert missing.json()["detail"] == "Client IP attestation is required"

    tampered = _ip_attestation(intent["transaction_id"])
    tampered["ip"] = "198.51.100.7"
    rejected = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"], "ip_attestation": tampered},
    )
    assert rejected.status_code == 400
    provider_service.create_widget_session.assert_not_awaited()

    claim = _ip_attestation(intent["transaction_id"])
    first = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"], "ip_attestation": claim},
    )
    replayed = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": intent["transaction_id"], "ip_attestation": claim},
    )
    assert first.status_code == 200
    assert replayed.status_code == 400
    assert replayed.json()["detail"] == "Client IP attestation was already used"
    provider_service.create_widget_session.assert_awaited_once()


def test_attested_session_rejects_invalid_claim_fields(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_attested_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    for mutation in (
        {"v": True},
        {"v": 1.0},
        {"iat": str(int(time.time()))},
        {"exp": float(int(time.time()) + 60)},
        {"nonce": "AB" * 16},
        {"unexpected": "field"},
    ):
        claim = {**_ip_attestation(intent["transaction_id"]), **mutation}
        response = client.post(
            "/v1/accounting/onramp/session",
            json={"transaction_id": intent["transaction_id"], "ip_attestation": claim},
        )
        assert response.status_code == 422, (mutation, response.text)
    provider_service.create_widget_session.assert_not_awaited()


def test_attested_mode_without_secret_keeps_intent_and_pending_available(
    monkeypatch, tmp_path
) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSAK_CLIENT_IP_MODE", "attested")
    monkeypatch.delenv("TRANSAK_CLIENT_IP_HEADER")
    monkeypatch.delenv("TRANSAK_IP_ATTESTATION_SECRET", raising=False)
    src.config._settings = None

    intent = _create_intent(client)
    provider_service.get_orders_by_partner_order_id.return_value = [
        _order(intent["transaction_id"])
    ]
    provider_service.get_orders_by_wallet.return_value = []
    pending = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": intent["transaction_id"]},
    )
    session = client.post(
        "/v1/accounting/onramp/session",
        json={
            "transaction_id": intent["transaction_id"],
            "ip_attestation": _ip_attestation(intent["transaction_id"]),
        },
    )

    assert pending.status_code == 200
    assert pending.json()["pending"][0]["provider"] == "transak"
    assert session.status_code == 503
    provider_service.get_orders_by_partner_order_id.assert_awaited_once()
    provider_service.create_widget_session.assert_not_awaited()


def test_header_mode_ignores_attestation_blob(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    response = client.post(
        "/v1/accounting/onramp/session",
        json={
            "transaction_id": intent["transaction_id"],
            "ip_attestation": _ip_attestation(intent["transaction_id"]),
        },
        headers={"x-original-user-ip": "198.51.100.9"},
    )
    assert response.status_code == 200
    kwargs = provider_service.create_widget_session.await_args.kwargs
    assert kwargs["user_ip"] == "198.51.100.9"


def test_session_is_disabled_when_moonpay_is_selected(monkeypatch, tmp_path) -> None:
    client, accounting, provider_service = _make_client(monkeypatch, tmp_path, provider="moonpay")
    moonpay_intent = onramp.create_onramp_intent(
        user_address=BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=84532,
        moonpay_currency_code="usdc",
    )
    response = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": moonpay_intent["transaction_id"]},
        headers={"x-original-user-ip": "203.0.113.4"},
    )
    assert response.status_code == 503
    accounting.get_deposit_address.assert_not_awaited()
    provider_service.create_widget_session.assert_not_awaited()


def test_moonpay_url_signing_is_disabled_when_transak_is_selected(monkeypatch, tmp_path) -> None:
    client, accounting, _provider_service = _make_client(monkeypatch, tmp_path)

    response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": "https://buy-sandbox.moonpay.com"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "MoonPay URL signing is disabled"
    accounting.get_deposit_address.assert_not_awaited()


def test_session_rejects_wrong_owner_and_expired_intent(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    config = transak.load_transak_config()
    wrong_owner = transak.create_transak_intent(
        user_address=OTHER_BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=84532,
        config=config,
    )
    response = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": wrong_owner["transaction_id"]},
        headers={"x-original-user-ip": "203.0.113.4"},
    )
    assert response.status_code == 400

    now = [1_800_000_000]
    monkeypatch.setattr(onramp_intent.time, "time", lambda: now[0])
    expiring = _create_intent(client)
    now[0] += onramp_intent.INTENT_TTL_SECONDS + 1
    expired = client.post(
        "/v1/accounting/onramp/session",
        json={"transaction_id": expiring["transaction_id"]},
        headers={"x-original-user-ip": "203.0.113.4"},
    )
    assert expired.status_code == 400
    assert expired.json()["detail"] == "On-ramp intent has expired"
    provider_service.create_widget_session.assert_not_awaited()


def test_pending_bootstraps_only_selected_transak_wallet(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    provider_service.get_orders_by_wallet.return_value = [_order(intent["transaction_id"])]

    response = client.get("/v1/accounting/onramp/pending")
    assert response.status_code == 200
    assert response.json()["pending"][0]["provider"] == "transak"
    assert response.json()["pending"][0]["provider_transaction_id"] == "transak-order-1"
    provider_service.get_orders_by_wallet.assert_awaited_once()
    routes.fetch_moonpay_buy_transactions.assert_not_awaited()


def test_pending_deduplicates_exact_and_wallet_transak_order(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    order = _order(intent["transaction_id"])
    provider_service.get_orders_by_partner_order_id.return_value = [order]
    provider_service.get_orders_by_wallet.return_value = [order]

    response = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": intent["transaction_id"]},
    )

    assert response.status_code == 200
    assert len(response.json()["pending"]) == 1
    provider_service.get_orders_by_partner_order_id.assert_awaited_once()
    exact_call = provider_service.get_orders_by_partner_order_id.await_args
    assert exact_call.args == (intent["transaction_id"],)
    assert (
        exact_call.kwargs["issued_at"]
        == onramp_intent.decode_intent(intent["transaction_id"])["iat"]
    )
    provider_service.get_orders_by_wallet.assert_awaited_once()


def test_pending_missing_exact_order_survives_wallet_failure(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    provider_service.get_orders_by_wallet.side_effect = transak.TransakAPIError(
        "Transak order lookup failed"
    )

    response = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": intent["transaction_id"]},
    )

    assert response.status_code == 200
    assert response.json() == {"pending": []}
    provider_service.get_orders_by_partner_order_id.assert_awaited_once()
    provider_service.get_orders_by_wallet.assert_awaited_once()


def test_pending_bounds_transak_provider_requests(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intents = [_create_intent(client) for _ in range(10)]

    response = client.get(
        "/v1/accounting/onramp/pending",
        params=[("externalTransactionId", intent["transaction_id"]) for intent in intents],
    )

    assert response.status_code == 200
    assert response.json() == {"pending": []}
    assert provider_service.get_orders_by_partner_order_id.await_count == 10
    provider_service.get_orders_by_wallet.assert_awaited_once()


def test_pending_exact_transak_recovery_survives_provider_switch(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    provider_service.get_orders_by_partner_order_id.return_value = [
        _order(intent["transaction_id"])
    ]
    monkeypatch.setenv("ONRAMP_PROVIDER", "moonpay")
    src.config._settings = None

    response = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": intent["transaction_id"]},
    )
    assert response.status_code == 200
    assert response.json()["pending"][0]["provider"] == "transak"
    provider_service.get_orders_by_partner_order_id.assert_awaited_once()
    routes.fetch_moonpay_buy_transactions.assert_awaited_once_with(external_customer_id=BENEFICIARY)


def test_stale_inactive_transak_intent_does_not_block_active_wallet_recovery(
    monkeypatch, tmp_path
) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    transak_intent = _create_intent(client)
    moonpay_intent = onramp.create_onramp_intent(
        user_address=BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=84532,
        moonpay_currency_code="usdc",
    )
    routes.fetch_moonpay_buy_transactions.return_value = [
        _moonpay_transaction(moonpay_intent["transaction_id"])
    ]
    monkeypatch.setenv("ONRAMP_PROVIDER", "moonpay")
    monkeypatch.setenv("TRANSAK_API_SECRET", "")
    src.config._settings = None

    response = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": transak_intent["transaction_id"]},
    )

    assert response.status_code == 200
    assert [row["provider"] for row in response.json()["pending"]] == ["moonpay"]
    provider_service.get_orders_by_partner_order_id.assert_not_awaited()
    routes.fetch_moonpay_buy_transactions.assert_awaited_once_with(external_customer_id=BENEFICIARY)


def test_pending_exact_moonpay_recovery_survives_transak_selection(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    moonpay_intent = onramp.create_onramp_intent(
        user_address=BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=84532,
        moonpay_currency_code="usdc",
    )
    routes.fetch_moonpay_buy_transactions_by_external_id.return_value = [
        _moonpay_transaction(moonpay_intent["transaction_id"])
    ]

    response = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": moonpay_intent["transaction_id"]},
    )
    assert response.status_code == 200
    assert response.json()["pending"][0]["provider"] == "moonpay"
    routes.fetch_moonpay_buy_transactions_by_external_id.assert_awaited_once()
    provider_service.get_orders_by_wallet.assert_awaited_once()


def test_pending_rejects_unsigned_wallet_order(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    provider_service.get_orders_by_wallet.return_value = [_order("", partnerOrderId=None)]
    response = client.get("/v1/accounting/onramp/pending")
    assert response.status_code == 200
    assert response.json() == {"pending": []}


def test_onramp_rate_limits_are_keyed_by_authenticated_user(monkeypatch, tmp_path) -> None:
    client, accounting, provider_service = _make_client(monkeypatch, tmp_path)

    class RecordingLimiter:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def hit(self, **kwargs):
            self.calls.append(kwargs)
            return 1

    limiter = RecordingLimiter()
    monkeypatch.setattr(routes, "get_auth_rate_limiter", lambda: limiter)
    response = client.get(
        "/v1/accounting/onramp/pending",
        headers={"x-forwarded-for": "198.51.100.20"},
    )
    assert response.status_code == 429
    assert limiter.calls[0]["key"] == BENEFICIARY.lower()
    assert limiter.calls[0]["bucket"] == "onramp_pending"
    accounting.get_deposit_address.assert_not_awaited()
    provider_service.get_orders_by_wallet.assert_not_awaited()


def test_pending_maps_transak_upstream_errors(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    provider_service.get_orders_by_wallet.side_effect = transak.TransakRateLimitError(
        "Transak order lookup is rate limited",
        retry_after=9,
    )
    limited = client.get("/v1/accounting/onramp/pending")
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "9"

    provider_service.get_orders_by_wallet.side_effect = transak.TransakAPIError(
        "Transak order lookup failed"
    )
    failed = client.get("/v1/accounting/onramp/pending")
    assert failed.status_code == 502


def test_pending_returns_exact_rows_when_wallet_lookup_is_rate_limited(
    monkeypatch, tmp_path
) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    provider_service.get_orders_by_partner_order_id.return_value = [
        _order(intent["transaction_id"])
    ]
    provider_service.get_orders_by_wallet.side_effect = transak.TransakRateLimitError(
        "Transak order lookup is rate limited",
        retry_after=9,
    )

    response = client.get(
        "/v1/accounting/onramp/pending",
        params={"externalTransactionId": intent["transaction_id"]},
    )

    assert response.status_code == 200
    assert response.json()["pending"][0]["provider_transaction_id"] == "transak-order-1"


def test_transak_webhook_is_capped_verified_and_observability_only(monkeypatch, tmp_path) -> None:
    client, _accounting, provider_service = _make_client(monkeypatch, tmp_path)
    raw = json.dumps({"data": "signed-jwt"}).encode()
    accepted = client.post("/v1/accounting/onramp/transak/webhook", content=raw)
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    provider_service.verify_webhook.assert_awaited_once_with(raw)

    provider_service.verify_webhook.side_effect = transak.TransakWebhookVerificationError(
        "Transak webhook signature mismatch"
    )
    rejected = client.post("/v1/accounting/onramp/transak/webhook", content=raw)
    assert rejected.status_code == 401

    too_large = client.post(
        "/v1/accounting/onramp/transak/webhook",
        content=b"x" * (routes._ONRAMP_WEBHOOK_MAX_BODY_BYTES + 1),
    )
    assert too_large.status_code == 413


def test_moonpay_intent_contract_remains_unchanged_by_default(monkeypatch, tmp_path) -> None:
    client, _accounting, _provider_service = _make_client(monkeypatch, tmp_path, provider="moonpay")
    response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 84532,
            "moonpay_currency_code": "usdc",
        },
    )
    assert response.status_code == 200
    assert response.json()["provider"] == "moonpay"
    assert response.json()["provider_asset_code"] == "usdc"

    missing_legacy_field = client.post(
        "/v1/accounting/onramp/intent",
        json={"token_id": TOKEN_ID, "chain_id": 84532},
    )
    assert missing_legacy_field.status_code == 400
