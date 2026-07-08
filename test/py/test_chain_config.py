# test/py/test_chain_config.py
import pytest

from src.config import (
    ALCHEMY_CHAIN_SUBDOMAINS,
    CHAIN_NAMES,
    NATIVE_TOKEN_DECIMALS,
    NATIVE_TOKEN_NAMES,
    NATIVE_TOKEN_SYMBOLS,
)
from src.config.chain_config import (
    MIN_DEPOSIT_ERC20_WEI,
    MIN_DEPOSIT_NATIVE_WEI,
    ChainConfig,
    get_finality_depth,
)


def _make_config(**overrides) -> ChainConfig:
    """Build a valid ChainConfig, overriding individual fields per test."""
    params = dict(
        chain_id=1,
        finality_depth=2,
        min_deposit_native_wei=1_000_000,
        min_deposit_erc20_wei=1_000_000,
        gas_funding_amount_wei=100_000,
    )
    params.update(overrides)
    return ChainConfig(**params)


def test_known_chain_finality():
    assert get_finality_depth(84532) == 15  # Base Sepolia
    assert get_finality_depth(11155111) == 2  # Eth Sepolia Testnet/Localnet


def test_unknown_chain_uses_default():
    assert get_finality_depth(999999) == 32  # conservative default


def test_minimum_deposits_are_positive():
    for chain_id, amount in MIN_DEPOSIT_NATIVE_WEI.items():
        assert amount > 0, f"chain {chain_id} native minimum must be > 0"
    for chain_id, amount in MIN_DEPOSIT_ERC20_WEI.items():
        assert amount > 0, f"chain {chain_id} ERC20 minimum must be > 0"


def test_ethereum_sepolia_runtime_metadata():
    assert ALCHEMY_CHAIN_SUBDOMAINS[11155111] == "eth-sepolia"
    assert CHAIN_NAMES[11155111] == "Ethereum Sepolia"
    assert NATIVE_TOKEN_SYMBOLS[11155111] == "ETH"
    assert NATIVE_TOKEN_NAMES[11155111] == "Ether"
    assert NATIVE_TOKEN_DECIMALS[11155111] == 18


def test_valid_config_constructs():
    cfg = _make_config()
    assert cfg.discovery_lookback_blocks == 300
    assert cfg.discovery_lookback_blocks <= cfg.discovery_max_lookback_blocks


def test_zero_lookback_rejected():
    with pytest.raises(ValueError, match="discovery_lookback_blocks"):
        _make_config(discovery_lookback_blocks=0)


def test_lookback_above_max_rejected():
    with pytest.raises(ValueError, match="discovery_lookback_blocks"):
        _make_config(
            discovery_lookback_blocks=1_001,
            discovery_max_lookback_blocks=1_000,
        )


def test_zero_scan_chunk_rejected():
    with pytest.raises(ValueError, match="discovery_scan_chunk_blocks"):
        _make_config(discovery_scan_chunk_blocks=0)


def test_negative_scan_chunk_rejected():
    with pytest.raises(ValueError, match="discovery_scan_chunk_blocks"):
        _make_config(discovery_scan_chunk_blocks=-1)


def test_gas_funding_not_below_min_deposit_rejected():
    with pytest.raises(ValueError, match="gas_funding_amount_wei"):
        _make_config(
            gas_funding_amount_wei=1_000_000,
            min_deposit_native_wei=1_000_000,
        )
