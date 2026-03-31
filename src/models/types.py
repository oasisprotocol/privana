"""Type definitions for the Accounting Module API."""

from dataclasses import dataclass, field
from typing import Dict, Set


@dataclass
class Settings:
    """Application configuration settings."""

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    environment: str = "development"
    cors_allowed_origins: str = ""

    accounting_contract_address: str = "0x0000000000000000000000000000000000000000"
    rofl_adapter_address: str = "0x0000000000000000000000000000000000000000"
    sapphire_chain_id: int = 23295
    sapphire_rpc_url: str = "https://testnet.sapphire.oasis.io"
    accounting_gas_limit: int = 500_000
    chain_rpc_urls: Dict[int, str] = field(default_factory=dict)
    deposit_poll_interval: int = 1
    deposit_batch_size: int = 100
    deposit_lookback_blocks: int = 100
    deposit_reset_state: bool = False
    withdrawal_poll_interval: int = 12
    withdrawal_resolution_timeout: int = 60
    min_withdrawal_gas_balance: int = 10_000_000_000_000  # 0.00001 ETH in wei

    auth_token_validity_seconds: int = 24 * 60 * 60
    siwe_domain: str = ""

    # Allowed chain IDs for SIWE authentication.
    # If empty, defaults to DEFAULT_SIWE_ALLOWED_CHAIN_IDS plus configured RPC chain IDs.
    siwe_allowed_chain_ids: Set[int] = field(default_factory=set)
    auth_token_storage_dir: str = ".auth_tokens"
    auth_clients_json: str = "[]"
    auth_code_ttl_seconds: int = 120
    auth_rate_limit_window_seconds: int = 60
    auth_nonce_rate_limit: int = 30
    auth_login_rate_limit: int = 10
    auth_authorize_rate_limit: int = 10
    auth_token_rate_limit: int = 20
    trust_x_forwarded_for: bool = False
