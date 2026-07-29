"""Tests for MoonPay on-ramp accounting routes."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, call

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3

import src.api.routes as routes
import src.auth.rate_limiter as rate_limiter
import src.config
import src.services.onramp as onramp
import src.services.onramp_intent as onramp_intent
from src.models.private_read import PrivateReadAuth

BENEFICIARY = Web3.to_checksum_address("0x" + "bb" * 20)
OTHER_BENEFICIARY = Web3.to_checksum_address("0x" + "cc" * 20)
DEPOSIT_ADDRESS = Web3.to_checksum_address("0x" + "aa" * 20)
OTHER_DEPOSIT_ADDRESS = Web3.to_checksum_address("0x" + "dd" * 20)
TOKEN_ID = "0x" + "11" * 32
TX_HASH = "0x" + "22" * 32
PRIVATE_READ_TOKEN = b"\x12" * 65
RESOLVED_PRIVATE_READ_TOKEN = b"\x34" * 65
INTENT_SIGNING_KEY = b"route-intent-signing-key-00000001"
ROTATED_INTENT_SIGNING_KEY = b"route-intent-signing-key-00000002"
INTENT_SIGNING_KEY_ID = "onramp_intent_signing_key.v1.key"
ROTATED_INTENT_SIGNING_KEY_ID = "onramp_intent_signing_key.v2.key"


class _FakeMoonPayResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeMoonPayClient:
    requests: list[dict[str, object]] = []
    responses: list[object] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    async def get(self, url: str, **kwargs):
        self.requests.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _set_required_env(monkeypatch, tmp_path) -> None:
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
        "MOONPAY_API_KEY": "pk_test_key",
        "MOONPAY_SECRET_KEY": "sk_test_key",
        "ONRAMP_INTENT_SIGNING_KEY_ID": INTENT_SIGNING_KEY_ID,
        "ONRAMP_INTENT_PREVIOUS_SIGNING_KEY_IDS": "",
        "MOONPAY_API_BASE_URL": "https://api.moonpay.com",
        "MOONPAY_WEBHOOK_SECRET_KEY": "wh_test_key",
        "MOONPAY_ALLOWED_HOSTS": "buy.moonpay.com,buy-sandbox.moonpay.com",
        "MOONPAY_ALLOWED_CURRENCY_CODES": "usdc",
        "MOONPAY_WEBHOOK_TOLERANCE_SECONDS": "300",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    src.config._settings = None
    if rate_limiter._auth_rate_limiter_instance is not None:
        rate_limiter._auth_rate_limiter_instance.close()
    rate_limiter._auth_rate_limiter_instance = None
    _configure_intent_keys(current=INTENT_SIGNING_KEY)


def _configure_intent_keys(
    *,
    current: bytes,
    previous: tuple[bytes, ...] = (),
) -> None:
    manager = onramp_intent.OnRampIntentKeyManager()
    manager._current_key = current
    manager._verification_keys = tuple(dict.fromkeys((current, *previous)))
    onramp_intent._onramp_intent_key_manager_instance = manager


def _use_fake_moonpay_client(monkeypatch, responses: list[object]) -> type[_FakeMoonPayClient]:
    _FakeMoonPayClient.requests = []
    _FakeMoonPayClient.responses = list(responses)
    monkeypatch.setattr(onramp.httpx, "AsyncClient", _FakeMoonPayClient)
    return _FakeMoonPayClient


def _make_client(monkeypatch, tmp_path) -> tuple[TestClient, MagicMock]:
    _set_required_env(monkeypatch, tmp_path)

    mock_service = MagicMock()
    mock_service.get_deposit_address = AsyncMock(return_value=DEPOSIT_ADDRESS)
    monkeypatch.setattr(routes, "_service", mock_service)
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        AsyncMock(return_value=[]),
    )

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_private_read_auth] = lambda: PrivateReadAuth(
        token=PRIVATE_READ_TOKEN,
        user_address=BENEFICIARY,
    )
    app.dependency_overrides[routes._require_resolved_private_read_auth] = lambda: PrivateReadAuth(
        token=RESOLVED_PRIVATE_READ_TOKEN,
        user_address=BENEFICIARY,
    )
    return TestClient(app), mock_service


def _create_intent(client: TestClient, **overrides) -> dict:
    payload = {
        "token_id": TOKEN_ID,
        "chain_id": 11155111,
        "moonpay_currency_code": "usdc",
    }
    payload.update(overrides)
    response = client.post("/v1/accounting/onramp/intent", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _moonpay_url(intent_id: str, **overrides) -> str:
    params = {
        "apiKey": "pk_test_key",
        "currencyCode": "usdc",
        "walletAddress": DEPOSIT_ADDRESS,
        "externalCustomerId": BENEFICIARY,
        "externalTransactionId": intent_id,
    }
    params.update(overrides)
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"https://buy-sandbox.moonpay.com?{query}"


def _moonpay_transaction(intent_id: str, **overrides) -> dict:
    transaction = {
        "id": "moonpay-tx-1",
        "status": "completed",
        "walletAddress": DEPOSIT_ADDRESS,
        "cryptoTransactionId": TX_HASH,
        "externalTransactionId": intent_id,
        "externalCustomerId": BENEFICIARY,
        "baseCurrencyAmount": "95.78",
        "quoteCurrencyAmount": "94.26",
        "baseCurrency": {"code": "usd"},
        "currency": {"code": "usdc"},
        "createdAt": "2026-06-09T15:00:00.000Z",
        "updatedAt": "2026-06-09T15:01:00.000Z",
    }
    transaction.update(overrides)
    return transaction


def _webhook_signature(raw: bytes, *, timestamp: int | None = None) -> str:
    timestamp = timestamp or int(time.time())
    timestamp_text = str(timestamp)
    signature = hmac.new(
        b"wh_test_key",
        timestamp_text.encode() + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp_text},s={signature}"


def _tamper(identifier: str) -> str:
    pivot = identifier.index(".") - 1
    replacement = "A" if identifier[pivot] != "A" else "B"
    return identifier[:pivot] + replacement + identifier[pivot + 1 :]


def test_intent_is_signed_and_sign_url_returns_moonpay_signature(monkeypatch, tmp_path) -> None:
    client, mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    intent_id = intent["transaction_id"]

    assert len(intent_id) <= onramp_intent.INTENT_MAX_LENGTH
    decoded = onramp.decode_onramp_intent(intent_id)
    assert Web3.to_checksum_address("0x" + decoded["u"]) == BENEFICIARY
    assert Web3.to_checksum_address("0x" + decoded["w"]) == DEPOSIT_ADDRESS
    assert intent["on_chain_tx_hash"] is None

    unsigned_url = _moonpay_url(intent_id)
    query = unsigned_url.split("?", 1)[1]
    response = client.post("/v1/accounting/onramp/sign-url", json={"url": unsigned_url})

    assert response.status_code == 200
    expected = base64.b64encode(
        hmac.new(b"sk_test_key", f"?{query}".encode(), hashlib.sha256).digest()
    ).decode("ascii")
    assert response.json() == {"signature": expected}
    assert mock_service.get_deposit_address.await_args_list == [
        call("evm", 0, RESOLVED_PRIVATE_READ_TOKEN),
        call("evm", 0, RESOLVED_PRIVATE_READ_TOKEN),
    ]


def test_sign_url_accepts_previous_key_after_rotation(
    monkeypatch,
    tmp_path,
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    _configure_intent_keys(
        current=ROTATED_INTENT_SIGNING_KEY,
        previous=(INTENT_SIGNING_KEY,),
    )
    response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": _moonpay_url(intent["transaction_id"])},
    )

    assert response.status_code == 200


def test_signing_key_id_configuration_strips_previous_ids(
    monkeypatch,
    tmp_path,
) -> None:
    _set_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "ONRAMP_INTENT_PREVIOUS_SIGNING_KEY_IDS",
        f" {INTENT_SIGNING_KEY_ID}, ,{ROTATED_INTENT_SIGNING_KEY_ID} ",
    )
    src.config._settings = None

    assert src.config.load_settings().onramp_intent_previous_signing_key_ids == (
        INTENT_SIGNING_KEY_ID,
        ROTATED_INTENT_SIGNING_KEY_ID,
    )


def test_sign_url_rejects_tampered_or_wrong_currency_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    tampered = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": _moonpay_url(_tamper(intent["transaction_id"]))},
    )
    assert tampered.status_code == 400
    assert "signature mismatch" in tampered.json()["detail"]

    wrong_currency = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": _moonpay_url(intent["transaction_id"], currencyCode="eth")},
    )
    assert wrong_currency.status_code == 400
    assert wrong_currency.json()["detail"] == "MoonPay currencyCode is not allowed"


def test_sign_url_rejects_missing_customer_and_non_deposit_wallet(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    missing_customer = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": _moonpay_url(intent["transaction_id"], externalCustomerId="")},
    )
    assert missing_customer.status_code == 400
    assert missing_customer.json()["detail"] == "MoonPay URL is missing externalCustomerId"

    lowercase_customer = client.post(
        "/v1/accounting/onramp/sign-url",
        json={
            "url": _moonpay_url(intent["transaction_id"], externalCustomerId=BENEFICIARY.lower())
        },
    )
    assert lowercase_customer.status_code == 400
    assert (
        lowercase_customer.json()["detail"]
        == "MoonPay externalCustomerId must match authenticated user"
    )

    wrong_wallet = client.post(
        "/v1/accounting/onramp/sign-url",
        json={
            "url": _moonpay_url(
                intent["transaction_id"],
                walletAddress=OTHER_DEPOSIT_ADDRESS,
            )
        },
    )
    assert wrong_wallet.status_code == 400
    assert (
        wrong_wallet.json()["detail"] == "MoonPay walletAddress must be the Privana deposit address"
    )


def test_sign_url_rejects_expired_intent_but_pending_can_recover(monkeypatch, tmp_path) -> None:
    base_time = 1_781_000_000
    monkeypatch.setattr(onramp.time, "time", lambda: base_time)
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    monkeypatch.setattr(
        onramp.time,
        "time",
        lambda: base_time + onramp_intent.INTENT_TTL_SECONDS + 1,
    )
    sign_response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": _moonpay_url(intent["transaction_id"])},
    )
    assert sign_response.status_code == 400
    assert sign_response.json()["detail"] == "On-ramp intent has expired"

    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        AsyncMock(return_value=[_moonpay_transaction(intent["transaction_id"])]),
    )
    pending_response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={intent['transaction_id']}"
    )
    assert pending_response.status_code == 200
    assert pending_response.json()["pending"][0]["transaction_id"] == intent["transaction_id"]


def test_pending_queries_moonpay_by_customer_and_filters_signed_intents(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    other_intent = onramp.create_onramp_intent(
        user_address=OTHER_BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=11155111,
        moonpay_currency_code="usdc",
    )["transaction_id"]
    mock_fetch = AsyncMock(
        return_value=[
            _moonpay_transaction(intent["transaction_id"]),
            _moonpay_transaction(
                intent["transaction_id"],
                id="moonpay-tx-pending",
                status="pending",
            ),
            _moonpay_transaction(
                intent["transaction_id"],
                id="moonpay-tx-wrong-wallet",
                walletAddress=OTHER_DEPOSIT_ADDRESS,
            ),
            _moonpay_transaction(other_intent, id="moonpay-tx-wrong-user"),
            _moonpay_transaction(_tamper(intent["transaction_id"]), id="moonpay-tx-tampered"),
        ]
    )
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", mock_fetch)

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 200
    assert response.json()["pending"] == [
        {
            "transaction_id": intent["transaction_id"],
            "external_transaction_id": intent["transaction_id"],
            "moonpay_transaction_id": "moonpay-tx-1",
            "status": "completed",
            "wallet_address": DEPOSIT_ADDRESS,
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "95.78",
            "quote_currency_amount": "94.26",
            "on_chain_tx_hash": TX_HASH,
            "deposit_id": None,
            "deposit_tx_hash": None,
            "deposit_triggered_at": None,
            "credited_at": None,
            "created_at": 1781017200,
            "updated_at": 1781017260,
        }
    ]
    mock_fetch.assert_awaited_once_with(external_customer_id=BENEFICIARY)


def test_pending_filters_currency_mismatch(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(
            return_value=[
                _moonpay_transaction(
                    intent["transaction_id"],
                    currency={"code": "eth"},
                )
            ]
        ),
    )

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 200
    assert response.json() == {"pending": []}


def test_pending_filters_missing_on_chain_hash(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(
            return_value=[
                _moonpay_transaction(
                    intent["transaction_id"],
                    cryptoTransactionId=None,
                )
            ]
        ),
    )

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 200
    assert response.json() == {"pending": []}


def test_pending_uses_exact_intent_lookup_for_stale_moonpay_customer_metadata(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    other_intent = onramp.create_onramp_intent(
        user_address=OTHER_BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=11155111,
        moonpay_currency_code="usdc",
    )["transaction_id"]
    tx = _moonpay_transaction(
        intent["transaction_id"],
        externalCustomerId=OTHER_BENEFICIARY,
    )
    mock_fetch_customer = AsyncMock(return_value=[])
    mock_fetch_external = AsyncMock(
        return_value=[
            tx,
            _moonpay_transaction(other_intent, id="moonpay-tx-other-user"),
        ]
    )
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", mock_fetch_customer)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        mock_fetch_external,
    )

    response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={intent['transaction_id']}"
    )

    assert response.status_code == 200
    assert response.json()["pending"][0]["moonpay_transaction_id"] == "moonpay-tx-1"
    mock_fetch_customer.assert_awaited_once_with(external_customer_id=BENEFICIARY)
    mock_fetch_external.assert_awaited_once_with(intent["transaction_id"])


def test_pending_runs_exact_intent_lookup_even_when_customer_lookup_has_rows(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    customer_intent = _create_intent(client)
    exact_intent = _create_intent(client)
    mock_fetch_customer = AsyncMock(
        return_value=[
            _moonpay_transaction(customer_intent["transaction_id"], id="moonpay-customer")
        ]
    )
    mock_fetch_external = AsyncMock(
        return_value=[
            _moonpay_transaction(
                exact_intent["transaction_id"],
                id="moonpay-exact",
                updatedAt="2026-06-09T15:02:00.000Z",
            )
        ]
    )
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", mock_fetch_customer)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        mock_fetch_external,
    )

    response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={exact_intent['transaction_id']}"
    )

    assert response.status_code == 200
    pending = response.json()["pending"]
    assert [row["moonpay_transaction_id"] for row in pending] == [
        "moonpay-exact",
        "moonpay-customer",
    ]
    mock_fetch_customer.assert_awaited_once_with(external_customer_id=BENEFICIARY)
    mock_fetch_external.assert_awaited_once_with(exact_intent["transaction_id"])


def test_pending_does_not_run_fallback_without_explicit_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    mock_fetch_customer = AsyncMock(return_value=[])
    mock_fetch_external = AsyncMock(return_value=[_moonpay_transaction("unused")])
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", mock_fetch_customer)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        mock_fetch_external,
    )

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 200
    assert response.json() == {"pending": []}
    mock_fetch_customer.assert_awaited_once_with(external_customer_id=BENEFICIARY)
    mock_fetch_external.assert_not_awaited()


def test_pending_is_rate_limited_before_moonpay_lookup(monkeypatch, tmp_path) -> None:
    client, mock_service = _make_client(monkeypatch, tmp_path)

    class DenyRateLimiter:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def hit(self, **kwargs) -> int:
            self.calls.append(kwargs)
            return 1

    limiter = DenyRateLimiter()
    monkeypatch.setattr(routes, "get_auth_rate_limiter", lambda: limiter)

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert limiter.calls[0]["bucket"] == "onramp_pending"
    assert limiter.calls[0]["limit"] == routes._ONRAMP_PENDING_RATE_LIMIT
    mock_service.get_deposit_address.assert_not_awaited()


def test_pending_returns_502_when_moonpay_lookup_fails(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(side_effect=onramp.MoonPayAPIError("MoonPay transaction lookup failed")),
    )

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 502
    assert response.json()["detail"] == "MoonPay transaction lookup failed"


def test_pending_exact_lookup_survives_customer_lookup_failure(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(side_effect=onramp.MoonPayAPIError("MoonPay customer lookup failed")),
    )
    mock_fetch_external = AsyncMock(
        return_value=[_moonpay_transaction(intent["transaction_id"], id="moonpay-exact")]
    )
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        mock_fetch_external,
    )

    response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={intent['transaction_id']}"
    )

    assert response.status_code == 200
    assert response.json()["pending"][0]["moonpay_transaction_id"] == "moonpay-exact"
    mock_fetch_external.assert_awaited_once_with(intent["transaction_id"])


def test_pending_customer_lookup_survives_exact_lookup_failure(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    customer_intent = _create_intent(client)
    exact_intent = _create_intent(client)
    mock_fetch_customer = AsyncMock(
        return_value=[_moonpay_transaction(customer_intent["transaction_id"])]
    )
    mock_fetch_external = AsyncMock(
        side_effect=onramp.MoonPayAPIError("MoonPay exact lookup failed")
    )
    monkeypatch.setattr(routes, "fetch_moonpay_buy_transactions", mock_fetch_customer)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        mock_fetch_external,
    )

    response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={exact_intent['transaction_id']}"
    )

    assert response.status_code == 200
    pending = response.json()["pending"]
    assert [row["moonpay_transaction_id"] for row in pending] == ["moonpay-tx-1"]
    mock_fetch_customer.assert_awaited_once_with(external_customer_id=BENEFICIARY)
    mock_fetch_external.assert_awaited_once_with(exact_intent["transaction_id"])


def test_pending_returns_502_when_all_moonpay_lookups_fail(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(side_effect=onramp.MoonPayAPIError("MoonPay customer lookup failed")),
    )
    mock_fetch_external = AsyncMock(
        side_effect=onramp.MoonPayAPIError("MoonPay exact lookup failed")
    )
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions_by_external_id",
        mock_fetch_external,
    )

    response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={intent['transaction_id']}"
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "MoonPay customer lookup failed"
    mock_fetch_external.assert_awaited_once_with(intent["transaction_id"])


def test_pending_returns_503_when_intent_key_manager_is_uninitialized(
    monkeypatch,
    tmp_path,
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(return_value=[_moonpay_transaction(intent["transaction_id"])]),
    )
    onramp_intent._onramp_intent_key_manager_instance = onramp_intent.OnRampIntentKeyManager()

    response = client.get("/v1/accounting/onramp/pending")

    assert response.status_code == 503
    assert response.json()["detail"] == "On-ramp intents are not configured"


def test_pending_rejects_too_many_exact_intent_lookups(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent_ids = [
        onramp.create_onramp_intent(
            user_address=BENEFICIARY,
            wallet_address=DEPOSIT_ADDRESS,
            token_id=TOKEN_ID,
            chain_id=11155111,
            moonpay_currency_code="usdc",
        )["transaction_id"]
        for _ in range(routes._ONRAMP_PENDING_MAX_INTENT_LOOKUPS + 1)
    ]

    response = client.get(
        "/v1/accounting/onramp/pending",
        params=[("externalTransactionId", intent_id) for intent_id in intent_ids],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Too many externalTransactionId values"


async def test_fetch_moonpay_transactions_uses_customer_filter_and_paginates(
    monkeypatch,
    tmp_path,
) -> None:
    _set_required_env(monkeypatch, tmp_path)
    first_page = [{"id": f"moonpay-{index}"} for index in range(50)]
    second_page = [{"id": "moonpay-50"}]
    fake_client = _use_fake_moonpay_client(
        monkeypatch,
        [
            _FakeMoonPayResponse(200, first_page),
            _FakeMoonPayResponse(200, {"data": second_page}),
        ],
    )

    transactions = await onramp.fetch_moonpay_buy_transactions(external_customer_id=BENEFICIARY)

    assert transactions == first_page + second_page
    assert fake_client.requests == [
        {
            "url": "https://api.moonpay.com/v1/transactions",
            "params": {
                "limit": 50,
                "offset": 0,
                "externalCustomerId": BENEFICIARY,
            },
            "headers": {"Authorization": "Api-Key sk_test_key"},
        },
        {
            "url": "https://api.moonpay.com/v1/transactions",
            "params": {
                "limit": 50,
                "offset": 50,
                "externalCustomerId": BENEFICIARY,
            },
            "headers": {"Authorization": "Api-Key sk_test_key"},
        },
    ]


async def test_fetch_moonpay_transactions_logs_page_cap(monkeypatch, tmp_path, caplog) -> None:
    _set_required_env(monkeypatch, tmp_path)
    _use_fake_moonpay_client(
        monkeypatch,
        [_FakeMoonPayResponse(200, [{"id": "moonpay-1"}, {"id": "moonpay-2"}])],
    )

    with caplog.at_level("WARNING", logger=onramp.logger.name):
        transactions = await onramp.fetch_moonpay_buy_transactions(
            external_customer_id=BENEFICIARY,
            limit=2,
            max_pages=1,
        )

    assert transactions == [{"id": "moonpay-1"}, {"id": "moonpay-2"}]
    assert "MoonPay transaction lookup hit page cap" in caplog.text


def test_dedupe_moonpay_transactions_preserves_order() -> None:
    assert onramp.dedupe_moonpay_transactions(
        [
            {"id": "moonpay-1"},
            {"id": "moonpay-1", "externalTransactionId": "ignored"},
            {"externalTransactionId": "intent-1"},
            {"externalTransactionId": "intent-1"},
            {"cryptoTransactionId": TX_HASH},
            {"cryptoTransactionId": TX_HASH},
            {"status": "completed"},
            {"status": "completed"},
        ]
    ) == [
        {"id": "moonpay-1"},
        {"externalTransactionId": "intent-1"},
        {"cryptoTransactionId": TX_HASH},
        {"status": "completed"},
        {"status": "completed"},
    ]


async def test_fetch_moonpay_transactions_by_external_id_uses_exact_endpoint(
    monkeypatch,
    tmp_path,
) -> None:
    _set_required_env(monkeypatch, tmp_path)
    intent_id = "privana_a.b/c?"
    fake_client = _use_fake_moonpay_client(
        monkeypatch,
        [_FakeMoonPayResponse(200, {"transactions": [{"id": "moonpay-exact"}]})],
    )

    transactions = await onramp.fetch_moonpay_buy_transactions_by_external_id(intent_id)

    assert transactions == [{"id": "moonpay-exact"}]
    assert fake_client.requests == [
        {
            "url": "https://api.moonpay.com/v1/transactions/ext/privana_a.b%2Fc%3F",
            "headers": {"Authorization": "Api-Key sk_test_key"},
        }
    ]


async def test_fetch_moonpay_transactions_maps_auth_and_transport_errors(
    monkeypatch,
    tmp_path,
) -> None:
    _set_required_env(monkeypatch, tmp_path)
    _use_fake_moonpay_client(monkeypatch, [_FakeMoonPayResponse(401, {"message": "bad key"})])

    try:
        await onramp.fetch_moonpay_buy_transactions(external_customer_id=BENEFICIARY)
    except onramp.MoonPayAPIError as exc:
        assert str(exc) == "MoonPay transaction lookup is unauthorized"
    else:
        raise AssertionError("expected MoonPayAPIError")

    _use_fake_moonpay_client(
        monkeypatch,
        [httpx.ConnectError("network unavailable")],
    )
    try:
        await onramp.fetch_moonpay_buy_transactions_by_external_id("privana_test")
    except onramp.MoonPayAPIError as exc:
        assert str(exc) == "MoonPay transaction lookup failed"
    else:
        raise AssertionError("expected MoonPayAPIError")


def test_pending_rejects_wrong_owner_exact_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    wrong_user_intent = onramp.create_onramp_intent(
        user_address=OTHER_BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=11155111,
        moonpay_currency_code="usdc",
    )

    response = client.get(
        f"/v1/accounting/onramp/pending?externalTransactionId={wrong_user_intent['transaction_id']}"
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"] == "MoonPay externalTransactionId does not belong to the caller"
    )


def test_webhook_is_verified_but_pending_comes_from_moonpay_lookup(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)
    raw = json.dumps(
        {
            "type": "transaction_updated",
            "data": _moonpay_transaction(intent["transaction_id"]),
        },
        separators=(",", ":"),
    ).encode()

    webhook = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={"Moonpay-Signature-V2": _webhook_signature(raw)},
    )
    assert webhook.status_code == 200
    assert webhook.json() == {"ok": True}

    no_lookup_rows = client.get("/v1/accounting/onramp/pending")
    assert no_lookup_rows.status_code == 200
    assert no_lookup_rows.json() == {"pending": []}

    monkeypatch.setattr(
        routes,
        "fetch_moonpay_buy_transactions",
        AsyncMock(return_value=[_moonpay_transaction(intent["transaction_id"])]),
    )
    pending = client.get("/v1/accounting/onramp/pending")
    assert pending.status_code == 200
    assert len(pending.json()["pending"]) == 1


def test_webhook_rejects_bad_signature_and_large_payload(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    raw = json.dumps({"data": {"id": "moonpay-tx-1"}}).encode()

    bad_signature = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={"Moonpay-Signature-V2": "t=1,s=bad"},
    )
    assert bad_signature.status_code == 401

    stale_signature = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={"Moonpay-Signature-V2": _webhook_signature(raw, timestamp=1)},
    )
    assert stale_signature.status_code == 401
    assert stale_signature.json()["detail"] == "MoonPay signature timestamp is outside tolerance"

    too_large = client.post(
        "/v1/accounting/onramp/webhook",
        content=b"x" * (routes._ONRAMP_WEBHOOK_MAX_BODY_BYTES + 1),
        headers={"Moonpay-Signature-V2": _webhook_signature(b"x")},
    )
    assert too_large.status_code == 413


def test_update_onramp_compatibility_validates_and_echoes_signed_intent(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(routes.time, "time", lambda: 1_781_017_400)
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={
            "wallet_address": DEPOSIT_ADDRESS,
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_transaction_id": "moonpay-tx-1",
            "quote_currency_amount": "94.26",
            "on_chain_tx_hash": TX_HASH,
            "deposit_tx_hash": "0x" + "44" * 32,
        },
    )

    assert response.status_code == 200
    record = response.json()
    assert record["transaction_id"] == intent["transaction_id"]
    assert record["moonpay_transaction_id"] == "moonpay-tx-1"
    assert record["status"] == "completed"
    assert record["on_chain_tx_hash"] == TX_HASH
    assert record["deposit_id"] is None
    assert record["deposit_tx_hash"] == "0x" + "44" * 32
    assert record["deposit_triggered_at"] == 1781017400
    assert record["credited_at"] is None


def test_update_onramp_rejects_ignored_compatibility_fields(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent = _create_intent(client)

    response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={"deposit_id": "0x" + "55" * 32},
    )

    assert response.status_code == 422


def test_update_onramp_rejects_wrong_owner_and_locked_field_mismatch(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    wrong_user_intent = onramp.create_onramp_intent(
        user_address=OTHER_BENEFICIARY,
        wallet_address=DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=11155111,
        moonpay_currency_code="usdc",
    )

    wrong_owner = client.post(
        f"/v1/accounting/onramp/{wrong_user_intent['transaction_id']}",
        json={"wallet_address": DEPOSIT_ADDRESS},
    )
    assert wrong_owner.status_code == 403

    wrong_wallet_intent = onramp.create_onramp_intent(
        user_address=BENEFICIARY,
        wallet_address=OTHER_DEPOSIT_ADDRESS,
        token_id=TOKEN_ID,
        chain_id=11155111,
        moonpay_currency_code="usdc",
    )
    wrong_wallet = client.post(
        f"/v1/accounting/onramp/{wrong_wallet_intent['transaction_id']}",
        json={"wallet_address": DEPOSIT_ADDRESS},
    )
    assert wrong_wallet.status_code == 403

    intent = _create_intent(client)
    wrong_token = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={"token_id": "0x" + "33" * 32},
    )
    assert wrong_token.status_code == 400
    assert wrong_token.json()["detail"] == "token_id does not match signed on-ramp intent"
