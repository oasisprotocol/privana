"""Startup gate: every RPC endpoint must report the chain ID it is filed under.

Nothing downstream re-checks it. A chain is resolved by dict key and the endpoint
behind that key is trusted, so one filed under the wrong chain — a mainnet URL
under a testnet ID, two chains' URLs swapped — has deposits verified on one chain
while transactions are signed for another, with real funds on both sides.

`verify_chain_rpc_urls` calls `eth_chainId` once per configured endpoint at
startup and keeps only those answering with the ID they are filed under. A
mismatching endpoint is dropped, leaving its chain *unserved* rather than
mis-served: an unserved chain rejects deposits and withdrawals, a mis-served one
moves funds on the wrong chain. An unreachable endpoint is dropped identically,
because it cannot be told apart from a mismatching one without trusting it; a
restart readmits it once it answers.

Startup aborts only when nothing verifies at all, since that deployment would
otherwise accept deposits it can never verify. A partially verified deployment
keeps serving the chains that passed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Mapping, Optional

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

logger = logging.getLogger(__name__)

# One hung endpoint must not hold startup open; a timed-out probe excludes its
# chain like any other failed probe.
CHAIN_ID_PROBE_TIMEOUT_SECONDS = 10


class NoVerifiedChainsError(RuntimeError):
    """No configured RPC endpoint proved its chain ID; the service can serve nothing."""


# Shared per URL so consumers get the exact client whose identity was probed, and
# so web3's per-provider connection pool is reused.
_clients: Dict[str, AsyncWeb3] = {}

# None until `initialize_verified_chain_rpc_urls` runs, distinguishing "nothing
# verified" (empty dict — serve nothing) from "the check never ran" (unit tests,
# one-off scripts), which must not silently serve nothing.
_verified_urls: Optional[Dict[int, str]] = None


def _client_for_url(url: str) -> AsyncWeb3:
    client = _clients.get(url)
    if client is None:
        client = AsyncWeb3(AsyncHTTPProvider(url))
        _clients[url] = client
    return client


async def _probe_chain_id(url: str, timeout: float) -> int:
    """Return the chain ID the endpoint at ``url`` claims for itself."""
    client = _client_for_url(url)
    return int(await asyncio.wait_for(client.eth.chain_id, timeout))


async def verify_chain_rpc_urls(
    chain_rpc_urls: Mapping[int, str],
    *,
    timeout: float = CHAIN_ID_PROBE_TIMEOUT_SECONDS,
) -> Dict[int, str]:
    """Probe every endpoint concurrently; return only those reporting their own ID.

    Mismatching and unreachable endpoints are both logged and dropped; URLs never
    are, since they carry provider API keys. Leaves module state alone — this
    reports, `initialize_verified_chain_rpc_urls` commits.
    """
    chain_ids = sorted(chain_rpc_urls)
    reported_ids = await asyncio.gather(
        *(_probe_chain_id(chain_rpc_urls[chain_id], timeout) for chain_id in chain_ids),
        return_exceptions=True,
    )

    verified: Dict[int, str] = {}
    for chain_id, reported in zip(chain_ids, reported_ids):
        if isinstance(reported, BaseException):
            logger.error(
                "RPC identity check failed for chain %s (%s: %s) — endpoint excluded, "
                "chain unserved until it answers on restart",
                chain_id,
                type(reported).__name__,
                reported,
            )
            continue
        if reported != chain_id:
            logger.error(
                "RPC identity mismatch: endpoint filed under chain %s reports chain %s — "
                "endpoint excluded, chain %s unserved",
                chain_id,
                reported,
                chain_id,
            )
            continue
        logger.info("RPC identity verified for chain %s", chain_id)
        verified[chain_id] = chain_rpc_urls[chain_id]
    return verified


async def initialize_verified_chain_rpc_urls(
    settings,
    *,
    timeout: float = CHAIN_ID_PROBE_TIMEOUT_SECONDS,
) -> Dict[int, str]:
    """Run the identity check once and narrow ``settings.chain_rpc_urls`` to what passed.

    Narrowing that mapping is what carries the check to consumers that never see
    the verified set: each is built lazily, after this runs, and admits a chain by
    membership in it.

    Returns the verified mapping. Raises `NoVerifiedChainsError` when endpoints
    were configured and none verified.
    """
    global _verified_urls

    configured = dict(settings.chain_rpc_urls)
    if not configured:
        # Nothing to mis-serve: an endpoint-less deployment already refuses every
        # chain at the call site.
        logger.warning("No chain RPC URLs configured; skipping RPC identity check")
        _verified_urls = {}
        return {}

    verified = await verify_chain_rpc_urls(configured, timeout=timeout)

    excluded = sorted(set(configured) - set(verified))
    if excluded:
        logger.error("Chains excluded by the RPC identity check, now unserved: %s", excluded)

    # Commit before the abort check so a caller that swallows
    # NoVerifiedChainsError serves nothing rather than the unverified mapping.
    _verified_urls = verified
    # Mutate the mapping every consumer already references rather than rebinding
    # the attribute, so copies taken later see the narrowed set.
    settings.chain_rpc_urls.clear()
    settings.chain_rpc_urls.update(verified)

    if not verified:
        raise NoVerifiedChainsError(
            f"None of the {len(configured)} configured RPC endpoints reported the chain ID "
            f"they are filed under (chains {sorted(configured)}); refusing to start"
        )

    logger.info("RPC identity check complete; serving chains %s", sorted(verified))
    return dict(verified)


def verified_web3(chain_id: int, chain_rpc_urls: Mapping[int, str]) -> Optional[AsyncWeb3]:
    """Return the shared verified client for ``chain_id``, or None if none is served.

    Once the startup check has run only verified chains resolve, even when the
    caller still holds an un-narrowed mapping; callers turn the None into their
    own "chain not available" error.

    ``chain_rpc_urls`` is consulted only when the check never ran — unit tests and
    one-off scripts, where there is no verified set to gate on.
    """
    if _verified_urls is not None:
        url = _verified_urls.get(chain_id)
    else:
        url = chain_rpc_urls.get(chain_id)
    if not url:
        return None
    return _client_for_url(url)


def reset_verified_chain_rpc_urls() -> None:
    """Drop the cached verified set and clients. For tests; unused in production."""
    global _verified_urls
    _verified_urls = None
    _clients.clear()
