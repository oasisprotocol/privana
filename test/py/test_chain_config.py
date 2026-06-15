# test/py/test_chain_config.py
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
    get_finality_depth,
)


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
