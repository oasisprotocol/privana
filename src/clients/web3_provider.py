"""Shared AsyncWeb3 construction with idempotent-request caching."""

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider
from web3.types import RPCEndpoint

# Only responses that can never change for a given endpoint. Deliberately NOT
# web3's default cacheable set: block/transaction requests would drag in its
# cache-validation layer, which fetches the finalized block per request and
# costs more than it saves.
_CACHEABLE_REQUESTS: set[RPCEndpoint] = {
    RPCEndpoint("eth_chainId"),
    RPCEndpoint("net_version"),
    RPCEndpoint("web3_clientVersion"),
}


def make_async_web3(rpc_url: str, headers: dict[str, str] | None = None) -> AsyncWeb3:
    """Build an AsyncWeb3 that caches immutable RPC responses per provider.

    Without this, every wrapped Sapphire call re-fetches eth_chainId several
    times, which dominates our RPC quota (the gateway rate-limits us).

    ``headers`` are sent with every RPC request; callers must only pass
    credentials to the endpoint they belong to.
    """
    request_kwargs = (
        {"headers": {**AsyncHTTPProvider.get_request_headers(), **headers}} if headers else None
    )
    return AsyncWeb3(
        AsyncHTTPProvider(
            rpc_url,
            request_kwargs=request_kwargs,
            cache_allowed_requests=True,
            cacheable_requests=_CACHEABLE_REQUESTS,
        )
    )
