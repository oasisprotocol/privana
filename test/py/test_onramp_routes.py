"""Tests for MoonPay on-ramp accounting routes."""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, call

from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3

import src.api.routes as routes
import src.config
import src.services.onramp as onramp
from src.models.private_read import PrivateReadAuth

BENEFICIARY = Web3.to_checksum_address("0x" + "bb" * 20)
DEPOSIT_ADDRESS = Web3.to_checksum_address("0x" + "aa" * 20)
TOKEN_ID = "0x" + "11" * 32
TX_HASH = "0x" + "22" * 32
PRIVATE_READ_TOKEN = b"\x12" * 65
RESOLVED_PRIVATE_READ_TOKEN = b"\x34" * 65


def _webhook_signature(raw: bytes) -> str:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"wh_test_key",
        timestamp.encode() + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},s={signature}"


def _reset_onramp_store() -> None:
    if onramp._onramp_store_instance is not None:
        onramp._onramp_store_instance.close()
    onramp._onramp_store_instance = None


def _make_client(monkeypatch, tmp_path) -> tuple[TestClient, MagicMock]:
    monkeypatch.setenv("AUTH_TOKEN_STORAGE_DIR", str(tmp_path / "auth_store"))
    monkeypatch.setenv("MOONPAY_API_KEY", "pk_test_key")
    monkeypatch.setenv("MOONPAY_SECRET_KEY", "sk_test_key")
    monkeypatch.setenv("MOONPAY_WEBHOOK_SECRET_KEY", "wh_test_key")
    src.config._settings = None
    _reset_onramp_store()

    mock_service = MagicMock()
    mock_service.get_deposit_address = AsyncMock(return_value=DEPOSIT_ADDRESS)
    monkeypatch.setattr(routes, "_service", mock_service)

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


def test_sign_onramp_url_returns_moonpay_signature(monkeypatch, tmp_path) -> None:
    client, mock_service = _make_client(monkeypatch, tmp_path)
    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent_id = intent_response.json()["transaction_id"]
    query = (
        "apiKey=pk_test_key"
        "&currencyCode=usdc"
        f"&walletAddress={DEPOSIT_ADDRESS}"
        f"&externalCustomerId={BENEFICIARY}"
        f"&externalTransactionId={intent_id}"
    )

    response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={"url": f"https://buy-sandbox.moonpay.com?{query}"},
    )

    assert response.status_code == 200
    expected = base64.b64encode(
        hmac.new(b"sk_test_key", f"?{query}".encode(), hashlib.sha256).digest()
    ).decode("ascii")
    assert response.json() == {"signature": expected}
    assert mock_service.get_deposit_address.await_args_list == [
        call("evm", 0, RESOLVED_PRIVATE_READ_TOKEN),
        call("evm", 0, PRIVATE_READ_TOKEN),
    ]


