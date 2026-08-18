"""Tests for accounting API routes."""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from web3 import Web3
from web3.exceptions import ContractCustomError

import src.api.routes as routes
import src.auth.dependencies as auth_dependencies
from src.config.chain_config import MIN_DEPOSIT_ERC20_WEI, MIN_DEPOSIT_NATIVE_WEI
from src.models.private_read import PrivateReadAuth
from src.services.accounting_contract import SubmissionResult
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


def _make_discovery_client(monkeypatch, result=None, error=None):
    from src.services.deposit_discovery import DiscoveryResult

    mock_service = MagicMock()
    mock_service.get_deposit_address = AsyncMock(
        return_value="0x" + "aa" * 20,
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    discovery = MagicMock()
    if error is not None:
        discovery.discover_pending_deposits = AsyncMock(side_effect=error)
    else:
        discovery.discover_pending_deposits = AsyncMock(
            return_value=result
            or DiscoveryResult(pending=[], scanned_from_block=100, scanned_to_block=200)
        )
    monkeypatch.setattr(routes, "get_deposit_discovery_service", lambda: discovery)

    rate_limit = MagicMock()
    monkeypatch.setattr(routes, "_enforce_auth_rate_limit", rate_limit)
    return _make_authed_client(monkeypatch), mock_service, discovery, rate_limit


def test_pending_deposits_returns_candidates(monkeypatch) -> None:
    from src.services.deposit_discovery import DiscoveredDeposit, DiscoveryResult

    result = DiscoveryResult(
        pending=[
            DiscoveredDeposit(
                chain_id=84532,
                tx_hash="0x" + "ab" * 32,
                log_index=3,
                amount=5_000_000,
                token_address="0x" + "11" * 20,
                token_id_hex=TOKEN_ID_HEX,
                block_number=99_000,
                version=0,
                status="discovered",
            ),
            DiscoveredDeposit(
                chain_id=84532,
                tx_hash="0x" + "cd" * 32,
                log_index=0,
                amount=2_000_000,
                token_address="0x" + "11" * 20,
                token_id_hex=TOKEN_ID_HEX,
                block_number=98_500,
                version=0,
                status="processing",
                deposit_id_hex=DEPOSIT_ID_HEX,
            ),
        ],
        scanned_from_block=98_186,
        scanned_to_block=99_985,
    )
    client, mock_service, discovery, rate_limit = _make_discovery_client(monkeypatch, result)

    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 84532})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned_from_block"] == 98_186
    assert body["scanned_to_block"] == 99_985
    assert [c["status"] for c in body["pending"]] == ["discovered", "processing"]
    first = body["pending"][0]
    assert first["chain_id"] == 84532
    assert first["tx_hash"] == "0x" + "ab" * 32
    assert first["amount"] == "5000000"
    assert first["log_index"] == 3
    assert first["version"] == 0
    assert first["deposit_id"] is None
    assert body["pending"][1]["deposit_id"] == DEPOSIT_ID_HEX

    rate_limit.assert_called_once()
    assert rate_limit.call_args[0][1] == "deposits_pending"
    mock_service.get_deposit_address.assert_awaited_once()
    kwargs = discovery.discover_pending_deposits.call_args.kwargs
    assert kwargs["deposit_address"] == "0x" + "aa" * 20
    assert kwargs["beneficiary"] == BENEFICIARY
    assert kwargs["chain_id"] == 84532
    assert kwargs["token_address"] is None


def test_pending_deposits_rejects_unsupported_chain(monkeypatch) -> None:
    client, _, discovery, _ = _make_discovery_client(monkeypatch)
    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 999})
    assert response.status_code == 400
    assert "Unsupported chain_id" in response.json()["detail"]
    discovery.discover_pending_deposits.assert_not_called()


def test_pending_deposits_rejects_unsupported_version(monkeypatch) -> None:
    client, _, discovery, _ = _make_discovery_client(monkeypatch)
    response = client.get(
        "/v1/accounting/deposits/pending", params={"chain_id": 84532, "version": 7}
    )
    assert response.status_code == 400
    discovery.discover_pending_deposits.assert_not_called()


def test_pending_deposits_rejects_invalid_token_address(monkeypatch) -> None:
    client, _, discovery, _ = _make_discovery_client(monkeypatch)
    response = client.get(
        "/v1/accounting/deposits/pending",
        params={"chain_id": 84532, "token_address": "not-an-address"},
    )
    assert response.status_code == 400
    discovery.discover_pending_deposits.assert_not_called()


def test_pending_deposits_rejects_nonpositive_lookback(monkeypatch) -> None:
    client, _, discovery, _ = _make_discovery_client(monkeypatch)
    response = client.get(
        "/v1/accounting/deposits/pending", params={"chain_id": 84532, "lookback_blocks": 0}
    )
    assert response.status_code == 422  # FastAPI Query(gt=0) validation
    discovery.discover_pending_deposits.assert_not_called()


