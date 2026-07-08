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
    # Deposit discovery (GET /deposits/pending) scan bounds, in blocks.
    # Defaults suit ~12s blocks; override per chain for faster block times.
    discovery_lookback_blocks: int = 300  # ~1h default scan window
    discovery_max_lookback_blocks: int = 7_200  # ~24h clamp for client requests
    discovery_scan_chunk_blocks: int = 5_000  # per eth_getLogs call (Alchemy-safe)

    def __post_init__(self):
        if self.gas_funding_amount_wei >= self.min_deposit_native_wei:
            raise ValueError(
                f"chain {self.chain_id}: gas_funding_amount_wei "
                f"({self.gas_funding_amount_wei}) must be strictly less than "
                f"min_deposit_native_wei ({self.min_deposit_native_wei}) — "
                "otherwise a gas-funding transfer could satisfy the min-deposit "
                "threshold and be claimed via /deposits/check after restart."
            )
        if not (0 < self.discovery_lookback_blocks <= self.discovery_max_lookback_blocks):
            raise ValueError(
                f"chain {self.chain_id}: discovery_lookback_blocks must be in "
                f"[1, discovery_max_lookback_blocks]"
            )
        if self.discovery_scan_chunk_blocks <= 0:
            raise ValueError(f"chain {self.chain_id}: discovery_scan_chunk_blocks must be positive")


# ─── Chain definitions (single source of truth) ────────────────────────

CHAIN_CONFIGS: Dict[int, ChainConfig] = {
    84532: ChainConfig(
        chain_id=84532,
        finality_depth=15,  # Base Sepolia (OP Stack)
        min_deposit_native_wei=1_000_000_000_000_000,  # 0.001 ETH
        min_deposit_erc20_wei=1_000_000,  # 1 USDC (6 decimals)
        gas_funding_amount_wei=200_000_000_000_000,  # 0.0002 ETH (~65k gas * 3 gwei)
        l2_type=L2Type.OP_STACK,
        discovery_lookback_blocks=1_800,  # ~1h at 2s blocks
        discovery_max_lookback_blocks=43_200,  # ~24h at 2s blocks
    ),
    11155111: ChainConfig(
        chain_id=11155111,
        finality_depth=2,  # Ethereum Sepolia Testnet/Localnet UX
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
