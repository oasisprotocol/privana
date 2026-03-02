"""Flare FDC configuration for Coston2 testnet."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class FDCConfig:
    """Flare FDC configuration. Defaults are for Coston2 testnet."""

    coston2_rpc_url: str = "https://coston2-api.flare.network/ext/C/rpc"
    coston2_private_key: str = ""

    # Coston2 contract addresses
    hub_address: str = "0x48aC463d7975828989331F4De43341627b9c5f1D"
    fee_config_address: str = "0x191a1282Ac700edE65c5B0AaF313BAcC3eA7fC7e"
    verification_address: str = "0x075bf301fF07C4920e5261f93a0609640F53487D"

    # API endpoints
    verifier_base_url: str = "https://fdc-verifiers-testnet.flare.network/"
    da_layer_url: str = "https://ctn2-data-availability.flare.network/"
    api_key: str = "00000000-0000-0000-0000-000000000000"

    # Voting round timing
    voting_epoch_duration_s: int = 90
    first_voting_round_start_ts: int = 1658430000

    # Attestation parameters
    required_confirmations: int = 1

    # Proof retrieval
    max_wait_s: int = 600
    initial_proof_delay_s: int = 95

    # Source chain mapping (chain_id → FDC source identifier)
    source_ids: dict[int, str] = field(
        default_factory=lambda: {
            11155111: "testETH",
        }
    )


def load_fdc_config() -> FDCConfig:
    """Load FDC config from environment variables with defaults."""
    defaults = FDCConfig()

    source_ids = defaults.source_ids
    if raw := os.getenv("FDC_SOURCE_IDS"):
        source_ids = {int(k): v for k, v in json.loads(raw).items()}

    return FDCConfig(
        coston2_rpc_url=os.getenv("COSTON2_RPC_URL", defaults.coston2_rpc_url),
        coston2_private_key=os.getenv("COSTON2_PRIVATE_KEY", ""),
        hub_address=os.getenv("FDC_HUB_ADDRESS", defaults.hub_address),
        fee_config_address=os.getenv("FDC_FEE_CONFIG_ADDRESS", defaults.fee_config_address),
        verification_address=os.getenv("FDC_VERIFICATION_ADDRESS", defaults.verification_address),
        verifier_base_url=os.getenv("FDC_VERIFIER_BASE_URL", defaults.verifier_base_url),
        da_layer_url=os.getenv("FDC_DA_LAYER_URL", defaults.da_layer_url),
        api_key=os.getenv("FDC_API_KEY", defaults.api_key),
        voting_epoch_duration_s=int(
            os.getenv("FDC_VOTING_EPOCH_DURATION_S", str(defaults.voting_epoch_duration_s))
        ),
        first_voting_round_start_ts=int(
            os.getenv("FDC_FIRST_VOTING_ROUND_START_TS", str(defaults.first_voting_round_start_ts))
        ),
        required_confirmations=int(
            os.getenv("FDC_REQUIRED_CONFIRMATIONS", str(defaults.required_confirmations))
        ),
        initial_proof_delay_s=int(
            os.getenv("FDC_INITIAL_PROOF_DELAY_S", str(defaults.initial_proof_delay_s))
        ),
        max_wait_s=int(os.getenv("FDC_MAX_WAIT_S", str(defaults.max_wait_s))),
        source_ids=source_ids,
    )