def test_pending_deposits_maps_rpc_failure_to_502(monkeypatch) -> None:
    from src.services.deposit_discovery import DiscoveryRPCError

    client, _, _, _ = _make_discovery_client(monkeypatch, error=DiscoveryRPCError("timeout"))
    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 84532})
    assert response.status_code == 502


def test_pending_deposits_maps_missing_rpc_config_to_503(monkeypatch) -> None:
    from src.services.deposit_discovery import DiscoveryNotConfiguredError

    client, _, _, _ = _make_discovery_client(
        monkeypatch, error=DiscoveryNotConfiguredError("No RPC URL configured for chain 84532")
    )
    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 84532})
    assert response.status_code == 503
    # Operator fault: the config detail must stay server-side
    assert "RPC URL" not in response.json()["detail"]


def test_pending_deposits_maps_siwe_revert_to_401(monkeypatch) -> None:
    from web3.exceptions import ContractLogicError

    client, _, _, _ = _make_discovery_client(
        monkeypatch, error=ContractLogicError("execution reverted: InvalidSiweToken")
    )
    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 84532})
    assert response.status_code == 401


def test_pending_deposits_maps_other_revert_to_422(monkeypatch) -> None:
    from web3.exceptions import ContractLogicError

    client, _, _, _ = _make_discovery_client(
        monkeypatch, error=ContractLogicError("execution reverted: SomethingElse")
    )
    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 84532})
    assert response.status_code == 422


def test_pending_deposits_maps_unregistered_token_to_400(monkeypatch) -> None:
    token = "0x" + "22" * 20
    client, _, _, _ = _make_discovery_client(
        monkeypatch,
        error=ValueError(f"token_address {token} is not a registered token on chain 84532"),
    )
    response = client.get(
        "/v1/accounting/deposits/pending",
        params={"chain_id": 84532, "token_address": Web3.to_checksum_address(token)},
    )
    assert response.status_code == 400
    assert "not a registered token" in response.json()["detail"]


def test_pending_deposits_requires_auth(monkeypatch) -> None:
    discovery = MagicMock()
    monkeypatch.setattr(routes, "get_deposit_discovery_service", lambda: discovery)
    client = _make_client()
    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 84532})
    assert response.status_code == 401
    discovery.discover_pending_deposits.assert_not_called()


def test_get_deposit_address_advertises_only_configured_asset_types(monkeypatch) -> None:
    """A chain advertises a minimum only for the asset types registered on it."""
    mock_service = MagicMock()
    mock_service.get_deposit_address = AsyncMock(
        return_value="0x" + "aa" * 20,
    )
    monkeypatch.setattr(routes, "_service", mock_service)

    fake_settings = MagicMock()
    fake_settings.token_infos = [
        {"chain_id": 84532, "token_address": None},
        {"chain_id": 84532, "token_address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"},
        {"chain_id": 23295, "token_address": "0xF5f49fbBBD46C204b836d243995df72A61bC7ce7"},
    ]
    fake_settings.chain_rpc_urls = {
        84532: "https://base-sepolia.g.alchemy.com/v2/test",
        23295: "https://testnet.sapphire.oasis.io",
    }
    monkeypatch.setattr(routes, "load_settings", lambda: fake_settings)

    client = _make_private_read_client(token=b"\xab" * 32)
    response = client.post(
        "/v1/accounting/deposits/address",
        json={"chain_type": "evm", "version": 0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deposit_address"] == "0x" + "aa" * 20
    assert body["chain_type"] == "evm"
    assert body["version"] == 0

    min_23295 = body["min_deposit"]["23295"]
    assert "native" not in min_23295
    assert min_23295["erc20"] == str(MIN_DEPOSIT_ERC20_WEI[23295])

    min_84532 = body["min_deposit"]["84532"]
    assert min_84532["native"] == str(MIN_DEPOSIT_NATIVE_WEI[84532])
    assert min_84532["erc20"] == str(MIN_DEPOSIT_ERC20_WEI[84532])

    min_11155111 = body["min_deposit"].get("11155111", {})
    assert "native" not in min_11155111
    assert "erc20" not in min_11155111


def test_pending_deposits_accepts_sapphire_chain_23295(monkeypatch) -> None:
    from src.services.deposit_discovery import DiscoveryResult

    result = DiscoveryResult(
        pending=[],
        scanned_from_block=10_000,
        scanned_to_block=10_100,
    )
    client, mock_service, discovery, rate_limit = _make_discovery_client(monkeypatch, result)

    response = client.get("/v1/accounting/deposits/pending", params={"chain_id": 23295})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned_from_block"] == 10_000
    assert body["scanned_to_block"] == 10_100
    assert body["pending"] == []

    rate_limit.assert_called_once()
    mock_service.get_deposit_address.assert_awaited_once()
    kwargs = discovery.discover_pending_deposits.call_args.kwargs
    assert kwargs["chain_id"] == 23295
    assert kwargs["deposit_address"] == "0x" + "aa" * 20
    assert kwargs["beneficiary"] == BENEFICIARY
