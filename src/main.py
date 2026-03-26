"""Main entry point for the Accounting Module API."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.api.auth_routes import auth_router
from src.api.routes import router
from src.auth.client_registry import get_client_registry
from src.auth.siwe_config import get_siwe_config
from src.config import load_settings

logger = logging.getLogger(__name__)

# Cache landing page HTML at module load to avoid disk I/O per request
_LANDING_HTML: str = (Path(__file__).parent / "templates" / "landing.html").read_text(
    encoding="utf-8"
)

settings = load_settings()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


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

    if settings.siwe_domain:
        origins.add(get_siwe_config(settings).origin)

    origins.update(get_client_registry().allowed_origins())

    return sorted(origins)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Manage application lifecycle.

    Args:
        app: FastAPI application instance
    """
    import os

    from src.auth.auth_token_keys import get_auth_token_key_manager
    from src.auth.jwt_keys import get_jwt_key_manager
    from src.services.deposit_listener import get_deposit_listener
    from src.services.withdrawal_processor import get_withdrawal_processor

    logger.info("Accounting Module API starting...")

    # Initialize JWT key manager (derives keys from ROFL seed in TEE)
    jwt_key_manager = get_jwt_key_manager()
    await jwt_key_manager.initialize()
    logger.info("JWT key manager initialized")

    # Initialize AuthToken encryption key manager and sync to contract
    auth_token_key_manager = get_auth_token_key_manager()
    await auth_token_key_manager.initialize()
    logger.info("AuthToken key manager initialized")

    # Sync the encryption key to the contract (skipped when DISABLE_ROFL_KEYS is set).
    # TODO: Remove DISABLE_ROFL_KEYS check when Sapphire localnet e2e tests are available.
    if not os.getenv("DISABLE_ROFL_KEYS"):
        try:
            await auth_token_key_manager.sync_key_to_contract()
            logger.info("AuthToken encryption key synced to contract")
        except Exception as e:
            logger.warning(
                f"Failed to sync AuthToken encryption key to contract: {e}. "
                "Continuing startup - on fresh deployments the key will be set later, "
                "on restarts the key may already be set."
            )

    deposit_listener = get_deposit_listener()
    await deposit_listener.start()
    logger.info("Deposit listener started")

    withdrawal_processor = get_withdrawal_processor()
    await withdrawal_processor.start()
    logger.info("Withdrawal processor started")

    yield

    await withdrawal_processor.stop()
    await deposit_listener.stop()
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
