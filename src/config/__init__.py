"""Configuration management for the Accounting Module API."""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from web3 import Web3

from src.models.types import Settings

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


_settings: Optional[Settings] = None

DEFAULT_ONRAMP_INTENT_SIGNING_KEY_ID = "onramp_intent_signing_key.v1.key"

ALCHEMY_CHAIN_SUBDOMAINS: Dict[int, str] = {
    84532: "base-sepolia",
    11155111: "eth-sepolia",
}

CHAIN_NAMES: Dict[int, str] = {
    23295: "Sapphire Testnet",
    23293: "Sapphire Localnet",
    84532: "Base Sepolia",
    11155111: "Ethereum Sepolia",
}

NATIVE_TOKEN_SYMBOLS: Dict[int, str] = {
    23295: "ROSE",
    23293: "ROSE",
    84532: "ETH",
    11155111: "ETH",
}

NATIVE_TOKEN_NAMES: Dict[int, str] = {
    23295: "Rose",
    23293: "Rose",
    84532: "Ether",
    11155111: "Ether",
}

NATIVE_TOKEN_DECIMALS: Dict[int, int] = {
    23295: 18,
    23293: 18,
    84532: 18,
    11155111: 18,
}


def _get_int(name: str) -> int:
    value = os.getenv(name)
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _get_optional_int_fail_closed(name: str) -> int | None:
    """Parse an optional integration value without blocking application startup."""

    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value, 0)
    except ValueError:
        logging.error("%s is invalid; the affected integration will remain disabled", name)
        return None


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


def _build_chain_rpc_urls(
    alchemy_api_key: Optional[str],
    sapphire_chain_id: int,
    sapphire_rpc_url: Optional[str] = None,
) -> Dict[int, str]:
    """Map chain ID to RPC URL.

    Sapphire is seeded from its caller-supplied endpoint before the Alchemy key is
    checked, so a deployment without Alchemy still serves Sapphire.
    """
    rpc_urls: Dict[int, str] = {}

    if sapphire_rpc_url is None:
        sapphire_rpc_url = os.getenv("SAPPHIRE_RPC_URL")

    if sapphire_chain_id and sapphire_rpc_url:
        rpc_urls[sapphire_chain_id] = sapphire_rpc_url

    if not alchemy_api_key or alchemy_api_key == "your-alchemy-api-key-here":
        logging.warning(
            "ALCHEMY_API_KEY not configured. Deposit verification will fail for Alchemy chains. "
            "Get an API key from https://dashboard.alchemy.com/"
        )
        return rpc_urls

    for chain_id, subdomain in ALCHEMY_CHAIN_SUBDOMAINS.items():
        rpc_urls[chain_id] = f"https://{subdomain}.g.alchemy.com/v2/{alchemy_api_key}"

    return rpc_urls


def _build_gas_prices() -> Dict[int, int]:
    """Parse per-chain gas prices from the ACCOUNTING_GAS_PRICE env var.

    Expects a JSON object mapping chain_id in decimal to gas price in wei, e.g.
    ACCOUNTING_GAS_PRICE='{"84532": 1000000000, "11155111": 20000000000}'.
    """
    raw = os.getenv("ACCOUNTING_GAS_PRICE")
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ACCOUNTING_GAS_PRICE JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("ACCOUNTING_GAS_PRICE must be a JSON object mapping chain_id to gas price")

    gas_prices: Dict[int, int] = {}
    for chain_id_raw, gas_price_raw in parsed.items():
        gas_prices[int(chain_id_raw)] = int(gas_price_raw)

    return gas_prices


