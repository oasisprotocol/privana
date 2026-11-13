from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from eth_abi.abi import encode
from eth_utils.crypto import keccak

from privana_bench.chain import Chain, ChainError, host_of

ACCOUNTING = "0x910CFfe4e8B27bc367F6E34D6D8e3C31DED68B6f"
USER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def chain_with(answer: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[Chain, list[Any]]:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **answer(body)})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Chain("https://rpc.example/key123", ACCOUNTING, http), seen


async def test_transfer_nonce_call() -> None:
    chain, seen = chain_with(lambda _: {"result": "0x" + encode(["uint256"], [42]).hex()})
    assert await chain.transfer_nonce(USER) == 42
    call, block = seen[0]["params"]
    assert seen[0]["method"] == "eth_call" and block == "latest"
    assert call["to"] == ACCOUNTING
    selector = keccak(text="transferNonces(address)")[:4]
    assert call["data"] == "0x" + (selector + encode(["address"], [USER])).hex()


async def test_eip712_domain() -> None:
    report = encode(
        ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"],
        [b"\x1b", "AccountingModule", "1", 0, ACCOUNTING, (23295).to_bytes(32, "big"), []],
    )
    chain, _ = chain_with(lambda _: {"result": "0x" + report.hex()})
    fields, name, version, contract, salt = await chain.eip712_domain()
    assert (fields, name, version, contract) == (0x1B, "AccountingModule", "1", ACCOUNTING)
    assert int.from_bytes(salt, "big") == 23295


async def test_rpc_error_and_empty_result() -> None:
    chain, _ = chain_with(lambda _: {"error": {"code": -32000, "message": "execution reverted"}})
    with pytest.raises(ChainError, match="execution reverted"):
        await chain.version()
    chain, _ = chain_with(lambda _: {"result": "0x"})
    with pytest.raises(ChainError, match="no contract"):
        await chain.version()


async def test_chain_id() -> None:
    chain, _ = chain_with(lambda _: {"result": "0x5aff"})
    assert await chain.chain_id() == 23295


def test_host_of_drops_path_and_query() -> None:
    assert host_of("https://rpc.example/v1/key123?token=abc") == "https://rpc.example"
