# test/py/test_chain_config.py
import pytest

from src.config import (
    ALCHEMY_CHAIN_SUBDOMAINS,
    CHAIN_NAMES,
    NATIVE_TOKEN_DECIMALS,
    NATIVE_TOKEN_NAMES,
    NATIVE_TOKEN_SYMBOLS,
    load_settings,
)
from src.config.bridge_validation import (
    destination_chain_ids,
    validate_bridge_settings,
)
from src.config.chain_config import (
    CHAIN_CONFIGS,
    MIN_DEPOSIT_ERC20_WEI,
    MIN_DEPOSIT_NATIVE_WEI,
    L2Type,
    get_finality_depth,
)
from src.models.types import Settings


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


def test_sapphire_chain_name():
    assert CHAIN_NAMES[23295] == "Sapphire Testnet"


def test_sapphire_native_token_metadata():
    assert NATIVE_TOKEN_SYMBOLS[23295] == "ROSE"
    assert NATIVE_TOKEN_NAMES[23295] == "ROSE"
    assert NATIVE_TOKEN_DECIMALS[23295] == 18


def test_sapphire_not_in_alchemy_subdomains():
    assert 23295 not in ALCHEMY_CHAIN_SUBDOMAINS


def test_sapphire_chain_rpc_url_uses_settings(monkeypatch):
    monkeypatch.setenv("SAPPHIRE_RPC_URL", "https://example.invalid/sapphire")
    monkeypatch.setenv("SAPPHIRE_CHAIN_ID", "23295")
    settings = load_settings(refresh=True)
    assert settings.chain_rpc_urls[23295] == settings.sapphire_rpc_url
    assert settings.chain_rpc_urls[23295] == "https://example.invalid/sapphire"


def test_sapphire_chain_config_finality_depth():
    # Testnet finality depth; re-evaluate against the production block-finality SLA before exposing bridge funds on mainnet.
    assert CHAIN_CONFIGS[23295].finality_depth == 1
    assert CHAIN_CONFIGS[23295].l2_type == L2Type.NONE


# --- Bridge env-var validation (validate_bridge_settings) ----------------

_VALID_ADDR = "0x000000000000000000000000000000000000dEaD"


def _valid_bridge_settings(**overrides) -> Settings:
    base = dict(
        rofl_bridge_address=_VALID_ADDR,
        xrose_address=_VALID_ADDR,
        bridge_mint_limit_wei=250_000_000_000_000_000_000_000,
        bridge_burn_limit_wei=250_000_000_000_000_000_000_000,
    )
    base.update(overrides)
    return Settings(**base)


def test_bridge_env_valid_settings_succeed():
    validate_bridge_settings(_valid_bridge_settings())


def test_bridge_env_missing_rofl_bridge_address_raises():
    settings = _valid_bridge_settings(rofl_bridge_address="")
    with pytest.raises(ValueError, match="ROFL_BRIDGE_ADDRESS"):
        validate_bridge_settings(settings)


def test_bridge_env_malformed_rofl_bridge_address_raises():
    settings = _valid_bridge_settings(rofl_bridge_address="0xnotanaddress")
    with pytest.raises(ValueError, match="ROFL_BRIDGE_ADDRESS"):
        validate_bridge_settings(settings)


def test_bridge_env_missing_xrose_address_raises():
    settings = _valid_bridge_settings(xrose_address="")
    with pytest.raises(ValueError, match="XROSE_ADDRESS"):
        validate_bridge_settings(settings)


def test_destination_chain_ids_subtracts_sapphire():
    settings = _valid_bridge_settings(
        sapphire_chain_id=23295,
        chain_rpc_urls={84532: "http://base", 23295: "http://sapphire"},
    )
    assert destination_chain_ids(settings) == {84532}


def test_destination_chain_ids_empty_when_only_sapphire_configured():
    settings = _valid_bridge_settings(
        sapphire_chain_id=23295,
        chain_rpc_urls={23295: "http://sapphire"},
    )
    assert destination_chain_ids(settings) == set()


def test_bridge_env_mint_limit_must_be_positive():
    settings = _valid_bridge_settings(bridge_mint_limit_wei=0)
    with pytest.raises(ValueError, match="BRIDGE_MINT_LIMIT_WEI"):
        validate_bridge_settings(settings)


def test_bridge_env_burn_limit_must_be_positive():
    settings = _valid_bridge_settings(bridge_burn_limit_wei=0)
    with pytest.raises(ValueError, match="BRIDGE_BURN_LIMIT_WEI"):
        validate_bridge_settings(settings)
