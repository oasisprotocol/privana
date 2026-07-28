"""Type definitions for the Accounting Module API."""

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class Settings:
    """Application configuration settings."""

    api_host: str
    api_port: int
    log_level: str
    environment: str
    cors_allowed_origins: str

    accounting_contract_address: str
    sapphire_chain_id: int
    sapphire_rpc_url: str
    accounting_gas_limit: int
    chain_rpc_urls: Dict[int, str]
    gas_prices_wei: Dict[int, int]
    token_infos: List[Dict[str, Any]]
    withdrawal_poll_interval: int
    withdrawal_resolution_timeout: int
    min_withdrawal_gas_balance: int

    auth_token_validity_seconds: int
    # Allow-list of SIWE domains. A SIWE message's ``domain`` field must match
    # one of these (after canonicalization) for authentication to succeed.
    siwe_domains: Tuple[str, ...]

    auth_token_storage_dir: str
    auth_clients_json: str
    auth_code_ttl_seconds: int
    auth_rate_limit_window_seconds: int
    auth_nonce_rate_limit: int
    auth_login_rate_limit: int
    auth_authorize_rate_limit: int
    auth_token_rate_limit: int
    trust_x_forwarded_for: bool

    moonpay_api_key: str
    moonpay_secret_key: str
    onramp_intent_signing_key_id: str
    onramp_intent_previous_signing_key_ids: Tuple[str, ...]
    moonpay_api_base_url: str
    moonpay_webhook_secret_key: str
    moonpay_allowed_hosts: Tuple[str, ...]
    moonpay_allowed_currency_codes: Tuple[str, ...]
    moonpay_webhook_tolerance_seconds: int
