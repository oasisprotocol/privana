"""Startup gate: every RPC endpoint must report the chain ID it is filed under.

Nothing downstream re-checks this. `deposit_verifier`, `deposit_discovery`,
`sweep_engine` and `withdrawal_processor` all build their clients from
`settings.chain_rpc_urls` and trust the dict key, so an endpoint filed under the
wrong chain — a mainnet URL under a testnet ID, two chains' URLs swapped — would
have deposits verified on one chain while transactions are signed for another,
with real funds on both sides.

`verify_chain_rpc_urls` closes that by calling `eth_chainId` on every configured
endpoint once at startup and keeping only the ones that answer with the ID they
are filed under. Both failure directions are closed:

* A mismatching endpoint is dropped, so its chain becomes *unserved* rather than
  mis-served. An unserved chain rejects deposits and withdrawals; a mis-served
  one moves funds on the wrong chain. Never mis-serve.
* An unreachable endpoint is dropped the same way. It cannot be told apart from
  a mismatching one without trusting it, and a chain the service could not reach
  at startup is not a chain it should sign for. A restart brings it back once the
  endpoint answers.

Startup aborts only when *nothing* verifies — the deployment can then serve no
chain at all, and failing loudly at boot beats accepting deposits it can never
verify. A partially verified deployment keeps serving the chains that passed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Mapping, Optional

from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

logger = logging.getLogger(__name__)

# A single eth_chainId call against an endpoint that is expected to be live. One
# hung endpoint must not hold startup open — a timeout is a failed probe, and a
# failed probe excludes the chain.
CHAIN_ID_PROBE_TIMEOUT_SECONDS = 10


class NoVerifiedChainsError(RuntimeError):
    """No configured RPC endpoint proved its chain ID; the service can serve nothing."""


# Clients are shared per URL: web3 keeps a connection pool per provider, and
# every service asking for a chain gets back the very client whose identity was
# probed rather than a fresh unproven one.
_clients: Dict[str, AsyncWeb3] = {}

# None until `initialize_verified_chain_rpc_urls` runs. That distinguishes "no
# chain verified" (an empty dict — serve nothing) from "the check has not run at
# all" (unit tests, one-off scripts), which must not silently serve nothing.
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

    A mismatch and an unreachable endpoint are both logged as errors and dropped.
    URLs are never logged — they carry provider API keys.

    Pure with respect to module state: this reports, `initialize_verified_chain_rpc_urls`
    commits.
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

    Narrowing that mapping in place is what makes the check reach consumers that
    never see the verified set directly: `AccountingContractService` copies it on
    construction (`accounting_contract.py:130`) and admits withdrawals by
    membership in it (`:833`, `:898`), `WithdrawalProcessor` reads it per call,
    and `routes.py` advertises finality per key. All of them are built lazily,
    after this runs, so they inherit the narrowed mapping instead of needing a
    second wiring path.

    Returns the verified mapping. Raises `NoVerifiedChainsError` when endpoints
    were configured and none of them verified.
    """
    global _verified_urls

    configured = dict(settings.chain_rpc_urls)
    if not configured:
        # Nothing to mis-serve, so nothing to fail closed on: an endpoint-less
        # deployment already refuses every chain at the call site.
        logger.warning("No chain RPC URLs configured; skipping RPC identity check")
        _verified_urls = {}
        return {}

    verified = await verify_chain_rpc_urls(configured, timeout=timeout)

    excluded = sorted(set(configured) - set(verified))
    if excluded:
        logger.error("Chains excluded by the RPC identity check, now unserved: %s", excluded)

    # Commit before the abort check, so that even a caller who swallows
    # NoVerifiedChainsError is left serving nothing rather than trusting the
    # unverified mapping.
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

    Once the startup check has run, only verified chains resolve: an excluded
    chain returns None even if the caller still holds an un-narrowed mapping.
    That is the fail-closed half of this module, and callers turn the None into
    their own "chain not available" error.

    ``chain_rpc_urls`` is the caller's configured mapping and is consulted only
    when the check has not run at all — unit tests and one-off scripts, where
    there is no verified set to gate on.
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