def _build_token_infos() -> List[Dict[str, Any]]:
    """Parse the ACCOUNTING_TOKEN_INFO env var: a JSON array of token descriptors.

    Each entry is ``{"chain_id": <int>}`` for a native token, or
    {"chain_id": <int>, "token_address": "0x..."}`` for an ERC20 token

    For example:
      ACCOUNTING_TOKEN_INFO='[
        {"chain_id": 84532},
        {"chain_id": 84532, "token_address": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"}
      ]'
    """
    raw = os.getenv("ACCOUNTING_TOKEN_INFO")
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ACCOUNTING_TOKEN_INFO JSON: {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError("ACCOUNTING_TOKEN_INFO must be a JSON array of token descriptors")

    token_infos: List[Dict[str, Any]] = []
    for index, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValueError(f"ACCOUNTING_TOKEN_INFO entry {index} must be a JSON object")

        if "chain_id" not in entry:
            raise ValueError(f"ACCOUNTING_TOKEN_INFO entry {index} missing chain_id")
        try:
            chain_id = int(entry["chain_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"ACCOUNTING_TOKEN_INFO entry {index} chain_id must be an integer"
            ) from exc

        token_address = entry.get("token_address")
        if token_address is not None:
            if not isinstance(token_address, str) or not Web3.is_address(token_address):
                raise ValueError(
                    f"ACCOUNTING_TOKEN_INFO entry {index} token_address is not a valid address"
                )

        token_infos.append({"chain_id": chain_id, "token_address": token_address})

    return token_infos


def load_settings(refresh: bool = False) -> Settings:
    """Load settings, optionally refreshing cached values."""

    global _settings
    if _settings is None or refresh:
        sapphire_chain_id = _get_int("SAPPHIRE_CHAIN_ID")
        sapphire_rpc_url = os.getenv("SAPPHIRE_RPC_URL")
        alchemy_api_key = os.getenv("ALCHEMY_API_KEY")
        chain_rpc_urls = _build_chain_rpc_urls(
            alchemy_api_key,
            sapphire_chain_id=sapphire_chain_id,
            sapphire_rpc_url=sapphire_rpc_url,
        )
        auth_token_storage_dir = os.getenv("AUTH_TOKEN_STORAGE_DIR", ".auth_tokens")
        onramp_provider = os.getenv("ONRAMP_PROVIDER")

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
            gas_prices_wei=_build_gas_prices(),
            token_infos=_build_token_infos(),
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
            onramp_provider=onramp_provider.strip().lower() if onramp_provider else None,
            onramp_intent_signing_key_id=os.getenv(
                "ONRAMP_INTENT_SIGNING_KEY_ID",
                DEFAULT_ONRAMP_INTENT_SIGNING_KEY_ID,
            ),
            onramp_intent_previous_signing_key_ids=_parse_csv_tuple(
                os.getenv("ONRAMP_INTENT_PREVIOUS_SIGNING_KEY_IDS")
            ),
            moonpay_api_base_url=os.getenv("MOONPAY_API_BASE_URL", "https://api.moonpay.com"),
            moonpay_webhook_secret_key=os.getenv("MOONPAY_WEBHOOK_SECRET_KEY"),
            moonpay_allowed_hosts=(_parse_csv_tuple(os.getenv("MOONPAY_ALLOWED_HOSTS"))),
            moonpay_allowed_currency_codes=(
                _parse_csv_tuple(os.getenv("MOONPAY_ALLOWED_CURRENCY_CODES"))
            ),
            moonpay_webhook_tolerance_seconds=_get_int(
                "MOONPAY_WEBHOOK_TOLERANCE_SECONDS",
            ),
            transak_api_key=os.getenv("TRANSAK_API_KEY"),
            transak_api_secret=os.getenv("TRANSAK_API_SECRET"),
            transak_api_base_url=os.getenv("TRANSAK_API_BASE_URL"),
            transak_gateway_base_url=os.getenv("TRANSAK_GATEWAY_BASE_URL"),
            transak_referrer_domain=os.getenv("TRANSAK_REFERRER_DOMAIN"),
            transak_client_ip_header=os.getenv("TRANSAK_CLIENT_IP_HEADER"),
            transak_crypto_currency_code=os.getenv("TRANSAK_CRYPTO_CURRENCY_CODE"),
            transak_network=os.getenv("TRANSAK_NETWORK"),
            transak_chain_id=_get_optional_int_fail_closed("TRANSAK_CHAIN_ID"),
            transak_token_address=os.getenv("TRANSAK_TOKEN_ADDRESS"),
        )
    return _settings


__all__ = [
    "load_settings",
    "CHAIN_NAMES",
    "NATIVE_TOKEN_SYMBOLS",
]
