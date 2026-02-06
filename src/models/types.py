"""Type definitions for the Accounting Module API."""

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Settings:
    """Application configuration settings."""

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    environment: str = "development"

    accounting_contract_address: str = "0x0000000000000000000000000000000000000000"
    rofl_adapter_address: str = "0x0000000000000000000000000000000000000000"
    sapphire_chain_id: int = 23295
    sapphire_rpc_url: str = "https://testnet.sapphire.oasis.io"
    accounting_gas_limit: int = 500_000
    chain_rpc_urls: Dict[int, str] = field(default_factory=dict)
    deposit_poll_interval: int = 1
    withdrawal_poll_interval: int = 12
    withdrawal_resolution_timeout: int = 60
    min_withdrawal_gas_balance: int = 10_000_000_000_000  # 0.00001 ETH in wei
