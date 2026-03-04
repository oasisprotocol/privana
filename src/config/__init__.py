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

ALCHEMY_CHAIN_SUBDOMAINS: Dict[int, str] = {
    84532: "base-sepolia",
}

CHAIN_NAMES: Dict[int, str] = {
    84532: "Base Sepolia",
}

NATIVE_TOKEN_SYMBOLS: Dict[int, str] = {
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


def _build_chain_rpc_urls(alchemy_api_key: Optional[str]) -> Dict[int, str]:
    if not alchemy_api_key or alchemy_api_key == "your-alchemy-api-key-here":
        logging.warning(
            "ALCHEMY_API_KEY not configured. Proof generation will fail. "
            "Get an API key from https://dashboard.alchemy.com/"
        )
        return {}

    rpc_urls = {}
    for chain_id, subdomain in ALCHEMY_CHAIN_SUBDOMAINS.items():
        rpc_urls[chain_id] = f"https://{subdomain}.g.alchemy.com/v2/{alchemy_api_key}"

    return rpc_urls


def load_settings(refresh: bool = False) -> Settings:
    """Load settings, optionally refreshing cached values."""

    global _settings
    if _settings is None or refresh:
        alchemy_api_key = os.getenv("ALCHEMY_API_KEY")
        chain_rpc_urls = _build_chain_rpc_urls(alchemy_api_key)

        _settings = Settings(
            api_host=os.getenv("API_HOST", _defaults.api_host),
            api_port=_get_int("API_PORT", _defaults.api_port),
            log_level=os.getenv("LOG_LEVEL", _defaults.log_level),
            environment=os.getenv("ENVIRONMENT", _defaults.environment),
            accounting_contract_address=os.getenv(
                "ACCOUNTING_CONTRACT_ADDRESS", _defaults.accounting_contract_address
            ),
            rofl_adapter_address=os.getenv("ROFL_ADAPTER_ADDRESS", _defaults.rofl_adapter_address),
            sapphire_chain_id=_get_int("SAPPHIRE_CHAIN_ID", _defaults.sapphire_chain_id),
            sapphire_rpc_url=os.getenv("SAPPHIRE_RPC_URL", _defaults.sapphire_rpc_url),
            accounting_gas_limit=_get_int("ACCOUNTING_GAS_LIMIT", _defaults.accounting_gas_limit),
            chain_rpc_urls=chain_rpc_urls,
            deposit_poll_interval=_get_int(
                "DEPOSIT_POLL_INTERVAL", _defaults.deposit_poll_interval
            ),
            withdrawal_poll_interval=_get_int(
                "WITHDRAWAL_POLL_INTERVAL", _defaults.withdrawal_poll_interval
            ),
            withdrawal_resolution_timeout=_get_int(
                "WITHDRAWAL_RESOLUTION_TIMEOUT", _defaults.withdrawal_resolution_timeout
            ),
            min_withdrawal_gas_balance=_get_int(
                "MIN_WITHDRAWAL_GAS_BALANCE", _defaults.min_withdrawal_gas_balance
            ),
            relay_poll_interval=_get_int(
                "RELAY_POLL_INTERVAL", _defaults.relay_poll_interval
            ),
        )
    return _settings


__all__ = ["load_settings", "CHAIN_NAMES", "NATIVE_TOKEN_SYMBOLS", "ERC20_TOKENS"]
