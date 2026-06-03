"""Configuration management for the Accounting Module API."""

import logging
import os
from typing import Dict, Optional, Set, Tuple

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
    23295: "Sapphire Testnet",
}

NATIVE_TOKEN_SYMBOLS: Dict[int, str] = {
    84532: "ETH",
    23295: "ROSE",
}

NATIVE_TOKEN_NAMES: Dict[int, str] = {
    84532: "Ether",
    23295: "ROSE",
}

NATIVE_TOKEN_DECIMALS: Dict[int, int] = {
    84532: 18,
    23295: 18,
}

DEFAULT_SIWE_ALLOWED_CHAIN_IDS: Set[int] = {
    1,  # Ethereum Mainnet
    8453,  # Base Mainnet
    84532,  # Base Sepolia
    23294,  # Sapphire Mainnet
    23295,  # Sapphire Testnet
    11155111,  # Ethereum Sepolia
}


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _parse_chain_ids(value: Optional[str]) -> Set[int]:
    """Parse comma-separated chain IDs (supports hex like 0x5afe)."""
    if not value:
        return set()
    return {int(x.strip(), 0) for x in value.split(",") if x.strip()}


def _parse_siwe_domains(value: Optional[str]) -> Tuple[str, ...]:
    """Parse the SIWE domain allow-list from ``SIWE_DOMAINS`` (comma-separated).

    Order is preserved; canonical-form dedupe is handled downstream in
    ``siwe_config``.
    """
    if not value:
        return ()
    return tuple(stripped for piece in value.split(",") if (stripped := piece.strip()))


def _get_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be a boolean")


def _build_chain_rpc_urls(
    alchemy_api_key: Optional[str],
    sapphire_chain_id: int,
    sapphire_rpc_url: str,
) -> Dict[int, str]:
    rpc_urls: Dict[int, str] = {}

    if not alchemy_api_key or alchemy_api_key == "your-alchemy-api-key-here":
        logging.warning(
            "ALCHEMY_API_KEY not configured. Deposit verification will fail. "
            "Get an API key from https://dashboard.alchemy.com/"
        )
    else:
        for chain_id, subdomain in ALCHEMY_CHAIN_SUBDOMAINS.items():
            rpc_urls[chain_id] = f"https://{subdomain}.g.alchemy.com/v2/{alchemy_api_key}"

    if sapphire_rpc_url:
        rpc_urls[sapphire_chain_id] = sapphire_rpc_url

    return rpc_urls


def load_settings(refresh: bool = False) -> Settings:
    """Load settings, optionally refreshing cached values."""

    global _settings
    if _settings is None or refresh:
        alchemy_api_key = os.getenv("ALCHEMY_API_KEY")
        sapphire_chain_id = _get_int("SAPPHIRE_CHAIN_ID", _defaults.sapphire_chain_id)
        sapphire_rpc_url = os.getenv("SAPPHIRE_RPC_URL", _defaults.sapphire_rpc_url)
        chain_rpc_urls = _build_chain_rpc_urls(alchemy_api_key, sapphire_chain_id, sapphire_rpc_url)

        _settings = Settings(
            api_host=os.getenv("API_HOST", _defaults.api_host),
            api_port=_get_int("API_PORT", _defaults.api_port),
            log_level=os.getenv("LOG_LEVEL", _defaults.log_level),
            environment=os.getenv("ENVIRONMENT", _defaults.environment),
            cors_allowed_origins=os.getenv("CORS_ALLOWED_ORIGINS", _defaults.cors_allowed_origins),
            accounting_contract_address=os.getenv(
                "ACCOUNTING_CONTRACT_ADDRESS", _defaults.accounting_contract_address
            ),
            sapphire_chain_id=sapphire_chain_id,
            sapphire_rpc_url=sapphire_rpc_url,
            accounting_gas_limit=_get_int("ACCOUNTING_GAS_LIMIT", _defaults.accounting_gas_limit),
            chain_rpc_urls=chain_rpc_urls,
            withdrawal_poll_interval=_get_int(
                "WITHDRAWAL_POLL_INTERVAL", _defaults.withdrawal_poll_interval
            ),
            withdrawal_resolution_timeout=_get_int(
                "WITHDRAWAL_RESOLUTION_TIMEOUT", _defaults.withdrawal_resolution_timeout
            ),
            min_withdrawal_gas_balance=_get_int(
                "MIN_WITHDRAWAL_GAS_BALANCE", _defaults.min_withdrawal_gas_balance
            ),
            auth_token_validity_seconds=_get_int(
                "AUTH_TOKEN_VALIDITY_SECONDS", _defaults.auth_token_validity_seconds
            ),
            siwe_domains=_parse_siwe_domains(os.getenv("SIWE_DOMAINS")),
            siwe_allowed_chain_ids=_parse_chain_ids(os.getenv("SIWE_ALLOWED_CHAIN_IDS")),
            auth_token_storage_dir=os.getenv(
                "AUTH_TOKEN_STORAGE_DIR", _defaults.auth_token_storage_dir
            ),
            auth_clients_json=os.getenv(
                "AUTH_CLIENTS",
                os.getenv("OAUTH_CLIENTS", _defaults.auth_clients_json),
            ),
            auth_code_ttl_seconds=_get_int(
                "AUTH_CODE_TTL_SECONDS", _defaults.auth_code_ttl_seconds
            ),
            auth_rate_limit_window_seconds=_get_int(
                "AUTH_RATE_LIMIT_WINDOW_SECONDS", _defaults.auth_rate_limit_window_seconds
            ),
            auth_nonce_rate_limit=_get_int(
                "AUTH_NONCE_RATE_LIMIT", _defaults.auth_nonce_rate_limit
            ),
            auth_login_rate_limit=_get_int(
                "AUTH_LOGIN_RATE_LIMIT", _defaults.auth_login_rate_limit
            ),
            auth_authorize_rate_limit=_get_int(
                "AUTH_AUTHORIZE_RATE_LIMIT", _defaults.auth_authorize_rate_limit
            ),
            auth_token_rate_limit=_get_int(
                "AUTH_TOKEN_RATE_LIMIT", _defaults.auth_token_rate_limit
            ),
            trust_x_forwarded_for=_get_bool(
                "TRUST_X_FORWARDED_FOR", _defaults.trust_x_forwarded_for
            ),
            rofl_bridge_address=_get_str("ROFL_BRIDGE_ADDRESS", _defaults.rofl_bridge_address),
            xrose_address=_get_str("XROSE_ADDRESS", _defaults.xrose_address),
            bridge_mint_limit_wei=_get_int(
                "BRIDGE_MINT_LIMIT_WEI", _defaults.bridge_mint_limit_wei
            ),
            bridge_burn_limit_wei=_get_int(
                "BRIDGE_BURN_LIMIT_WEI", _defaults.bridge_burn_limit_wei
            ),
            bridge_route_reconcile_interval=_get_int(
                "BRIDGE_ROUTE_RECONCILE_INTERVAL",
                _defaults.bridge_route_reconcile_interval,
            ),
        )
    return _settings


__all__ = [
    "load_settings",
    "CHAIN_NAMES",
    "NATIVE_TOKEN_SYMBOLS",
    "DEFAULT_SIWE_ALLOWED_CHAIN_IDS",
]
