"""Startup validation for ROSE bridge environment variables.

These checks run once from the FastAPI lifespan, not from ``load_settings()`` —
module-import-time validation would break every test that imports
``src.config`` without bridge env vars set.
"""

from web3 import Web3

from src.models.types import Settings

# Denominator for basis-point (bps) settings — 1 bp = 1/10000.
# Used wherever a ``*_bps`` value is converted into a fraction.
BASIS_POINTS_DENOMINATOR: int = 10_000


def _require_address(env_name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{env_name} is required (got empty value).")
    if not Web3.is_address(value):
        raise ValueError(f"{env_name} must be a 0x-prefixed 20-byte EVM address (got {value!r}).")


def destination_chain_ids(settings: Settings) -> set[int]:
    """Phase-0 destination set: every RPC-configured chain except Sapphire.

    Loose coupling — assumes any non-Sapphire chain with RPC config is a
    bridge destination. Holds for Phase-0 (Base Sepolia is the only non-Sapphire
    RPC). When that ceases to be true, switch this to read the on-chain
    ``roflBridgeAddress`` mapping directly.
    """
    return set(settings.chain_rpc_urls.keys()) - {settings.sapphire_chain_id}


def validate_bridge_settings(settings: Settings) -> None:
    """Raise ``ValueError`` if any bridge field is missing or off-spec."""
    _require_address("ROFL_BRIDGE_ADDRESS", settings.rofl_bridge_address)
    _require_address("XROSE_ADDRESS", settings.xrose_address)

    if settings.bridge_mint_limit_wei <= 0:
        raise ValueError(
            "BRIDGE_MINT_LIMIT_WEI must be a positive integer "
            f"(got {settings.bridge_mint_limit_wei})."
        )
    if settings.bridge_burn_limit_wei <= 0:
        raise ValueError(
            "BRIDGE_BURN_LIMIT_WEI must be a positive integer "
            f"(got {settings.bridge_burn_limit_wei})."
        )
