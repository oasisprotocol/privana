"""Tests for application startup security invariants."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from uvicorn.protocols.http.httptools_impl import RequestResponseCycle

import src.main as main


def test_sensitive_access_log_middleware_is_outermost() -> None:
    assert main.app.user_middleware[0].cls is main.SensitiveOnRampAccessLogMiddleware
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.asyncio
async def test_cors_preflight_still_redacts_pending_query_from_outer_scope() -> None:
    signed_intent = b"privana_sensitive.signed"
    outer_scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "OPTIONS",
        "scheme": "https",
        "path": "/v1/accounting/onramp/pending",
        "raw_path": b"/v1/accounting/onramp/pending",
        "query_string": b"externalTransactionId=" + signed_intent,
        "root_path": "",
        "headers": [
            (b"origin", b"http://localhost:3000"),
            (b"access-control-request-method", b"GET"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await main.app(outer_scope, receive, send)

    assert messages[0]["status"] == 200
    assert outer_scope["query_string"] == b"redacted=1"
    assert signed_intent not in repr(outer_scope).encode()


@pytest.mark.asyncio
async def test_pending_onramp_query_is_redacted_only_from_access_log_scope() -> None:
    signed_intent = b"privana_sensitive.signed"
    outer_scope = {
        "type": "http",
        "path": "/v1/accounting/onramp/pending",
        "query_string": b"externalTransactionId=" + signed_intent,
    }
    downstream_queries: list[bytes] = []
    messages: list[dict] = []

    async def downstream(scope, _receive, send) -> None:
        downstream_queries.append(scope["query_string"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    middleware = main.SensitiveOnRampAccessLogMiddleware(downstream)
    await middleware(outer_scope, receive, send)

    assert downstream_queries == [b"externalTransactionId=" + signed_intent]
    assert outer_scope["query_string"] == b"redacted=1"
    assert signed_intent not in repr(outer_scope).encode()
    assert messages[-1]["type"] == "http.response.body"


@pytest.mark.asyncio
async def test_compatibility_route_intent_is_redacted_only_from_access_log_scope() -> None:
    signed_intent = "privana_sensitive.signed"
    original_path = f"/v1/accounting/onramp/{signed_intent}"
    outer_scope = {
        "type": "http",
        "path": original_path,
        "raw_path": original_path.encode(),
        "query_string": b"",
    }
    downstream_paths: list[str] = []

    async def downstream(scope, _receive, _send) -> None:
        downstream_paths.append(scope["path"])

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict) -> None:
        return None

    middleware = main.SensitiveOnRampAccessLogMiddleware(downstream)
    await middleware(outer_scope, receive, send)

    assert downstream_paths == [original_path]
    assert outer_scope["path"] == "/v1/accounting/onramp/redacted"
    assert signed_intent not in repr(outer_scope)


@pytest.mark.asyncio
async def test_uvicorn_access_log_uses_redacted_scope(caplog) -> None:
    signed_intent = "privana_sensitive.signed"
    outer_scope = {
        "type": "http",
        "http_version": "1.1",
        "client": ("127.0.0.1", 12345),
        "method": "GET",
        "path": "/v1/accounting/onramp/pending",
        "query_string": f"externalTransactionId={signed_intent}".encode(),
        "headers": [],
    }
    transport = MagicMock(spec=asyncio.Transport)
    cycle = RequestResponseCycle(
        scope=outer_scope,
        transport=transport,
        flow=SimpleNamespace(write_paused=False),
        logger=logging.getLogger("uvicorn.error"),
        access_logger=logging.getLogger("uvicorn.access"),
        access_log=True,
        default_headers=[],
        message_event=asyncio.Event(),
        expect_100_continue=False,
        keep_alive=True,
        on_response=lambda: None,
    )

    async def downstream(_scope, _receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = main.SensitiveOnRampAccessLogMiddleware(downstream)
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        await middleware(outer_scope, AsyncMock(), cycle.send)

    access_messages = [
        record.getMessage() for record in caplog.records if record.name == "uvicorn.access"
    ]
    assert len(access_messages) == 1
    assert signed_intent not in access_messages[0]
    assert "/v1/accounting/onramp/pending?redacted=1" in access_messages[0]


@pytest.mark.asyncio
async def test_lifespan_aborts_when_auth_token_key_sync_fails(monkeypatch) -> None:
    monkeypatch.delenv("DISABLE_ROFL_KEYS", raising=False)

    jwt_key_manager = SimpleNamespace(initialize=AsyncMock())
    auth_token_key_manager = SimpleNamespace(
        initialize=AsyncMock(),
        sync_key_to_contract=AsyncMock(side_effect=RuntimeError("sync failed")),
    )
    onramp_intent_key_manager = SimpleNamespace(initialize=AsyncMock())
    bootstrap_rofl_signer_address = AsyncMock()

    monkeypatch.setattr(main, "get_jwt_key_manager", lambda: jwt_key_manager)
    monkeypatch.setattr(main, "get_auth_token_key_manager", lambda: auth_token_key_manager)
    monkeypatch.setattr(
        main,
        "get_onramp_intent_key_manager",
        lambda: onramp_intent_key_manager,
    )
    monkeypatch.setattr(main, "bootstrap_rofl_signer_address", bootstrap_rofl_signer_address)

    with pytest.raises(RuntimeError, match="sync failed"):
        async with main.lifespan(None):
            pass

    bootstrap_rofl_signer_address.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_aborts_when_onramp_intent_key_derivation_fails(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DISABLE_ROFL_KEYS", raising=False)

    jwt_key_manager = SimpleNamespace(initialize=AsyncMock())
    auth_token_key_manager = SimpleNamespace(
        initialize=AsyncMock(),
        sync_key_to_contract=AsyncMock(),
    )
    onramp_intent_key_manager = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError("intent key derivation failed"))
    )

    monkeypatch.setattr(main, "get_jwt_key_manager", lambda: jwt_key_manager)
    monkeypatch.setattr(main, "get_auth_token_key_manager", lambda: auth_token_key_manager)
    monkeypatch.setattr(
        main,
        "get_onramp_intent_key_manager",
        lambda: onramp_intent_key_manager,
    )

    with pytest.raises(RuntimeError, match="intent key derivation failed"):
        async with main.lifespan(None):
            pass

    auth_token_key_manager.sync_key_to_contract.assert_not_awaited()
