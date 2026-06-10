"""Per-chain deposit and sweep configuration."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Dict


class L2Type(StrEnum):
    NONE = "none"  # L1 chains — no L1 data fee
    OP_STACK = "op_stack"
    ARBITRUM = "arbitrum"


@dataclass(frozen=True)
class ChainConfig:
    """All per-chain deposit and sweep settings in one place.

    Adding a new chain requires a single entry here — no parallel dicts to keep in sync.
    """

    chain_id: int
    finality_depth: int
    min_deposit_native_wei: int
    min_deposit_erc20_wei: int
    gas_funding_amount_wei: int
    l2_type: L2Type = L2Type.NONE

    def __post_init__(self):
        if self.gas_funding_amount_wei >= self.min_deposit_native_wei:
            raise ValueError(
                f"chain {self.chain_id}: gas_funding_amount_wei "
                f"({self.gas_funding_amount_wei}) must be strictly less than "
                f"min_deposit_native_wei ({self.min_deposit_native_wei}) — "
                "otherwise a gas-funding transfer could satisfy the min-deposit "
                "threshold and be claimed via /deposits/check after restart."
            )


# ─── Chain definitions (single source of truth) ────────────────────────

CHAIN_CONFIGS: Dict[int, ChainConfig] = {
    84532: ChainConfig(
        chain_id=84532,
        finality_depth=15,  # Base Sepolia (OP Stack)
        min_deposit_native_wei=1_000_000_000_000_000,  # 0.001 ETH
        min_deposit_erc20_wei=1_000_000,  # 1 USDC (6 decimals)
        gas_funding_amount_wei=200_000_000_000_000,  # 0.0002 ETH (~65k gas * 3 gwei)
        l2_type=L2Type.OP_STACK,
    ),
    11155111: ChainConfig(
        chain_id=11155111,
        finality_depth=32,  # Ethereum Sepolia (2 epochs)
        min_deposit_native_wei=50_000_000_000_000_000,  # 0.05 ETH
        min_deposit_erc20_wei=50_000_000,  # ERC-20 base-unit floor (token decimals vary)
        gas_funding_amount_wei=2_000_000_000_000_000,  # 0.002 ETH (~65k gas * 30 gwei)
    ),
}

DEFAULT_FINALITY_DEPTH = 32

# Gas limits for sweep transactions (chain-independent)
SWEEP_GAS_LIMIT_NATIVE = 21_000
SWEEP_GAS_LIMIT_ERC20 = 65_000
GAS_FUNDING_GAS_LIMIT = 21_000

# ERC20 Transfer event topic
TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# ─── Derived dicts for backward compat ──────────────────────────────────

FINALITY_DEPTHS: Dict[int, int] = {c.chain_id: c.finality_depth for c in CHAIN_CONFIGS.values()}
MIN_DEPOSIT_NATIVE_WEI: Dict[int, int] = {
    c.chain_id: c.min_deposit_native_wei for c in CHAIN_CONFIGS.values()
}
MIN_DEPOSIT_ERC20_WEI: Dict[int, int] = {
    c.chain_id: c.min_deposit_erc20_wei for c in CHAIN_CONFIGS.values()
}
GAS_FUNDING_AMOUNT_WEI: Dict[int, int] = {
    c.chain_id: c.gas_funding_amount_wei for c in CHAIN_CONFIGS.values()
}


def get_finality_depth(chain_id: int) -> int:
    """Get the required confirmation depth for a chain."""
    cfg = CHAIN_CONFIGS.get(chain_id)
    if cfg:
        return cfg.finality_depth
    return DEFAULT_FINALITY_DEPTH