def test_sign_onramp_url_rejects_currency_mismatched_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent_id = intent_response.json()["transaction_id"]

    response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={
            "url": (
                "https://buy-sandbox.moonpay.com"
                "?apiKey=pk_test_key"
                "&currencyCode=usdc_base"
                f"&walletAddress={DEPOSIT_ADDRESS}"
                f"&externalCustomerId={BENEFICIARY}"
                f"&externalTransactionId={intent_id}"
            )
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "MoonPay currencyCode does not match the Privana intent"


def test_sign_onramp_url_rejects_non_deposit_wallet(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    wrong_wallet = Web3.to_checksum_address("0x" + "cc" * 20)
    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent_id = intent_response.json()["transaction_id"]

    response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={
            "url": (
                "https://buy-sandbox.moonpay.com"
                "?apiKey=pk_test_key"
                "&currencyCode=usdc"
                f"&walletAddress={wrong_wallet}"
                f"&externalCustomerId={BENEFICIARY}"
                f"&externalTransactionId={intent_id}"
            )
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "MoonPay walletAddress must be the Privana deposit address"


def test_sign_onramp_url_requires_external_customer_id(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent_id = intent_response.json()["transaction_id"]

    response = client.post(
        "/v1/accounting/onramp/sign-url",
        json={
            "url": (
                "https://buy-sandbox.moonpay.com"
                "?apiKey=pk_test_key"
                "&currencyCode=usdc"
                f"&walletAddress={DEPOSIT_ADDRESS}"
                f"&externalTransactionId={intent_id}"
            )
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "MoonPay URL is missing externalCustomerId"


def test_webhook_completion_surfaces_pending_onramp(monkeypatch, tmp_path) -> None:
    client, mock_service = _make_client(monkeypatch, tmp_path)

    create_response = client.post(
        "/v1/accounting/onramp/moonpay-tx-1",
        json={
            "wallet_address": DEPOSIT_ADDRESS,
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert create_response.status_code == 200

    payload = {
        "type": "transaction_updated",
        "externalCustomerId": BENEFICIARY,
        "data": {
            "id": "moonpay-tx-1",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "baseCurrencyAmount": 100,
            "quoteCurrencyAmount": 99.12,
            "externalCustomerId": BENEFICIARY,
            "baseCurrency": {"code": "usd"},
            "currency": {
                "code": "usdc",
                "metadata": {"chainId": "1", "networkCode": "ethereum"},
            },
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    mock_service.get_deposit_address.reset_mock()
    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    mock_service.get_deposit_address.assert_awaited_once_with("evm", 0, RESOLVED_PRIVATE_READ_TOKEN)
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == "moonpay-tx-1"
    assert pending[0]["status"] == "completed"
    assert pending[0]["wallet_address"] == DEPOSIT_ADDRESS
    assert pending[0]["token_id"] == TOKEN_ID
    assert pending[0]["chain_id"] == 11155111
    assert pending[0]["on_chain_tx_hash"] == TX_HASH
    assert pending[0]["quote_currency_amount"] == "99.12"


def test_webhook_completion_uses_external_transaction_id_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-2",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "baseCurrencyAmount": 100,
            "quoteCurrencyAmount": 0.9613,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "baseCurrency": {"code": "usd"},
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intent["transaction_id"]
    assert pending[0]["external_transaction_id"] == intent["transaction_id"]
    assert pending[0]["moonpay_transaction_id"] == "moonpay-tx-2"
    assert pending[0]["status"] == "completed"
    assert pending[0]["wallet_address"] == DEPOSIT_ADDRESS
    assert pending[0]["token_id"] == TOKEN_ID
    assert pending[0]["chain_id"] == 11155111
    assert pending[0]["moonpay_currency_code"] == "usdc"
    assert pending[0]["on_chain_tx_hash"] == TX_HASH
    assert pending[0]["quote_currency_amount"] == "0.9613"


def test_webhook_completion_keeps_existing_intent_owner(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    moonpay_customer_id = Web3.to_checksum_address("0x" + "cc" * 20)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-embedded",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "baseCurrencyAmount": 95.79,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": moonpay_customer_id,
            "externalTransactionId": intent["transaction_id"],
            "baseCurrency": {"code": "usd"},
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intent["transaction_id"]
    assert pending[0]["moonpay_transaction_id"] == "moonpay-tx-embedded"
    assert pending[0]["status"] == "completed"
    assert pending[0]["wallet_address"] == DEPOSIT_ADDRESS
    assert pending[0]["on_chain_tx_hash"] == TX_HASH
    assert pending[0]["quote_currency_amount"] == "94.28"

    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored["user_address"] == BENEFICIARY


def test_webhook_completion_keeps_existing_intent_wallet(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    stale_wallet = Web3.to_checksum_address("0x" + "cc" * 20)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-stale-wallet",
            "status": "completed",
            "walletAddress": stale_wallet,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intent["transaction_id"]
    assert pending[0]["wallet_address"] == DEPOSIT_ADDRESS

    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored["wallet_address"] == DEPOSIT_ADDRESS


def test_webhook_without_external_transaction_id_joins_moonpay_mapping(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    mapping_response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_transaction_id": "moonpay-tx-no-external-id",
        },
    )
    assert mapping_response.status_code == 200

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-no-external-id",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": BENEFICIARY,
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intent["transaction_id"]
    assert pending[0]["external_transaction_id"] == intent["transaction_id"]
    assert pending[0]["moonpay_transaction_id"] == "moonpay-tx-no-external-id"

    assert onramp.get_onramp_store().get("moonpay-tx-no-external-id") is None


def test_webhook_without_external_transaction_id_matches_single_open_intent(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    stale_moonpay_customer = Web3.to_checksum_address("0x" + "cc" * 20)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-no-external-no-mapping",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": stale_moonpay_customer,
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intent["transaction_id"]
    assert pending[0]["external_transaction_id"] == intent["transaction_id"]
    assert pending[0]["moonpay_transaction_id"] == "moonpay-tx-no-external-no-mapping"
    assert pending[0]["wallet_address"] == DEPOSIT_ADDRESS
    assert pending[0]["on_chain_tx_hash"] == TX_HASH

    assert onramp.get_onramp_store().get("moonpay-tx-no-external-no-mapping") is None
    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored["user_address"] == BENEFICIARY


def test_webhook_without_external_transaction_id_keeps_ambiguous_intents_orphaned(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    for _ in range(2):
        response = client.post(
            "/v1/accounting/onramp/intent",
            json={
                "token_id": TOKEN_ID,
                "chain_id": 11155111,
                "moonpay_currency_code": "usdc",
                "base_currency_code": "usd",
                "base_currency_amount": "100",
            },
        )
        assert response.status_code == 200

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-ambiguous-intent",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": BENEFICIARY,
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    assert client.get("/v1/accounting/onramp/pending").json()["pending"] == []
    orphan = onramp.get_onramp_store().get("moonpay-tx-ambiguous-intent")
    assert orphan is not None
    assert orphan["on_chain_tx_hash"] == TX_HASH


def test_late_moonpay_mapping_merges_existing_orphan_webhook(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    intents = []
    for _ in range(2):
        intent_response = client.post(
            "/v1/accounting/onramp/intent",
            json={
                "token_id": TOKEN_ID,
                "chain_id": 11155111,
                "moonpay_currency_code": "usdc",
                "base_currency_code": "usd",
                "base_currency_amount": "100",
            },
        )
        assert intent_response.status_code == 200
        intents.append(intent_response.json())

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-orphan-first",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": BENEFICIARY,
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200
    assert client.get("/v1/accounting/onramp/pending").json()["pending"] == []

    mapping_response = client.post(
        f"/v1/accounting/onramp/{intents[0]['transaction_id']}",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_transaction_id": "moonpay-tx-orphan-first",
        },
    )
    assert mapping_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intents[0]["transaction_id"]
    assert pending[0]["external_transaction_id"] == intents[0]["transaction_id"]
    assert pending[0]["moonpay_transaction_id"] == "moonpay-tx-orphan-first"
    assert pending[0]["on_chain_tx_hash"] == TX_HASH

    assert onramp.get_onramp_store().get("moonpay-tx-orphan-first") is None


def test_late_moonpay_mapping_drops_stale_orphan_failure_reason(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-stale-reason",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "failureReason": "stale failure",
            "externalCustomerId": BENEFICIARY,
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    mapping_response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_transaction_id": "moonpay-tx-stale-reason",
        },
    )
    assert mapping_response.status_code == 200

    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored.get("failure_reason") is None


def test_late_moonpay_mapping_does_not_delete_wallet_mismatched_orphan(
    monkeypatch, tmp_path
) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    other_wallet = Web3.to_checksum_address("0x" + "cc" * 20)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
            "base_currency_code": "usd",
            "base_currency_amount": "100",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-mismatched-orphan",
            "status": "completed",
            "walletAddress": other_wallet,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 94.28,
            "externalCustomerId": BENEFICIARY,
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()

    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    mapping_response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_transaction_id": "moonpay-tx-mismatched-orphan",
        },
    )
    assert mapping_response.status_code == 200

    assert client.get("/v1/accounting/onramp/pending").json()["pending"] == []
    orphan = onramp.get_onramp_store().get("moonpay-tx-mismatched-orphan")
    assert orphan is not None
    assert orphan["wallet_address"] == other_wallet

    stored_intent = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored_intent is not None
    assert stored_intent["wallet_address"] == DEPOSIT_ADDRESS
    assert stored_intent.get("on_chain_tx_hash") is None


def test_update_onramp_rejects_cross_user_rebind(monkeypatch, tmp_path) -> None:
    client, mock_service = _make_client(monkeypatch, tmp_path)
    other_user = Web3.to_checksum_address("0x" + "cc" * 20)
    transaction_id = "privana_existing"
    onramp.get_onramp_store().upsert(
        transaction_id,
        {
            "external_transaction_id": transaction_id,
            "user_address": other_user,
            "wallet_address": DEPOSIT_ADDRESS,
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )

    response = client.post(
        f"/v1/accounting/onramp/{transaction_id}",
        json={"token_id": TOKEN_ID, "chain_id": 11155111},
    )

    assert response.status_code == 403
    mock_service.get_deposit_address.assert_awaited_once_with("evm", 0, RESOLVED_PRIVATE_READ_TOKEN)
    stored = onramp.get_onramp_store().get(transaction_id)
    assert stored is not None
    assert stored["user_address"] == other_user


def test_update_onramp_rejects_unknown_privana_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    response = client.post(
        "/v1/accounting/onramp/privana_missing",
        json={"token_id": TOKEN_ID, "chain_id": 11155111},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown Privana on-ramp intent"


def test_duplicate_completed_webhook_does_not_overwrite_delivery(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    conflicting_tx_hash = "0x" + "99" * 32

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    completed_payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-duplicate-completed",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 0.9613,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw_completed = json.dumps(completed_payload, separators=(",", ":")).encode()
    completed_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw_completed,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw_completed),
        },
    )
    assert completed_response.status_code == 200

    duplicate_payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-duplicate-completed",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": conflicting_tx_hash,
            "quoteCurrencyAmount": 123.45,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw_duplicate = json.dumps(duplicate_payload, separators=(",", ":")).encode()
    duplicate_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw_duplicate,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw_duplicate),
        },
    )
    assert duplicate_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["on_chain_tx_hash"] == TX_HASH
    assert pending[0]["quote_currency_amount"] == "0.9613"


def test_deposit_tx_hash_marks_triggered_but_not_credited(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    deposit_tx_hash = "0x" + "33" * 32

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-credit-semantics",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 0.9613,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    update_response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={"deposit_tx_hash": deposit_tx_hash},
    )

    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["deposit_tx_hash"] == deposit_tx_hash
    assert updated["deposit_triggered_at"] is not None
    assert updated["credited_at"] is None


def test_update_onramp_rejects_malformed_deposit_tx_hash(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    response = client.post(
        f"/v1/accounting/onramp/{intent['transaction_id']}",
        json={"deposit_tx_hash": "0x1234"},
    )

    assert response.status_code == 422
    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored.get("deposit_tx_hash") is None


def test_deposit_status_marks_onramp_credited_server_side(monkeypatch, tmp_path) -> None:
    client, mock_service = _make_client(monkeypatch, tmp_path)
    deposit_id = "0x" + "dd" * 32

    wrong_chain_intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 84532,
            "moonpay_currency_code": "usdc",
        },
    )
    assert wrong_chain_intent_response.status_code == 200
    wrong_chain_intent = wrong_chain_intent_response.json()

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-server-credit",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 0.9613,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )
    assert webhook_response.status_code == 200

    wrong_chain_payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-wrong-chain",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 0.9613,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": wrong_chain_intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    wrong_chain_raw = json.dumps(wrong_chain_payload, separators=(",", ":")).encode()
    wrong_chain_webhook_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=wrong_chain_raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(wrong_chain_raw),
        },
    )
    assert wrong_chain_webhook_response.status_code == 200

    mock_processor = MagicMock()
    mock_processor.process_deposit = AsyncMock(
        return_value={
            "status": "pending",
            "deposit_id": deposit_id,
            "amount": "961300000000000000",
            "token_address": "0x" + "cc" * 20,
        }
    )
    mock_processor.get_deposit_status = MagicMock(return_value=None)
    monkeypatch.setattr(routes, "get_deposit_processor", lambda: mock_processor)
    mock_service.is_deposit_processed = AsyncMock(return_value=True)

    check_response = client.post(
        "/v1/accounting/deposits/check",
        json={
            "chain_id": 11155111,
            "tx_hash": TX_HASH,
            "amount": "961300000000000000",
        },
    )
    assert check_response.status_code == 202

    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored["deposit_id"] == deposit_id
    assert stored["deposit_triggered_at"] is not None
    assert stored.get("credited_at") is None
    wrong_chain_stored = onramp.get_onramp_store().get(wrong_chain_intent["transaction_id"])
    assert wrong_chain_stored is not None
    assert wrong_chain_stored.get("deposit_id") is None
    pending = client.get("/v1/accounting/onramp/pending").json()["pending"]
    assert sorted(row["chain_id"] for row in pending) == [84532, 11155111]
    matching = [row for row in pending if row["chain_id"] == 11155111]
    assert len(matching) == 1
    assert matching[0]["deposit_id"] == deposit_id

    status_response = client.get(f"/v1/accounting/deposits/status/{deposit_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "credited"

    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored["credited_at"] is not None
    pending_after_credit = client.get("/v1/accounting/onramp/pending").json()["pending"]
    assert len(pending_after_credit) == 1
    assert pending_after_credit[0]["transaction_id"] == wrong_chain_intent["transaction_id"]


def test_webhook_stale_status_does_not_mutate_completed_intent(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)

    intent_response = client.post(
        "/v1/accounting/onramp/intent",
        json={
            "token_id": TOKEN_ID,
            "chain_id": 11155111,
            "moonpay_currency_code": "usdc",
        },
    )
    assert intent_response.status_code == 200
    intent = intent_response.json()

    completed_payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-3",
            "status": "completed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": TX_HASH,
            "quoteCurrencyAmount": 0.9613,
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw_completed = json.dumps(completed_payload, separators=(",", ":")).encode()
    completed_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw_completed,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw_completed),
        },
    )
    assert completed_response.status_code == 200

    stale_payload = {
        "type": "transaction_updated",
        "data": {
            "id": "moonpay-tx-3",
            "status": "failed",
            "walletAddress": DEPOSIT_ADDRESS,
            "cryptoTransactionId": "0x" + "99" * 32,
            "quoteCurrencyAmount": 123.45,
            "failureReason": "stale failure",
            "externalCustomerId": BENEFICIARY,
            "externalTransactionId": intent["transaction_id"],
            "currency": {"code": "usdc"},
        },
    }
    raw_stale = json.dumps(stale_payload, separators=(",", ":")).encode()
    stale_response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw_stale,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw_stale),
        },
    )
    assert stale_response.status_code == 200

    pending_response = client.get("/v1/accounting/onramp/pending")

    assert pending_response.status_code == 200
    pending = pending_response.json()["pending"]
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == intent["transaction_id"]
    assert pending[0]["status"] == "completed"
    assert pending[0]["on_chain_tx_hash"] == TX_HASH
    assert pending[0]["quote_currency_amount"] == "0.9613"
    stored = onramp.get_onramp_store().get(intent["transaction_id"])
    assert stored is not None
    assert stored.get("failure_reason") is None


def test_webhook_rejects_bad_signature(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    raw = b'{"type":"transaction_updated","data":{"id":"moonpay-tx-1"}}'

    response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "Moonpay-Signature-V2": "t=1,s=bad"},
    )

    assert response.status_code == 401


def test_webhook_rejects_malformed_payload_with_valid_signature(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    raw = b"not-json"

    response = client.post(
        "/v1/accounting/onramp/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(raw),
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid MoonPay webhook JSON"


def test_webhook_rejects_oversized_body(monkeypatch, tmp_path) -> None:
    client, _mock_service = _make_client(monkeypatch, tmp_path)
    oversized = b"{" + b"a" * (1024 * 1024 + 1)

    response = client.post(
        "/v1/accounting/onramp/webhook",
        content=oversized,
        headers={
            "Content-Type": "application/json",
            "Moonpay-Signature-V2": _webhook_signature(oversized),
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "MoonPay webhook payload is too large"
