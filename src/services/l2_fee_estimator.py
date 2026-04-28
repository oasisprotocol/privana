"""Estimate L1 data fees for sweep transactions on L2 chains.

Different L2 architectures charge different fees for posting tx data to
Ethereum L1. This module provides chain-type-aware fee estimation so
sweep cost calculations account for the full execution + data cost.
"""

import logging

from web3 import AsyncWeb3

from src.config.chain_config import CHAIN_CONFIGS, L2Type

logger = logging.getLogger(__name__)

SAFETY_MARGIN_NUMERATOR = 125
SAFETY_MARGIN_DENOMINATOR = 100

# Realistic serialized tx sizes for fee estimation (non-zero bytes
# give a slightly pessimistic estimate since they compress less)
NATIVE_TX_SIZE = 112
ERC20_TX_SIZE = 180


# ── OP Stack ────────────────────────────────────────────────────────────

OP_GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"

OP_GAS_PRICE_ORACLE_ABI = [
    {
        "inputs": [{"name": "_data", "type": "bytes"}],
        "name": "getL1Fee",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


async def _estimate_op_stack(w3: AsyncWeb3, is_erc20: bool) -> int:
    size = ERC20_TX_SIZE if is_erc20 else NATIVE_TX_SIZE
    dummy_data = bytes([0xAA] * size)

    oracle = w3.eth.contract(
        address=w3.to_checksum_address(OP_GAS_PRICE_ORACLE),
        abi=OP_GAS_PRICE_ORACLE_ABI,
    )
    fee: int = await oracle.functions.getL1Fee(dummy_data).call()
    return fee * SAFETY_MARGIN_NUMERATOR // SAFETY_MARGIN_DENOMINATOR


# ── Arbitrum ────────────────────────────────────────────────────────────

ARB_NODE_INTERFACE = "0x00000000000000000000000000000000000000C8"

ARB_NODE_INTERFACE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "contractCreation", "type": "bool"},
            {"name": "data", "type": "bytes"},
        ],
        "name": "gasEstimateL1Component",
        "outputs": [
            {"name": "gasEstimateForL1", "type": "uint64"},
            {"name": "baseFee", "type": "uint256"},
            {"name": "l1BaseFeeEstimate", "type": "uint256"},
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]


async def _estimate_arbitrum(w3: AsyncWeb3, is_erc20: bool) -> int:
    # NOTE: Arbitrum bakes L1 data cost into gas units, so the Solidity
    # contract's hardcoded gasLimitNative = 21_000 may be insufficient
    # for Arbitrum sweeps. This is a known limitation pending a contract
    # change to use chain-aware gas limits.
    dummy_data = bytes([0xAA] * 68) if is_erc20 else b""

    node_iface = w3.eth.contract(
        address=w3.to_checksum_address(ARB_NODE_INTERFACE),
        abi=ARB_NODE_INTERFACE_ABI,
    )
    gas_for_l1, base_fee, _ = await node_iface.functions.gasEstimateL1Component(
        w3.to_checksum_address("0x0000000000000000000000000000000000000001"),
        False,
        dummy_data,
    ).call()

    fee = gas_for_l1 * base_fee
    return fee * SAFETY_MARGIN_NUMERATOR // SAFETY_MARGIN_DENOMINATOR


# ── Public API ──────────────────────────────────────────────────────────


async def estimate_l1_data_fee(w3: AsyncWeb3, chain_id: int, is_erc20: bool = False) -> int:
    """Return the estimated L1 data fee (in wei) for a sweep tx on *chain_id*.

    Returns 0 for L1 chains or on any estimation failure — better to
    fall back on the dust buffer than crash the sweep.
    """
    cfg = CHAIN_CONFIGS.get(chain_id)
    if cfg is None:
        logger.warning("No chain config for chain_id=%d, assuming no L1 data fee", chain_id)
        return 0

    l2_type = getattr(cfg, "l2_type", L2Type.NONE)

    match l2_type:
        case L2Type.NONE:
            return 0
        case L2Type.OP_STACK:
            estimator = _estimate_op_stack
        case L2Type.ARBITRUM:
            estimator = _estimate_arbitrum
        case _:
            logger.warning("Unknown L2 type %r for chain_id=%d", l2_type, chain_id)
            return 0

    try:
        fee = await estimator(w3, is_erc20)
        tx_kind = "ERC20" if is_erc20 else "native"
        logger.info(
            "L1 data fee estimate for %s sweep on chain_id=%d: %d wei (%.6f ETH)",
            tx_kind,
            chain_id,
            fee,
            fee / 1e18,
        )
        return fee
    except Exception:
        logger.exception("Failed to estimate L1 data fee for chain_id=%d", chain_id)
        return 0
