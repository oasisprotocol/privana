"""Configuration management for the Accounting Module API."""

import logging
import os
from typing import Dict, Optional

from src.models.types import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


_settings: Optional[Settings] = None
_defaults = Settings()

DEFAULT_CHAIN_RPC_URLS: Dict[int, str] = {
    # 8453: "https://mainnet.base.org",
    84532: "https://base-sepolia-rpc.publicnode.com",
}

CHAIN_NAMES: Dict[int, str] = {
    # 8453: "Base",
    84532: "Base Sepolia",
}

NATIVE_TOKEN_SYMBOLS: Dict[int, str] = {
    # 8453: "ETH",
    84532: "ETH",
}

ERC20_TOKENS: Dict[int, Dict[str, str]] = {
    84532: {
        "0x12084E1A0fe92b5ab803a81A0Ae54D91040F89ca": "USDC",
    }
}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def load_settings(refresh: bool = False) -> Settings:
    """Load settings, optionally refreshing cached values."""

    global _settings
    if _settings is None or refresh:
        _settings = Settings(
            api_host=os.getenv("API_HOST", _defaults.api_host),
            api_port=_get_int("API_PORT", _defaults.api_port),
            log_level=os.getenv("LOG_LEVEL", _defaults.log_level),
            environment=os.getenv("ENVIRONMENT", _defaults.environment),
            accounting_contract_address=os.getenv(
                "ACCOUNTING_CONTRACT_ADDRESS", _defaults.accounting_contract_address
            ),
            sapphire_chain_id=_get_int(
                "SAPPHIRE_CHAIN_ID", _defaults.sapphire_chain_id
            ),
            sapphire_rpc_url=os.getenv("SAPPHIRE_RPC_URL", _defaults.sapphire_rpc_url),
            accounting_gas_limit=_get_int(
                "ACCOUNTING_GAS_LIMIT", _defaults.accounting_gas_limit
            ),
            chain_rpc_urls=dict(DEFAULT_CHAIN_RPC_URLS),
            deposit_poll_interval=_get_int(
                "DEPOSIT_POLL_INTERVAL", _defaults.deposit_poll_interval
            ),
        )
    return _settings


__all__ = ["load_settings", "CHAIN_NAMES", "NATIVE_TOKEN_SYMBOLS", "ERC20_TOKENS"]
