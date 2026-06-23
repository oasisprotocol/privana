"""Single authoritative source for bridge quote config.

Both the quote and signed-submit endpoints read this
module; ``quote_config_version`` is the contract that lets submit detect a
config rotation that happened between quote and submit.

``route_address`` is intentionally *not* part of the hashed config —
the TEE reconciler may rotate ``roflBridgeAddress[destChainId]`` mid-flight,
and a rotation must not stamp every outstanding quote stale. The route is
read on-chain per quote; only operator-pinned scalars live in the hash.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from eth_utils import keccak

from src.config.bridge_validation import destination_chain_ids
from src.models.types import Settings

# MUST equal `BridgeLib.GAS_LIMIT_NATIVE_RELEASE` — quote reserve and contract
# consumption must agree or the user's `maxGasCost` is off.
SAPPHIRE_RELEASE_GAS_LIMIT: int = 25_000

# Pads the user's locked reserve for operator `setGasPrice` bumps between sign
# and resolve. Over-cap reverts `GasBudgetExceeded`; the user re-quotes.
SAPPHIRE_RELEASE_GAS_SAFETY_MARGIN_BPS: int = 11_000

# MUST equal `BridgeLib.MAX_SAPPHIRE_RELEASE_RESERVE`. Cap on the user-signed
# Sapphire reserve; the contract enforces the same bound at request time.
MAX_SAPPHIRE_RELEASE_RESERVE_WEI: int = 10_000_000_000_000_000

# Quote envelope lifetime. Bounds the window between quote and submit.
BRIDGE_QUOTE_TTL_SECONDS: int = 300


@dataclass(frozen=True, slots=True)
class BridgeQuoteConfig:
    """Operator-pinned parameters that govern a bridge-withdrawal quote."""

    sapphire_chain_id: int
    destination_chain_ids: tuple[int, ...]
    token_symbol: str = "ROSE"
    token_decimals: int = 18
    fee_model_sapphire: str = "native_gas_user_paid"
    fee_model_registered: str = "foreign_gas_operator_paid"


def canonical_config_json(cfg: BridgeQuoteConfig) -> str:
    """Serialise the config to canonical JSON: sorted keys, no whitespace.

    Tuples become lists in JSON; ``destination_chain_ids`` is already sorted
    before construction so the wire form is order-stable across processes.
    """
    return json.dumps(asdict(cfg), separators=(",", ":"), sort_keys=True)


def quote_config_version(cfg: BridgeQuoteConfig) -> str:
    """Compute the ``bridge-quote-v1:<keccak>`` version string."""
    digest = keccak(canonical_config_json(cfg).encode("utf-8"))
    return f"bridge-quote-v1:0x{digest.hex()}"


def get_bridge_quote_config(settings: Settings) -> BridgeQuoteConfig:
    """Build the quote config from ``Settings``.

    Not cached — ``Settings`` is a mutable dataclass and therefore unhashable,
    and keccak of a small JSON payload is well under a millisecond. The quote
    endpoint is human-rate; the per-call cost is negligible compared to the
    on-chain reads it issues.
    """
    return BridgeQuoteConfig(
        sapphire_chain_id=settings.sapphire_chain_id,
        destination_chain_ids=tuple(sorted(destination_chain_ids(settings))),
    )
