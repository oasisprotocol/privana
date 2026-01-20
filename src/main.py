"""Main entry point for the Accounting Module API."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from src.api.routes import router
from src.config import load_settings

logger = logging.getLogger(__name__)

settings = load_settings()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Manage application lifecycle.

    Args:
        app: FastAPI application instance
    """
    from src.services.deposit_listener import get_deposit_listener
    from src.services.withdrawal_resolver import get_withdrawal_resolver

    logger.info("Accounting Module API starting...")

    deposit_listener = get_deposit_listener()
    await deposit_listener.start()
    logger.info("Deposit listener started")

    withdrawal_resolver = get_withdrawal_resolver()
    await withdrawal_resolver.start()
    logger.info("Withdrawal resolver started")

    yield

    await withdrawal_resolver.stop()
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Landing page for the Accounting Module API."""
    template_path = Path(__file__).parent / "templates" / "landing.html"
    return template_path.read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment.lower() == "development",
        log_level=settings.log_level.lower(),
    )
