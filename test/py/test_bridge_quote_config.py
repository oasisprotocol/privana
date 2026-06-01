"""Tests for ``src/config/bridge.py`` — the authoritative quote config source."""

import json
import re

from src.config.bridge import (
    canonical_config_json,
    get_bridge_quote_config,
    quote_config_version,
)
from src.models.types import Settings

_VALID_ADDR = "0x000000000000000000000000000000000000dEaD"


def _settings(**overrides) -> Settings:
    base = dict(
        rofl_bridge_address=_VALID_ADDR,
        xrose_address=_VALID_ADDR,
        bridge_mint_limit_wei=10**24,
        bridge_burn_limit_wei=10**24,
        sapphire_chain_id=23295,
        chain_rpc_urls={
            23295: "https://example.invalid/sapphire",
            84532: "https://example.invalid/base",
        },
    )
    base.update(overrides)
    return Settings(**base)


def test_quote_config_version_format():
    cfg = get_bridge_quote_config(_settings())
    version = quote_config_version(cfg)
    assert re.fullmatch(r"bridge-quote-v1:0x[0-9a-f]{64}", version)


def test_quote_config_version_deterministic():
    v1 = quote_config_version(get_bridge_quote_config(_settings()))
    v2 = quote_config_version(get_bridge_quote_config(_settings()))
    assert v1 == v2


def test_quote_config_version_bumps_on_field_change():
    baseline = quote_config_version(get_bridge_quote_config(_settings()))

    bumped_dest = quote_config_version(
        get_bridge_quote_config(_settings(chain_rpc_urls={23295: "x", 84532: "x", 11155111: "x"}))
    )
    assert bumped_dest != baseline


def test_canonical_json_is_sorted_and_compact():
    cfg = get_bridge_quote_config(_settings())
    payload = canonical_config_json(cfg)

    # No whitespace between separators
    assert " " not in payload
    assert "\n" not in payload

    # Keys lexicographically sorted
    parsed = json.loads(payload)
    keys = list(parsed.keys())
    assert keys == sorted(keys)


def test_destination_chain_ids_sorted():
    settings = _settings(
        chain_rpc_urls={
            23295: "x",
            11155111: "x",
            84532: "x",
        }
    )
    cfg = get_bridge_quote_config(settings)
    # Sapphire excluded, remainder sorted ascending
    assert cfg.destination_chain_ids == (84532, 11155111)


def test_token_metadata_defaults():
    cfg = get_bridge_quote_config(_settings())
    assert cfg.token_symbol == "ROSE"
    assert cfg.token_decimals == 18
    assert cfg.fee_model_sapphire == "native_gas_user_paid"
    assert cfg.fee_model_registered == "foreign_gas_operator_paid"


def test_quote_config_is_frozen():
    cfg = get_bridge_quote_config(_settings())
    try:
        cfg.sapphire_chain_id = 999  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("BridgeQuoteConfig must be frozen")
