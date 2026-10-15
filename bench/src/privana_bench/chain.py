"""Sapphire reads: public eth_calls over JSON-RPC, and the service's signed
queries for its locks. Nothing here writes."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from eth_abi.abi import decode, encode
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from sapphirepy import sapphire
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

_TRANSFER_NONCES = keccak(text="transferNonces(address)")[:4]
_VERSION = keccak(text="VERSION()")[:4]
_EIP712_DOMAIN = keccak(text="eip712Domain()")[:4]
_GET_SERVICE_LOCKS = keccak(text="getServiceLocks(address,bytes)")[:4]
# A FundLock: lockId, serviceId, tokenId, amount, expiry.
_FUND_LOCK = "(uint256,address,bytes32,uint256,uint256)"


class ChainError(Exception):
    pass


def host_of(url: str) -> str:
    """Scheme and host only, since an RPC URL can carry an API key in its path or query."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}"


class Chain:
    def __init__(self, rpc_url: str, contract: str, http: httpx.AsyncClient) -> None:
        self._url = rpc_url
        self._contract = contract
        self._http = http

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        reply = await self._http.post(
            self._url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        reply.raise_for_status()
        body = reply.json()
        if "error" in body:
            raise ChainError(f"{method}: {str(body['error'].get('message'))[:200]}")
        return body["result"]

    async def _call(self, data: bytes) -> bytes:
        result = await self._rpc(
            "eth_call", [{"to": self._contract, "data": "0x" + data.hex()}, "latest"]
        )
        out = bytes.fromhex(str(result).removeprefix("0x"))
        if not out:
            raise ChainError(f"empty eth_call result: no contract at {self._contract}?")
        return out

    async def chain_id(self) -> int:
        return int(await self._rpc("eth_chainId", []), 16)

    async def version(self) -> int:
        return int(decode(["uint64"], await self._call(_VERSION))[0])

    async def eip712_domain(self) -> tuple[int, str, str, str, bytes]:
        """The ERC-5267 domain: fields bitmap, name, version, verifying contract, salt."""
        fields, name, version, _, contract, salt, _ = decode(
            ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"],
            await self._call(_EIP712_DOMAIN),
        )
        return fields[0], name, version, to_checksum_address(contract), salt

    async def transfer_nonce(self, address: str) -> int:
        out = await self._call(_TRANSFER_NONCES + encode(["address"], [address]))
        return int(decode(["uint256"], out)[0])

    async def _batch(self, calls: list[bytes], block: str) -> list[bytes]:
        """Each eth_call in one JSON-RPC batch, results in the order of `calls`."""
        if not calls:
            return []
        reply = await self._http.post(
            self._url,
            json=[
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "eth_call",
                    "params": [{"to": self._contract, "data": "0x" + data.hex()}, block],
                }
                for i, data in enumerate(calls)
            ],
        )
        reply.raise_for_status()
        body = reply.json()
        if not isinstance(body, list):
            raise ChainError(f"a batch of eth_calls got no list back: {str(body)[:200]}")
        results = {r.get("id"): r for r in body}
        out = []
        for i in range(len(calls)):
            result = results.get(i, {})
            if "result" not in result:
                error = result.get("error", {}).get("message", "no result")
                raise ChainError(f"eth_call {i} in a batch: {str(error)[:200]}")
            out.append(bytes.fromhex(str(result["result"]).removeprefix("0x")))
        return out

    async def nonces(self, getter: str, addresses: list[str]) -> tuple[int, dict[str, int]]:
        """`getter(address)` for each address, all read at one block, and that block."""
        block = int(await self._rpc("eth_blockNumber", []), 16)
        selector = keccak(text=f"{getter}(address)")[:4]
        out = await self._batch(
            [selector + encode(["address"], [a]) for a in addresses], hex(block)
        )
        return block, {
            a: int(decode(["uint256"], o)[0]) for a, o in zip(addresses, out, strict=True)
        }

    async def service_locks(
        self, service: LocalAccount, users: list[str]
    ) -> dict[str, list[tuple[int, int]]]:
        """Each user's locks for `service`, as (lock id, amount).

        A query signed by the service with an empty token: Sapphire encrypts it
        and the contract takes the service from msg.sender.
        """
        w3 = sapphire.wrap(AsyncWeb3(AsyncHTTPProvider(self._url)), service)
        limit = asyncio.Semaphore(8)  # each signed query is about five RPC requests

        async def read(user: str) -> list[tuple[int, int]]:
            data = _GET_SERVICE_LOCKS + encode(["address", "bytes"], [user, b""])
            call = {
                "from": service.address,
                "to": self._contract,
                "data": HexStr("0x" + data.hex()),
            }
            async with limit:
                out = await w3.eth.call(call)
            return [(int(lock[0]), int(lock[3])) for lock in decode([f"{_FUND_LOCK}[]"], out)[0]]

        try:
            found = await asyncio.gather(*(read(u) for u in users))
        except Exception as exc:
            # An HTTP error names the URL, and an RPC URL can hold a key.
            reason = re.sub(r"https?://\S+", "<rpc-url>", str(exc))[:200]
            raise ChainError(f"getServiceLocks: {type(exc).__name__}: {reason}") from None
        finally:
            await w3.provider.disconnect()
        return dict(zip(users, found, strict=True))
