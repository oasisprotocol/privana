"""Tests for accounting API routes."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3
from web3.exceptions import ContractCustomError

import src.api.routes as routes
import src.auth.dependencies as auth_dependencies
from src.config.chain_config import (
    MIN_DEPOSIT_ERC20_WEI,
    MIN_DEPOSIT_NATIVE_WEI,
    get_finality_depth,
)
from src.models.private_read import PrivateReadAuth
from src.services.accounting_contract import SubmissionOutcomeUnknownError, SubmissionResult
from src.services.deposit_processor import DepositProcessor

BENEFICIARY = "0x" + "bb" * 20
CHECKSUM_BENEFICIARY = Web3.to_checksum_address(BENEFICIARY)
DEPOSIT_ID_HEX = "0x" + "dd" * 32
TOKEN_ID_HEX = "0x" + "11" * 32
SERVICE_ADDRESS = Web3.to_checksum_address("0x" + "22" * 20)


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


def _make_transfer_client() -> TestClient:
    """Client with _require_caller overridden to a fixed caller."""
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_caller] = lambda: BENEFICIARY
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


def test_get_balance_route_passes_token_and_echoes_user(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_balance = AsyncMock(
        return_value={
            "token_id": TOKEN_ID_HEX,
            "balance": "7",
            "token_symbol": "TEST",
            "chain_id": "23295",
        }
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    token = b"\xba\x1a"
    client = _make_private_read_client(token=token)
    response = client.get(f"/v1/accounting/balances/{TOKEN_ID_HEX}")

    assert response.status_code == 200
    assert response.json() == {
        "user_address": CHECKSUM_BENEFICIARY,
        "token_id": TOKEN_ID_HEX,
        "balance": "7",
        "token_symbol": "TEST",
        "chain_id": "23295",
    }
    mock_service.get_balance.assert_awaited_once_with(token, TOKEN_ID_HEX)


def test_get_batch_balances_route_passes_token_and_echoes_user(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_batch_balances = AsyncMock(
        return_value={
            "balances": [
                {
                    "token_id": TOKEN_ID_HEX,
                    "balance": "7",
                    "token_symbol": "TEST",
                    "chain_id": "23295",
                }
            ],
        }
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    token = b"\xba\x7c"
    client = _make_private_read_client(token=token)
    response = client.post("/v1/accounting/balances/batch", json={"token_ids": [TOKEN_ID_HEX]})

    assert response.status_code == 200
    assert response.json() == {
        "user_address": CHECKSUM_BENEFICIARY,
        "balances": [
            {
                "token_id": TOKEN_ID_HEX,
                "balance": "7",
                "token_symbol": "TEST",
                "chain_id": "23295",
            }
        ],
    }
    mock_service.get_batch_balances.assert_awaited_once_with(token, [TOKEN_ID_HEX])


def test_get_locked_funds_route_passes_token_and_echoes_user(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_locked_funds = AsyncMock(
        return_value={
            "service_address": SERVICE_ADDRESS,
            "locks": [
                {
                    "lock_id": 1,
                    "service_address": SERVICE_ADDRESS,
                    "token_id": TOKEN_ID_HEX,
                    "amount": "7",
                    "expiry": 9999999999,
                    "is_expired": False,
                }
            ],
            "total_locked": "7",
        }
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    token = b"\x10\xcc"
    client = _make_private_read_client(token=token)
    response = client.get(f"/v1/accounting/funds/locked?service_address={SERVICE_ADDRESS}")

    assert response.status_code == 200
    assert response.json() == {
        "user_address": CHECKSUM_BENEFICIARY,
        "service_address": SERVICE_ADDRESS,
        "locks": [
            {
                "lock_id": 1,
                "user_address": CHECKSUM_BENEFICIARY,
                "service_address": SERVICE_ADDRESS,
                "token_id": TOKEN_ID_HEX,
                "amount": "7",
                "expiry": 9999999999,
                "is_expired": False,
            }
        ],
        "total_locked": "7",
    }
    mock_service.get_locked_funds.assert_awaited_once_with(token, SERVICE_ADDRESS)


def test_get_expired_locks_route_passes_token_and_echoes_user(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_expired_locks = AsyncMock(
        return_value={
            "expired_locks": [
                {
                    "lock_id": 1,
                    "service_address": SERVICE_ADDRESS,
                    "token_id": TOKEN_ID_HEX,
                    "amount": "7",
                    "expiry": 1,
                    "is_expired": True,
                }
            ],
        }
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    token = b"\xee\x11"
    client = _make_private_read_client(token=token)
    response = client.get("/v1/accounting/funds/expired")

    assert response.status_code == 200
    assert response.json() == {
        "user_address": CHECKSUM_BENEFICIARY,
        "expired_locks": [
            {
                "lock_id": 1,
                "user_address": CHECKSUM_BENEFICIARY,
                "service_address": SERVICE_ADDRESS,
                "token_id": TOKEN_ID_HEX,
                "amount": "7",
                "expiry": 1,
                "is_expired": True,
            }
        ],
    }
    mock_service.get_expired_locks.assert_awaited_once_with(token)


def test_get_total_locked_balance_route_passes_token_and_echoes_user(monkeypatch) -> None:
    mock_service = MagicMock()
    mock_service.get_total_locked_balance = AsyncMock(
        return_value={"token_id": TOKEN_ID_HEX, "total_locked": "7"}
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    token = b"\x70\x7a\x01"
    client = _make_private_read_client(token=token)
    response = client.get(f"/v1/accounting/funds/locked/total/{TOKEN_ID_HEX}")

    assert response.status_code == 200
    assert response.json() == {
        "user_address": CHECKSUM_BENEFICIARY,
        "token_id": TOKEN_ID_HEX,
        "total_locked": "7",
    }
    mock_service.get_total_locked_balance.assert_awaited_once_with(token, TOKEN_ID_HEX)


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


def _deposit_address_body(monkeypatch, token_infos, chain_rpc_urls) -> dict:
    """POST /deposits/address with the given registered assets and verified chains."""
    mock_service = MagicMock()
    mock_service.get_deposit_address = AsyncMock(return_value="0x" + "aa" * 20)
    monkeypatch.setattr(routes, "_service", mock_service)

    fake_settings = MagicMock()
    fake_settings.token_infos = {f"0x{i:064x}": info for i, info in enumerate(token_infos)}
    fake_settings.chain_rpc_urls = chain_rpc_urls
    monkeypatch.setattr(routes, "load_settings", lambda: fake_settings)

    client = _make_private_read_client(token=b"\xab" * 32)
    response = client.post(
        "/v1/accounting/deposits/address",
        json={"chain_type": "evm", "version": 0},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_get_deposit_address_advertises_only_configured_asset_types(monkeypatch) -> None:
    """A chain advertises a minimum only for the asset types registered on it."""
    body = _deposit_address_body(
        monkeypatch,
        token_infos=[
            {"chain_id": 84532, "token_address": None},
            {"chain_id": 84532, "token_address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"},
            {"chain_id": 23295, "token_address": "0xF5f49fbBBD46C204b836d243995df72A61bC7ce7"},
        ],
        chain_rpc_urls={
            84532: "https://base-sepolia.g.alchemy.com/v2/test",
            23295: "https://testnet.sapphire.oasis.io",
        },
    )
    assert body["deposit_address"] == "0x" + "aa" * 20
    assert body["chain_type"] == "evm"
    assert body["version"] == 0

    assert set(body["min_deposit"]) == {"84532", "23295"}

    min_23295 = body["min_deposit"]["23295"]
    assert "native" not in min_23295
    assert min_23295["erc20"] == str(MIN_DEPOSIT_ERC20_WEI[23295])

    min_84532 = body["min_deposit"]["84532"]
    assert min_84532["native"] == str(MIN_DEPOSIT_NATIVE_WEI[84532])
    assert min_84532["erc20"] == str(MIN_DEPOSIT_ERC20_WEI[84532])


def test_get_deposit_address_omits_chain_without_verified_rpc(monkeypatch) -> None:
    """A chain with registered assets but no verified RPC is not advertised."""
    body = _deposit_address_body(
        monkeypatch,
        token_infos=[
            {"chain_id": 84532, "token_address": None},
            {"chain_id": 11155111, "token_address": None},
        ],
        chain_rpc_urls={84532: "https://base-sepolia.g.alchemy.com/v2/test"},
    )
    assert "11155111" not in body["min_deposit"]
    assert "11155111" not in body["finality_depth"]
    assert body["min_deposit"]["84532"]["native"] == str(MIN_DEPOSIT_NATIVE_WEI[84532])


def test_get_deposit_address_omits_verified_chain_without_registered_assets(monkeypatch) -> None:
    """A verified chain with no registered asset yields no entry, not an empty dict."""
    body = _deposit_address_body(
        monkeypatch,
        token_infos=[
            {"chain_id": 23295, "token_address": "0xF5f49fbBBD46C204b836d243995df72A61bC7ce7"},
        ],
        chain_rpc_urls={
            23295: "https://testnet.sapphire.oasis.io",
            23293: "http://localhost:8545",
        },
    )
    assert set(body["min_deposit"]) == {"23295"}
    # Served for confirmation depth, just not depositable.
    assert body["finality_depth"]["23293"] == get_finality_depth(23293)


@pytest.mark.parametrize(
    "path, method, body",
    [
        (
            "lock",
            "lock_funds",
            {
                "service_address": SERVICE_ADDRESS,
                "token_id": "0x" + "aa" * 32,
                "amount": "1000",
                "expiry": 9999999999,
            },
        ),
        ("modify-lock", "modify_lock", {"lock_id": 1, "amount": "0", "new_expiry": 9999999999}),
        (
            "transfer",
            "transfer_funds",
            {"to_address": BENEFICIARY, "token_id": "0x" + "aa" * 32, "amount": "1000"},
        ),
        (
            "transfer-locked",
            "transfer_locked_funds",
            {
                "user_address": BENEFICIARY,
                "lock_id": 1,
                "to_address": BENEFICIARY,
                "amount": "1000",
                "service_address": SERVICE_ADDRESS,
            },
        ),
    ],
)
def test_signed_write_route_maps_unknown_outcome_to_409(monkeypatch, path, method, body) -> None:
    mock_service = MagicMock()
    setattr(
        mock_service,
        method,
        AsyncMock(
            side_effect=SubmissionOutcomeUnknownError("submitted and its outcome is unknown")
        ),
    )
    monkeypatch.setattr(routes, "_service", mock_service)
    _set_rate_limit_exempt(monkeypatch, ())
    monkeypatch.setattr(routes, "get_auth_rate_limiter", lambda: _RecordingRateLimiter())

    response = _make_transfer_client().post(
        f"/v1/accounting/funds/{path}", json={**body, "nonce": 0, "signature": "0xabcd"}
    )

    assert response.status_code == 409
    assert "outcome is unknown" in response.json()["detail"]


TRANSFER_BODY = {
    "to_address": "0x9876543210987654321098765432109876543210",
    "token_id": TOKEN_ID_HEX,
    "amount": "1000",
    "nonce": 3,
    "signature": "abcd",
}


class _RecordingRateLimiter:
    def __init__(self, retry_after: int | None = None) -> None:
        self.retry_after = retry_after
        self.calls: list[dict[str, object]] = []

    def hit(self, **kwargs) -> int | None:
        self.calls.append(kwargs)
        return self.retry_after


def _mock_transfer_service(monkeypatch) -> MagicMock:
    mock_service = MagicMock()
    mock_service.transfer_funds = AsyncMock(
        return_value=SubmissionResult(submission_id="sub-1", status="submitted")
    )
    monkeypatch.setattr(routes, "_service", mock_service)
    return mock_service


def _set_rate_limit_exempt(monkeypatch, exempt: tuple[str, ...]) -> None:
    fake_settings = MagicMock()
    fake_settings.rate_limit_exempt = exempt
    monkeypatch.setattr(routes, "load_settings", lambda: fake_settings)


def test_transfer_route_rejects_missing_auth(monkeypatch) -> None:
    mock_service = _mock_transfer_service(monkeypatch)

    response = _make_client().post("/v1/accounting/funds/transfer", json=TRANSFER_BODY)

    assert response.status_code == 401
    mock_service.transfer_funds.assert_not_called()


def test_transfer_route_rate_limits_per_caller(monkeypatch) -> None:
    mock_service = _mock_transfer_service(monkeypatch)
    _set_rate_limit_exempt(monkeypatch, ())
    limiter = _RecordingRateLimiter()
    monkeypatch.setattr(routes, "get_auth_rate_limiter", lambda: limiter)

    response = _make_transfer_client().post("/v1/accounting/funds/transfer", json=TRANSFER_BODY)

    assert response.status_code == 200
    assert [(call["limit"], call["window_seconds"]) for call in limiter.calls] == list(
        routes._TRANSFER_RATE_LIMITS
    )
    assert {call["key"] for call in limiter.calls} == {BENEFICIARY}
    mock_service.transfer_funds.assert_awaited_once()


def test_transfer_route_returns_429_over_the_limit(monkeypatch) -> None:
    mock_service = _mock_transfer_service(monkeypatch)
    _set_rate_limit_exempt(monkeypatch, ())
    monkeypatch.setattr(routes, "get_auth_rate_limiter", lambda: _RecordingRateLimiter(30))

    response = _make_transfer_client().post("/v1/accounting/funds/transfer", json=TRANSFER_BODY)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    mock_service.transfer_funds.assert_not_called()


def test_transfer_route_skips_rate_limit_for_exempt_caller(monkeypatch) -> None:
    mock_service = _mock_transfer_service(monkeypatch)
    _set_rate_limit_exempt(monkeypatch, (CHECKSUM_BENEFICIARY,))
    limiter = _RecordingRateLimiter(30)
    monkeypatch.setattr(routes, "get_auth_rate_limiter", lambda: limiter)

    response = _make_transfer_client().post("/v1/accounting/funds/transfer", json=TRANSFER_BODY)

    assert response.status_code == 200
    assert limiter.calls == []
    mock_service.transfer_funds.assert_awaited_once()
