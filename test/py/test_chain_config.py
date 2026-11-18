# test/py/test_chain_config.py
import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from src.config import (
    ALCHEMY_CHAIN_SUBDOMAINS,
    CHAIN_NAMES,
    NATIVE_TOKEN_DECIMALS,
    NATIVE_TOKEN_NAMES,
    NATIVE_TOKEN_SYMBOLS,
    _build_chain_rpc_urls,
    _build_gas_prices,
    _build_limit_exempt_accounts,
    _build_min_deposits,
    _build_token_infos,
    get_token_id,
)
from src.config.chain_config import (
    CHAIN_CONFIGS,
    ChainConfig,
    _build_chain_configs,
    get_finality_depth,
)


def _make_config(**overrides) -> ChainConfig:
    """Build a valid ChainConfig, overriding individual fields per test."""
    params = dict(
        chain_id=1,
        finality_depth=2,
        gas_funding_amount_wei=200_000,
        min_sweep_gas_price_wei=1,  # keep gas_limit * floor * headroom under the cap
    )
    params.update(overrides)
    return ChainConfig(**params)


def test_known_chain_finality():
    assert get_finality_depth(84532) == 250  # Base Sepolia
    assert get_finality_depth(11155111) == 2  # Eth Sepolia Testnet/Localnet


@pytest.mark.parametrize("env_file", [".env.mainnet", ".env.testnet"])
def test_deployed_chains_are_configured(env_file):
    """Every chain a deployment env registers tokens for needs deposit/sweep settings."""
    repo_root = Path(__file__).parents[2]
    raw = dotenv_values(repo_root / env_file).get("ACCOUNTING_TOKEN_INFO")
    assert raw, f"{env_file} has no ACCOUNTING_TOKEN_INFO"
    chain_ids = {entry["chain_id"] for entry in json.loads(raw)}
    missing = chain_ids - CHAIN_CONFIGS.keys()
    assert not missing, f"{env_file} chains missing from CHAIN_CONFIGS: {missing}"


def test_zero_gas_price_floor_rejected():
    with pytest.raises(ValueError, match="min_sweep_gas_price_wei"):
        _make_config(min_sweep_gas_price_wei=0)


def test_cap_below_floor_priced_sweep_rejected():
    # floor 1 gwei * 65_000 gas * 2 headroom = 1.3e14 > 1e13 cap
    with pytest.raises(ValueError, match="floor-priced ERC-20 sweep"):
        _make_config(
            gas_funding_amount_wei=10**13,
            min_sweep_gas_price_wei=10**9,
        )


def test_unknown_chain_uses_default():
    assert get_finality_depth(999999) == 32  # conservative default


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


def test_get_token_id_matches_the_contract():
    """Ids read from the mainnet Accounting contract's getTokenId."""
    base_usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert get_token_id(8453, None) == (
        "0xe5e6c795953f93ec9c872a4a6000788aacf182163c7abf8a5e982fedcd9f1620"
    )
    assert get_token_id(8453, base_usdc) == (
        "0x5e7facf6b7b5b9887e41398db2ae1990ff7e379d17fedb428b04265d748b33b2"
    )


def test_native_min_deposit_must_exceed_the_funding_cap(monkeypatch):
    """Otherwise a gas-funding transfer could be claimed as a native deposit."""
    native = [{"chain_id": 8453, "token_address": None}]
    cap = CHAIN_CONFIGS[8453].gas_funding_amount_wei
    monkeypatch.setenv("MIN_DEPOSITS", json.dumps({get_token_id(8453, None): str(cap)}))
    with pytest.raises(ValueError, match="native token of chain 8453"):
        _build_min_deposits(native)

    monkeypatch.delenv("MIN_DEPOSITS")
    with pytest.raises(ValueError, match="native token of chain 8453"):
        _build_min_deposits(native)


@pytest.mark.parametrize(
    "env_file", [".env.mainnet", ".env.testnet", ".env.rubetest", ".env.localnet"]
)
def test_deployed_min_deposits_match_registered_tokens(monkeypatch, env_file):
    """A stale or mistyped tokenId would silently leave its token without a minimum."""
    values = dotenv_values(Path(__file__).parents[2] / env_file)
    monkeypatch.setenv("ACCOUNTING_TOKEN_INFO", values["ACCOUNTING_TOKEN_INFO"])
    monkeypatch.setenv("MIN_DEPOSITS", values["MIN_DEPOSITS"])
    token_infos = _build_token_infos()

    min_deposits = _build_min_deposits(token_infos)

    registered = {get_token_id(e["chain_id"], e["token_address"]) for e in token_infos}
    assert set(min_deposits) <= registered
    assert all(amount > 0 for amount in min_deposits.values())


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
    assert cfg.finality_depth == 2


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


EXEMPT_LP = "0xd1CBc91AB86c43F975a18126Cc6ad77cf1f22B8a"
EXEMPT_HONOROLL = "0xDCFF0891F0Aea40b0ae4A7Ca3e00AD1012Fc2d16"


def test_build_limit_exempt_accounts_parses_and_checksums(monkeypatch):
    monkeypatch.setenv("LIMIT_EXEMPT_ACCOUNTS", f" {EXEMPT_LP.lower()} , {EXEMPT_HONOROLL} ,")

    assert _build_limit_exempt_accounts() == (EXEMPT_LP, EXEMPT_HONOROLL)


def test_build_limit_exempt_accounts_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("LIMIT_EXEMPT_ACCOUNTS", raising=False)

    assert _build_limit_exempt_accounts() == ()


def test_build_limit_exempt_accounts_rejects_invalid_address(monkeypatch):
    monkeypatch.setenv("LIMIT_EXEMPT_ACCOUNTS", "not-an-address")

    with pytest.raises(ValueError, match="is not a valid address"):
        _build_limit_exempt_accounts()


@pytest.mark.parametrize("env_file", [".env.mainnet", ".env.testnet"])
def test_deployed_limit_exempt_accounts_parses(monkeypatch, env_file):
    """A bad entry would stop the service at startup, so check the deployed values."""
    repo_root = Path(__file__).parents[2]
    raw = dotenv_values(repo_root / env_file).get("LIMIT_EXEMPT_ACCOUNTS")
    assert raw, f"{env_file} has no LIMIT_EXEMPT_ACCOUNTS"
    monkeypatch.setenv("LIMIT_EXEMPT_ACCOUNTS", raw)

    exempt = _build_limit_exempt_accounts()
    assert len(exempt) == len([entry for entry in raw.split(",") if entry.strip()])
    assert len(set(exempt)) == len(exempt), f"{env_file} lists an address twice"
