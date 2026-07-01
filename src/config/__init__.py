"""Configuration management for the Accounting Module API."""

import logging
import os
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv

from src.models.types import Settings

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


_settings: Optional[Settings] = None

ALCHEMY_CHAIN_SUBDOMAINS: Dict[int, str] = {
    84532: "base-sepolia",
    11155111: "eth-sepolia",
}

CHAIN_NAMES: Dict[int, str] = {
    84532: "Base Sepolia",
    11155111: "Ethereum Sepolia",
}

NATIVE_TOKEN_SYMBOLS: Dict[int, str] = {
    84532: "ETH",
    11155111: "ETH",
}

NATIVE_TOKEN_NAMES: Dict[int, str] = {
    84532: "Ether",
    11155111: "Ether",
}

NATIVE_TOKEN_DECIMALS: Dict[int, int] = {
    84532: 18,
    11155111: 18,
}


def _get_int(name: str) -> int:
    value = os.getenv(name)
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _parse_siwe_domains(value: Optional[str]) -> Tuple[str, ...]:
    """Parse the SIWE domain allow-list from ``SIWE_DOMAINS`` (comma-separated).

    Order is preserved; canonical-form dedupe is handled downstream in
    ``siwe_config``.
    """
    if not value:
        return ()
    return tuple(stripped for piece in value.split(",") if (stripped := piece.strip()))


def _parse_csv_tuple(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(stripped for piece in value.split(",") if (stripped := piece.strip()))


def _get_bool(name: str) -> bool:
    value = os.getenv(name)
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be a boolean")


def _build_chain_rpc_urls(alchemy_api_key: Optional[str]) -> Dict[int, str]:
    if not alchemy_api_key or alchemy_api_key == "your-alchemy-api-key-here":
        logging.warning(
            "ALCHEMY_API_KEY not configured. Deposit verification will fail. "
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
        auth_token_storage_dir = os.getenv("AUTH_TOKEN_STORAGE_DIR", ".auth_tokens")

        _settings = Settings(
            api_host=os.getenv("API_HOST"),
            api_port=_get_int("API_PORT"),
            log_level=os.getenv("LOG_LEVEL"),
            environment=os.getenv("ENVIRONMENT"),
            cors_allowed_origins=os.getenv("CORS_ALLOWED_ORIGINS"),
            accounting_contract_address=os.getenv("ACCOUNTING_CONTRACT_ADDRESS"),
            sapphire_chain_id=_get_int("SAPPHIRE_CHAIN_ID"),
            sapphire_rpc_url=os.getenv("SAPPHIRE_RPC_URL"),
            accounting_gas_limit=_get_int("ACCOUNTING_GAS_LIMIT"),
            chain_rpc_urls=chain_rpc_urls,
            withdrawal_poll_interval=_get_int("WITHDRAWAL_POLL_INTERVAL"),
            withdrawal_resolution_timeout=_get_int("WITHDRAWAL_RESOLUTION_TIMEOUT"),
            min_withdrawal_gas_balance=_get_int("MIN_WITHDRAWAL_GAS_BALANCE"),
            auth_token_validity_seconds=_get_int("AUTH_TOKEN_VALIDITY_SECONDS"),
            siwe_domains=_parse_siwe_domains(os.getenv("SIWE_DOMAINS")),
            auth_token_storage_dir=auth_token_storage_dir,
            auth_clients_json=os.getenv("AUTH_CLIENTS"),
            auth_code_ttl_seconds=_get_int("AUTH_CODE_TTL_SECONDS"),
            auth_rate_limit_window_seconds=_get_int("AUTH_RATE_LIMIT_WINDOW_SECONDS"),
            auth_nonce_rate_limit=_get_int("AUTH_NONCE_RATE_LIMIT"),
            auth_login_rate_limit=_get_int("AUTH_LOGIN_RATE_LIMIT"),
            auth_authorize_rate_limit=_get_int("AUTH_AUTHORIZE_RATE_LIMIT"),
            auth_token_rate_limit=_get_int("AUTH_TOKEN_RATE_LIMIT"),
            trust_x_forwarded_for=_get_bool("TRUST_X_FORWARDED_FOR"),
            moonpay_api_key=os.getenv("MOONPAY_API_KEY"),
            moonpay_secret_key=os.getenv("MOONPAY_SECRET_KEY"),
            moonpay_intent_signing_key=os.getenv("MOONPAY_INTENT_SIGNING_KEY"),
            moonpay_api_base_url=os.getenv("MOONPAY_API_BASE_URL", "https://api.moonpay.com"),
            moonpay_webhook_secret_key=os.getenv("MOONPAY_WEBHOOK_SECRET_KEY"),
            moonpay_allowed_hosts=(_parse_csv_tuple(os.getenv("MOONPAY_ALLOWED_HOSTS"))),
            moonpay_allowed_currency_codes=(
                _parse_csv_tuple(os.getenv("MOONPAY_ALLOWED_CURRENCY_CODES"))
            ),
            moonpay_webhook_tolerance_seconds=_get_int(
                "MOONPAY_WEBHOOK_TOLERANCE_SECONDS",
            ),
        )
    return _settings


__all__ = [
    "load_settings",
    "CHAIN_NAMES",
    "NATIVE_TOKEN_SYMBOLS",
]
