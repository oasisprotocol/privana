"""Tests for new accounting API routes."""

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.routes as routes
from src.services.accounting_contract import SubmissionResult


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_withdraw_from_lock_route_wires_to_service(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.withdraw_from_lock.return_value = SubmissionResult(
        submission_id="sub-1", status="submitted", detail="chain_id=84532"
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_client()
    response = client.post(
        "/v1/accounting/funds/withdraw-from-lock",
        json={
            "user_address": "0x1234567890123456789012345678901234567890",
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
    called_payload = mock_service.withdraw_from_lock.call_args[0][0]
    assert called_payload["signature"] == "0xabcd"
    assert called_payload["amount"] == 1000
    assert called_payload["nonce"] == 7


def test_credit_deposit_to_route_wires_to_service(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.credit_deposit_to.return_value = SubmissionResult(
        submission_id="sub-2", status="submitted"
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    client = _make_client()
    response = client.post(
        "/v1/accounting/deposits/for-user",
        json={
            "depositor_address": "0x1234567890123456789012345678901234567890",
            "beneficiary_address": "0x9876543210987654321098765432109876543210",
            "token_id": "11" * 32,
            "nonce": "8",
            "depositor_signature": "beef",
            "rlp_block_header": "11",
            "transaction_index_rlp": "22",
            "transaction_proof_stack": "33",
            "receipt_index_rlp": "44",
            "receipt_proof_stack": "55",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["submission_id"] == "sub-2"
    assert body["status"] == "submitted"
    called_payload = mock_service.credit_deposit_to.call_args[0][0]
    assert called_payload["token_id"].startswith("0x")
    assert called_payload["nonce"] == 8
    assert called_payload["depositor_signature"] == "0xbeef"
