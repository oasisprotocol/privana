"""Tests for the shared AsyncWeb3 construction helper."""

import json

import pytest

from src.clients.web3_provider import make_async_web3
from src.config import _build_sapphire_rpc_headers


class TestMakeAsyncWeb3:
    def test_caches_only_immutable_requests(self):
        provider = make_async_web3("http://localhost:1").provider
        assert provider.cache_allowed_requests is True
        assert provider.cacheable_requests == {"eth_chainId", "net_version", "web3_clientVersion"}

    def test_no_headers_keeps_web3_defaults(self):
        provider = make_async_web3("http://localhost:1").provider
        headers = provider.get_request_kwargs()["headers"]
        assert headers["Content-Type"] == "application/json"

    def test_extra_headers_are_merged_over_defaults(self):
        provider = make_async_web3("http://localhost:1", {"x-oasis-client": "token"}).provider
        headers = provider.get_request_kwargs()["headers"]
        assert headers["x-oasis-client"] == "token"
        # Custom request_kwargs replace web3's defaults, so the helper must
        # merge Content-Type back in or JSON-RPC endpoints reject the request.
        assert headers["Content-Type"] == "application/json"


class TestBuildSapphireRpcHeaders:
    def test_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv("SAPPHIRE_RPC_HEADERS", raising=False)
        assert _build_sapphire_rpc_headers() == {}

    def test_parses_json_object(self, monkeypatch):
        monkeypatch.setenv("SAPPHIRE_RPC_HEADERS", json.dumps({"x-oasis-client": "secret"}))
        assert _build_sapphire_rpc_headers() == {"x-oasis-client": "secret"}

    def test_rejects_invalid_json(self, monkeypatch):
        monkeypatch.setenv("SAPPHIRE_RPC_HEADERS", "not-json")
        with pytest.raises(ValueError, match="Invalid SAPPHIRE_RPC_HEADERS"):
            _build_sapphire_rpc_headers()

    def test_rejects_non_string_values(self, monkeypatch):
        monkeypatch.setenv("SAPPHIRE_RPC_HEADERS", json.dumps({"x-limit": 20}))
        with pytest.raises(ValueError, match="header name to value"):
            _build_sapphire_rpc_headers()
