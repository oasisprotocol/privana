"""Main entry point for the Accounting Module API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.config import load_settings

logger = logging.getLogger(__name__)

settings = load_settings()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifecycle.
    
    Args:
        app: FastAPI application instance
    """
    logger.info("Accounting Module API starting...")
    yield
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment.lower() == "development",
        log_level=settings.log_level.lower(),
    )
