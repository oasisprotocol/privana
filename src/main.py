"""Main entry point for the Accounting Module API."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.auth_routes import auth_router
from src.api.routes import router
from src.auth.auth_token_keys import get_auth_token_key_manager
from src.auth.client_registry import get_client_registry
from src.auth.jwt_keys import get_jwt_key_manager
from src.auth.siwe_config import get_siwe_configs
from src.config import load_settings
from src.services.accounting_contract import get_accounting_contract_service
from src.services.deposit_processor import get_deposit_processor
from src.services.gas_price_bootstrap import bootstrap_gas_prices
from src.services.onramp_intent import get_onramp_intent_key_manager
from src.services.rofl_signer_bootstrap import bootstrap_rofl_signer_address
from src.services.rpc_identity import initialize_verified_chain_rpc_urls
from src.services.token_info_bootstrap import bootstrap_token_info
from src.services.withdrawal_processor import get_withdrawal_processor

logger = logging.getLogger(__name__)

# Cache landing page HTML at module load to avoid disk I/O per request
_LANDING_HTML: str = (Path(__file__).parent / "templates" / "landing.html").read_text(
    encoding="utf-8"
)

settings = load_settings()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# WARNING: oasis-rofl-client DEBUG logs include full tx payloads before appd encryption!
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


class SensitiveOnRampAccessLogMiddleware:
    """Keep signed on-ramp intents out of server access logs.

    The downstream application receives a shallow copy with the original path
    and query so FastAPI can route and parse the request. The server protocol
    retains the redacted original scope it uses when writing the access log.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        downstream_scope = dict(scope)
        redacted = False
        path = str(scope.get("path") or "")
        if path.rstrip("/") == "/v1/accounting/onramp/pending" and scope.get("query_string"):
            scope["query_string"] = b"redacted=1"
            redacted = True

        dynamic_prefix = "/v1/accounting/onramp/"
        dynamic_value = path.removeprefix(dynamic_prefix)
        if path.startswith(dynamic_prefix) and dynamic_value.startswith("privana_"):
            redacted_path = f"{dynamic_prefix}redacted"
            scope["path"] = redacted_path
            scope["raw_path"] = redacted_path.encode()
            redacted = True

        if redacted:
            await self.app(downstream_scope, receive, send)
            return
        await self.app(scope, receive, send)


def _parse_cors_origins(raw_origins: str) -> set[str]:
    return {origin.strip().rstrip("/") for origin in raw_origins.split(",") if origin.strip()}


def _build_cors_origins() -> list[str]:
    origins = _parse_cors_origins(settings.cors_allowed_origins)

    if settings.environment.lower() == "development":
        origins.update(
            {
                "http://127.0.0.1:3000",
                "http://127.0.0.1:4173",
                "http://127.0.0.1:5173",
                "http://localhost:3000",
                "http://localhost:4173",
                "http://localhost:5173",
            }
        )

    if settings.siwe_domains:
        origins.update(cfg.origin for cfg in get_siwe_configs(settings))

    origins.update(get_client_registry().allowed_origins())

    return sorted(origins)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Manage application lifecycle.

    Args:
        app: FastAPI application instance
    """
    logger.info("Accounting Module API starting...")
    logger.info("Accounting contract: %s", settings.accounting_contract_address)

    # Must run before anything else: every service below builds its chain clients
    # lazily from the narrowed settings mapping, and a deployment that can serve no
    # chain should not sync keys or register tokens on its way to failing.
    served_chains = await initialize_verified_chain_rpc_urls(settings)
    logger.info("Serving source chains: %s", sorted(served_chains))

    # Initialize JWT key manager (derives keys from ROFL seed in TEE)
    jwt_key_manager = get_jwt_key_manager()
    await jwt_key_manager.initialize()
    logger.info("JWT key manager initialized")

    # Initialize AuthToken encryption key manager and sync to contract
    auth_token_key_manager = get_auth_token_key_manager()
    await auth_token_key_manager.initialize()
    logger.info("AuthToken key manager initialized")

    onramp_intent_key_manager = get_onramp_intent_key_manager()
    await onramp_intent_key_manager.initialize()
    logger.info("On-ramp intent key manager initialized")

    # Sync the encryption key to the contract (skipped when DISABLE_ROFL_KEYS is set).
    # TODO: Remove DISABLE_ROFL_KEYS check when Sapphire localnet e2e tests are available.
    if not os.getenv("DISABLE_ROFL_KEYS"):
        try:
            await auth_token_key_manager.sync_key_to_contract()
        except Exception:
            logger.exception("Failed to sync AuthToken encryption key to contract")
            raise
        logger.info("AuthToken encryption key synced to contract")

        await bootstrap_rofl_signer_address(get_accounting_contract_service())
        await bootstrap_token_info(get_accounting_contract_service(), settings.token_infos)
        await bootstrap_gas_prices(get_accounting_contract_service(), settings.gas_prices_wei)

    withdrawal_processor = get_withdrawal_processor()
    await withdrawal_processor.start()
    logger.info("Withdrawal processor started")

    # Initialize deposit processor — failure here means broken config, crash startup.
    processor = get_deposit_processor()
    logger.info("Deposit processor initialized")

    # Resume incomplete sweeps from a previous crash — best-effort, don't block startup.
    try:
        await processor.resume_incomplete_sweeps()
        logger.info("Sweep recovery complete")
    except Exception:
        logger.exception("Sweep recovery failed — incomplete sweeps may need manual attention")

    # Periodic retry for SWEPT records whose credit failed (at startup or runtime).
    processor.start_recovery_loop()

    yield

    await processor.stop()
    await withdrawal_processor.stop()
    logger.info("Accounting Module API shutting down...")


app = FastAPI(
    title="Accounting Module API",
    description="Accounting service module for ROFL apps",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SensitiveOnRampAccessLogMiddleware)

app.include_router(router)
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Landing page for the Accounting Module API."""
    return _LANDING_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment.lower() == "development",
        log_level=settings.log_level.lower(),
    )
