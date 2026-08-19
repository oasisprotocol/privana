# test/py/test_chain_config.py
import pytest

from src.config import (
    ALCHEMY_CHAIN_SUBDOMAINS,
    CHAIN_NAMES,
    NATIVE_TOKEN_DECIMALS,
    NATIVE_TOKEN_NAMES,
    NATIVE_TOKEN_SYMBOLS,
    _build_chain_rpc_urls,
    _build_gas_prices,
    _build_token_infos,
)
from src.config.chain_config import (
    CHAIN_CONFIGS,
    MIN_DEPOSIT_ERC20_WEI,
    MIN_DEPOSIT_NATIVE_WEI,
    ChainConfig,
    _build_chain_configs,
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


def test_build_gas_prices_wei_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("ACCOUNTING_GAS_PRICE", raising=False)

    assert _build_gas_prices() == {}


def test_build_gas_prices_wei_parses_json_mapping(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_GAS_PRICE", '{"84532": 1000000000, "11155111": 20000000000}')

    assert _build_gas_prices() == {84532: 1_000_000_000, 11155111: 20_000_000_000}


def test_build_gas_prices_wei_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_GAS_PRICE", "{not valid json")

    with pytest.raises(ValueError, match="Invalid ACCOUNTING_GAS_PRICE JSON"):
        _build_gas_prices()


def test_build_gas_prices_wei_rejects_non_object(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_GAS_PRICE", "[1, 2, 3]")

    with pytest.raises(ValueError, match="must be a JSON object"):
        _build_gas_prices()


USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def test_build_token_infos_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("ACCOUNTING_TOKEN_INFO", raising=False)

    assert _build_token_infos() == []


def test_build_token_infos_parses_native_and_erc20(monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTING_TOKEN_INFO",
        f'[{{"chain_id": 84532}}, {{"chain_id": 84532, "token_address": "{USDC_BASE_SEPOLIA}"}}]',
    )

    assert _build_token_infos() == [
        {"chain_id": 84532, "token_address": None},
        {"chain_id": 84532, "token_address": USDC_BASE_SEPOLIA},
    ]


def test_build_token_infos_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_TOKEN_INFO", "{not valid json")

    with pytest.raises(ValueError, match="Invalid ACCOUNTING_TOKEN_INFO JSON"):
        _build_token_infos()


def test_build_token_infos_rejects_non_array(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_TOKEN_INFO", '{"chain_id": 84532}')

    with pytest.raises(ValueError, match="must be a JSON array"):
        _build_token_infos()


def test_build_token_infos_rejects_non_object_entry(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_TOKEN_INFO", "[84532]")

    with pytest.raises(ValueError, match="entry 0 must be a JSON object"):
        _build_token_infos()


def test_build_token_infos_rejects_missing_chain_id(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_TOKEN_INFO", '[{"token_address": "%s"}]' % USDC_BASE_SEPOLIA)

    with pytest.raises(ValueError, match="entry 0 missing chain_id"):
        _build_token_infos()


def test_build_token_infos_rejects_non_integer_chain_id(monkeypatch):
    monkeypatch.setenv("ACCOUNTING_TOKEN_INFO", '[{"chain_id": "not-a-number"}]')

    with pytest.raises(ValueError, match="entry 0 chain_id must be an integer"):
        _build_token_infos()


def test_build_token_infos_rejects_invalid_token_address(monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTING_TOKEN_INFO", '[{"chain_id": 84532, "token_address": "not-an-address"}]'
    )

    with pytest.raises(ValueError, match="entry 0 token_address is not a valid address"):
        _build_token_infos()


def test_sapphire_testnet_runtime_metadata():
    assert CHAIN_NAMES[23295] == "Sapphire Testnet"
    assert NATIVE_TOKEN_SYMBOLS[23295] == "ROSE"
    assert NATIVE_TOKEN_NAMES[23295] == "Rose"
    assert NATIVE_TOKEN_DECIMALS[23295] == 18


def test_sapphire_testnet_chain_config_m6_1():
    assert 23295 in CHAIN_CONFIGS
    cfg = CHAIN_CONFIGS[23295]
    assert cfg.discovery_scan_chunk_blocks <= 100
    assert cfg.min_deposit_erc20_wei == 10**18
    assert cfg.finality_depth == 2
    assert cfg.discovery_lookback_blocks == 640
    assert cfg.discovery_max_lookback_blocks == 1_000


def test_sapphire_localnet_absent_on_testnet(monkeypatch):
    """The localnet mirror must not be depositable on a testnet deployment."""
    monkeypatch.setenv("SAPPHIRE_CHAIN_ID", "23295")

    assert 23293 not in _build_chain_configs()


def test_sapphire_localnet_chain_config(monkeypatch):
    monkeypatch.setenv("SAPPHIRE_CHAIN_ID", "23293")
    configs = _build_chain_configs()

    assert 23293 in configs
    cfg = configs[23293]
    assert cfg.discovery_scan_chunk_blocks <= 100
    assert cfg.min_deposit_erc20_wei == 10**18
    assert cfg.finality_depth == 2


def test_gas_funding_covers_native_sweep_limit_m3_4():
    """Every chain funds gas for a native sweep (25,000 gas) at its real gas price.

    Live sweeps size funding from the contract's own sweep gas limit (sweep_engine's
    `_gas_funding_amount`); this config value is the fallback when that read fails.
    """
    expected_reasonable_gas_prices = {
        23295: 100_000_000_000,  # 100 gwei
        23293: 100_000_000_000,  # 100 gwei
        84532: 3_000_000_000,  # 3 gwei
        11155111: 30_000_000_000,  # 30 gwei
    }
    native_sweep_gas_limit = 25_000

    for chain_id, cfg in CHAIN_CONFIGS.items():
        reasonable_gas_price = expected_reasonable_gas_prices.get(chain_id)
        assert reasonable_gas_price is not None, f"Missing test gas price baseline for {chain_id}"
        min_required_gas_funding = native_sweep_gas_limit * reasonable_gas_price
        assert cfg.gas_funding_amount_wei >= min_required_gas_funding, (
            f"chain {chain_id}: gas_funding_amount_wei ({cfg.gas_funding_amount_wei}) "
            f"must be >= 25,000 * {reasonable_gas_price} ({min_required_gas_funding})"
        )


def test_build_chain_rpc_urls_without_alchemy_key():
    urls = _build_chain_rpc_urls(
        alchemy_api_key=None,
        sapphire_chain_id=23295,
        sapphire_rpc_url="https://testnet.sapphire.oasis.io",
    )
    assert urls == {23295: "https://testnet.sapphire.oasis.io"}


def test_build_chain_rpc_urls_with_placeholder_alchemy_key():
    urls = _build_chain_rpc_urls(
        alchemy_api_key="your-alchemy-api-key-here",
        sapphire_chain_id=23295,
        sapphire_rpc_url="https://testnet.sapphire.oasis.io",
    )
    assert urls == {23295: "https://testnet.sapphire.oasis.io"}


def test_build_chain_rpc_urls_with_valid_alchemy_key():
    urls = _build_chain_rpc_urls(
        alchemy_api_key="secret-alchemy-key",
        sapphire_chain_id=23295,
        sapphire_rpc_url="https://testnet.sapphire.oasis.io",
    )
    assert urls[23295] == "https://testnet.sapphire.oasis.io"
    assert urls[84532] == "https://base-sepolia.g.alchemy.com/v2/secret-alchemy-key"
    assert urls[11155111] == "https://eth-sepolia.g.alchemy.com/v2/secret-alchemy-key"
