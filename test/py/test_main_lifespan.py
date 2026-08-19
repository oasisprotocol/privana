"""Tests for application startup security invariants."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from uvicorn.protocols.http.httptools_impl import RequestResponseCycle

import src.main as main
import src.services.gas_price_bootstrap as gas_price_bootstrap
import src.services.rpc_identity as rpc_identity

LIFESPAN_CHAIN = 84532
LIFESPAN_RPC_URL = "https://base-sepolia.example.invalid/key"
MIS_FILED_CHAIN = 23295
MIS_FILED_URL = "https://mis-filed.example.invalid"


def _lifespan_settings() -> SimpleNamespace:
    """Settings stand-in — never the load_settings() singleton.

    The identity check narrows ``chain_rpc_urls`` in place, so a narrowed
    singleton would leak into later tests.
    """
    return SimpleNamespace(
        accounting_contract_address="0x" + "ab" * 20,
        chain_rpc_urls={LIFESPAN_CHAIN: LIFESPAN_RPC_URL},
        token_infos=[{"chain_id": LIFESPAN_CHAIN, "token_address": None}],
        gas_prices_wei={LIFESPAN_CHAIN: 3_000_000_000},
    )


def _wire_lifespan(
    monkeypatch,
    settings: SimpleNamespace,
    steps: list[str],
    *,
    stub_identity: bool = True,
) -> SimpleNamespace:
    """Patch everything the lifespan touches, appending each step to ``steps``."""
    monkeypatch.delenv("DISABLE_ROFL_KEYS", raising=False)
    monkeypatch.setattr(main, "settings", settings)

    def step(name: str, result=None) -> AsyncMock:
        async def recorded(*_args, **_kwargs):
            steps.append(name)
            return result

        return AsyncMock(side_effect=recorded)

    jwt_key_manager = SimpleNamespace(initialize=step("jwt_keys"))
    auth_token_key_manager = SimpleNamespace(
        initialize=step("auth_token_keys"),
        sync_key_to_contract=step("sync_key_to_contract"),
    )
    onramp_intent_key_manager = SimpleNamespace(initialize=step("onramp_intent_keys"))
    accounting = MagicMock()
    accounting.get_accounting_version = step("version_check", 2)
    withdrawal_processor = SimpleNamespace(
        start=step("withdrawal_start"), stop=step("withdrawal_stop")
    )
    deposit_processor = SimpleNamespace(
        resume_incomplete_sweeps=step("resume_sweeps"),
        start_recovery_loop=MagicMock(side_effect=lambda: steps.append("recovery_loop")),
        stop=step("processor_stop"),
    )

    def get_deposit_processor() -> SimpleNamespace:
        steps.append("deposit_processor")
        return deposit_processor

    if stub_identity:
        monkeypatch.setattr(
            main,
            "initialize_verified_chain_rpc_urls",
            step("rpc_identity", {LIFESPAN_CHAIN: LIFESPAN_RPC_URL}),
        )
    monkeypatch.setattr(main, "get_jwt_key_manager", lambda: jwt_key_manager)
    monkeypatch.setattr(main, "get_auth_token_key_manager", lambda: auth_token_key_manager)
    monkeypatch.setattr(main, "get_onramp_intent_key_manager", lambda: onramp_intent_key_manager)
    monkeypatch.setattr(main, "get_accounting_contract_service", lambda: accounting)
    monkeypatch.setattr(main, "bootstrap_rofl_signer_address", step("rofl_signer"))
    monkeypatch.setattr(main, "bootstrap_token_info", step("token_info"))
    monkeypatch.setattr(main, "bootstrap_gas_prices", step("gas_prices"))
    monkeypatch.setattr(main, "get_withdrawal_processor", lambda: withdrawal_processor)
    monkeypatch.setattr(main, "get_deposit_processor", get_deposit_processor)
    # The real loop would spawn a background prober against the stubbed endpoints.
    monkeypatch.setattr(
        main,
        "start_reverification_loop",
        MagicMock(side_effect=lambda _settings: steps.append("reverify_start")),
    )
    monkeypatch.setattr(main, "stop_reverification_loop", step("reverify_stop"))
    return SimpleNamespace(
        accounting=accounting,
        deposit_processor=deposit_processor,
        withdrawal_processor=withdrawal_processor,
    )


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
    # The identity check runs first; unstubbed it would probe the real
    # endpoints before reaching the abort under test, and its loop would keep
    # probing after it.
    monkeypatch.setattr(
        main, "initialize_verified_chain_rpc_urls", AsyncMock(return_value={LIFESPAN_CHAIN: ""})
    )
    monkeypatch.setattr(main, "start_reverification_loop", MagicMock())

    jwt_key_manager = SimpleNamespace(initialize=AsyncMock())
    auth_token_key_manager = SimpleNamespace(
        initialize=AsyncMock(),
        sync_key_to_contract=AsyncMock(side_effect=RuntimeError("sync failed")),
    )
    onramp_intent_key_manager = SimpleNamespace(initialize=AsyncMock())
    bootstrap_rofl_signer_address = AsyncMock()
    accounting = MagicMock()
    accounting.get_accounting_version = AsyncMock(return_value=2)
    monkeypatch.setattr(main, "get_accounting_contract_service", lambda: accounting)

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
    monkeypatch.setattr(
        main, "initialize_verified_chain_rpc_urls", AsyncMock(return_value={LIFESPAN_CHAIN: ""})
    )
    monkeypatch.setattr(main, "start_reverification_loop", MagicMock())

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


@pytest.mark.asyncio
async def test_lifespan_verifies_rpc_identity_before_touching_any_chain(monkeypatch) -> None:
    steps: list[str] = []
    _wire_lifespan(monkeypatch, _lifespan_settings(), steps)

    async with main.lifespan(None):
        startup = list(steps)

    # Identity check is first, with its re-verification loop started on its heels:
    # nothing may read a chain, write to the contract, or register a token on an
    # unverified endpoint. Token registration precedes gas-price sync.
    assert startup == [
        "rpc_identity",
        "reverify_start",
        "jwt_keys",
        "auth_token_keys",
        "onramp_intent_keys",
        "version_check",
        "sync_key_to_contract",
        "rofl_signer",
        "token_info",
        "gas_prices",
        "withdrawal_start",
        "deposit_processor",
        "resume_sweeps",
        "recovery_loop",
    ]
    # Shutdown stops the prober before the processors it feeds.
    assert steps[len(startup) :] == ["reverify_stop", "processor_stop", "withdrawal_stop"]


@pytest.mark.asyncio
async def test_lifespan_leaves_a_mis_filed_chain_unserved(monkeypatch, caplog) -> None:
    steps: list[str] = []
    settings = _lifespan_settings()
    settings.chain_rpc_urls[MIS_FILED_CHAIN] = MIS_FILED_URL
    _wire_lifespan(monkeypatch, settings, steps, stub_identity=False)

    async def probe(url: str, _timeout: float) -> int:
        return {LIFESPAN_RPC_URL: LIFESPAN_CHAIN, MIS_FILED_URL: 1}[url]

    monkeypatch.setattr(rpc_identity, "_probe_chain_id", probe)

    with caplog.at_level(logging.ERROR, logger="src.services.rpc_identity"):
        async with main.lifespan(None):
            # Excluded, not mis-served: the chain is dropped from the served set
            # before token registration or client construction.
            assert settings.chain_rpc_urls == {LIFESPAN_CHAIN: LIFESPAN_RPC_URL}
            assert (
                rpc_identity.verified_web3(MIS_FILED_CHAIN, {MIS_FILED_CHAIN: MIS_FILED_URL})
                is None
            )
            assert rpc_identity.verified_web3(LIFESPAN_CHAIN, {}) is not None

    # One bad endpoint does not abort startup; valid chains continue serving.
    assert "withdrawal_start" in steps
    assert any(str(MIS_FILED_CHAIN) in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_lifespan_completes_when_a_registered_chain_has_no_gas_price(
    monkeypatch, caplog
) -> None:
    steps: list[str] = []
    settings = _lifespan_settings()
    handles = _wire_lifespan(monkeypatch, settings, steps)

    async def failing_gas_price(_chain_id: int) -> int:
        steps.append("gas_price_attempt")
        raise RuntimeError("rofl-appd unreachable")

    # Gas-price bootstrap is best-effort and returns after retries are exhausted.
    monkeypatch.setattr(gas_price_bootstrap, "_BASE_RETRY_DELAY", 0)
    handles.accounting.get_gas_price = AsyncMock(side_effect=failing_gas_price)
    monkeypatch.setattr(main, "bootstrap_gas_prices", gas_price_bootstrap.bootstrap_gas_prices)

    with caplog.at_level(logging.ERROR, logger="src.services.gas_price_bootstrap"):
        async with main.lifespan(None):
            pass

    # Token registration is permanent on the contract, while the chain carries no
    # gas price until synced. Startup completes regardless.
    assert steps.count("gas_price_attempt") == gas_price_bootstrap._MAX_ATTEMPTS
    assert steps.index("token_info") < steps.index("gas_price_attempt")
    assert steps.index("gas_price_attempt") < steps.index("withdrawal_start")
    assert steps[-1] == "withdrawal_stop"
    assert settings.chain_rpc_urls == {LIFESPAN_CHAIN: LIFESPAN_RPC_URL}
    assert any(
        "Failed to sync gas price for chain 84532" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_lifespan_aborts_when_contract_version_below_required(monkeypatch) -> None:
    steps: list[str] = []
    handles = _wire_lifespan(monkeypatch, _lifespan_settings(), steps)
    version_read = AsyncMock(return_value=1)
    handles.accounting.get_accounting_version = version_read

    with pytest.raises(RuntimeError, match="upgrade the proxy before restarting"):
        async with main.lifespan(None):
            pass

    # The gate read the version and nothing touched the contract afterwards.
    version_read.assert_awaited_once()
    assert "sync_key_to_contract" not in steps
    assert "token_info" not in steps
    assert "withdrawal_start" not in steps


@pytest.mark.asyncio
async def test_lifespan_proceeds_at_required_version(monkeypatch) -> None:
    steps: list[str] = []
    _wire_lifespan(monkeypatch, _lifespan_settings(), steps)

    async with main.lifespan(None):
        pass

    assert steps.index("version_check") < steps.index("sync_key_to_contract")
    assert steps.index("token_info") < steps.index("gas_prices")
    assert steps[-1] == "withdrawal_stop"
