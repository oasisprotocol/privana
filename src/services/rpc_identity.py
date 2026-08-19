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
because it cannot be told apart from a mismatching one without trusting it; the
re-verification loop readmits it once it answers.

Startup aborts only when nothing verifies at all, since that deployment would
otherwise accept deposits it can never verify. A partially verified deployment
keeps serving the chains that passed. Nothing is served *before* the check runs:
the verified set starts empty, so a process that skipped it (a one-off script)
resolves no chain unless it opts out with `allow_unverified_urls`.
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

# Endpoints drift after startup: a provider re-points a URL, a load balancer fails
# over, an operator edits config. One probe at startup would trust a drifted
# endpoint forever and leave a briefly-unreachable one unserved until restart.
REVERIFICATION_INTERVAL_SECONDS = 300


class NoVerifiedChainsError(RuntimeError):
    """No configured RPC endpoint proved its chain ID; the service can serve nothing."""


# Shared per URL so consumers get the exact client whose identity was probed, and
# so web3's per-provider connection pool is reused.
_clients: Dict[str, AsyncWeb3] = {}

# Empty until `initialize_verified_chain_rpc_urls` runs: before the check nothing
# has proved its identity, so nothing is served. Unit tests and one-off scripts
# that never run the check opt out explicitly via `allow_unverified_urls`.
_verified_urls: Dict[int, str] = {}

# Set by `allow_unverified_urls` only; production never sets it. With no verified
# set, callers fall back to the mapping they were handed.
_allow_unverified: bool = False

# The configured mapping as seen at initialization. Re-verification probes this
# superset, not the narrowed `settings.chain_rpc_urls`, so a chain excluded by an
# earlier probe can be readmitted once its endpoint answers correctly again.
_configured_urls: Dict[int, str] = {}

_reverification_task: Optional[asyncio.Task] = None


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
            if not isinstance(reported, Exception):
                # CancelledError: the caller is shutting down, not a failed probe.
                raise reported
            logger.error(
                "RPC identity check failed for chain %s (%s: %s) — endpoint excluded, "
                "chain unserved until a later probe gets an answer",
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


def _commit_verified(settings, verified: Mapping[int, str]) -> None:
    """Arm the gate with ``verified`` and narrow ``settings.chain_rpc_urls`` to match.

    Committing ends the pre-check window, so the `allow_unverified_urls` opt-out is
    dropped with it: from here on only endpoints that answered correctly resolve.
    """
    global _verified_urls, _allow_unverified

    _verified_urls = dict(verified)
    _allow_unverified = False
    # Mutate the mapping every consumer already references rather than rebinding
    # the attribute, so copies taken later see the narrowed set.
    settings.chain_rpc_urls.clear()
    settings.chain_rpc_urls.update(verified)


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
    global _configured_urls

    configured = dict(settings.chain_rpc_urls)
    # Remembered for re-verification, which must probe the full configured set to
    # be able to readmit a chain this pass excludes.
    _configured_urls = configured
    if not configured:
        # Nothing to mis-serve: an endpoint-less deployment already refuses every
        # chain at the call site.
        logger.warning("No chain RPC URLs configured; skipping RPC identity check")
        _commit_verified(settings, {})
        return {}

    verified = await verify_chain_rpc_urls(configured, timeout=timeout)

    excluded = sorted(set(configured) - set(verified))
    if excluded:
        logger.error("Chains excluded by the RPC identity check, now unserved: %s", excluded)

    # Commit before the abort check so a caller that swallows
    # NoVerifiedChainsError serves nothing rather than the unverified mapping.
    _commit_verified(settings, verified)

    if not verified:
        raise NoVerifiedChainsError(
            f"None of the {len(configured)} configured RPC endpoints reported the chain ID "
            f"they are filed under (chains {sorted(configured)}); refusing to start"
        )

    logger.info("RPC identity check complete; serving chains %s", sorted(verified))
    return dict(verified)


async def reverify_chain_rpc_urls(
    settings,
    *,
    timeout: float = CHAIN_ID_PROBE_TIMEOUT_SECONDS,
) -> Dict[int, str]:
    """Re-run the identity check over the endpoints configured at initialization.

    Probing that superset rather than the narrowed mapping is what makes the gate
    two-way: an endpoint now answering with the ID it is filed under is readmitted
    — as safe as admitting it at startup — while one that has drifted drops out and
    leaves its chain unserved.

    A drop binds every chain resolved from here on; consumers that already
    memoized the chain's client keep it until they are rebuilt.

    Never raises `NoVerifiedChainsError`: a running service must not die of an RPC
    outage, and a gate that verifies nothing already refuses every chain.
    """
    configured = _configured_urls or dict(settings.chain_rpc_urls)
    if not configured:
        return {}

    verified = await verify_chain_rpc_urls(configured, timeout=timeout)
    previously_served = set(_verified_urls)
    _commit_verified(settings, verified)

    readmitted = sorted(set(verified) - previously_served)
    if readmitted:
        logger.info("RPC identity re-verified; chains readmitted: %s", readmitted)
    dropped = sorted(previously_served - set(verified))
    if dropped:
        logger.error("RPC identity re-check failed; chains now unserved: %s", dropped)
    if not verified:
        logger.critical("No configured RPC endpoint passed re-verification; serving no chain")
    return dict(verified)


async def _reverification_loop(settings, interval_seconds: float, timeout: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reverify_chain_rpc_urls(settings, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the loop alive: the current verified set stays as it is, and the
            # next pass gets another chance to drop or readmit.
            logger.exception("RPC identity re-verification pass failed; verified set unchanged")


def start_reverification_loop(
    settings,
    *,
    interval_seconds: float = REVERIFICATION_INTERVAL_SECONDS,
    timeout: float = CHAIN_ID_PROBE_TIMEOUT_SECONDS,
) -> asyncio.Task:
    """Start the periodic re-verification task, or return the one already running."""
    global _reverification_task

    if _reverification_task is not None and not _reverification_task.done():
        return _reverification_task
    _reverification_task = asyncio.create_task(
        _reverification_loop(settings, interval_seconds, timeout)
    )
    logger.info("RPC identity re-verification loop started (interval=%ss)", interval_seconds)
    return _reverification_task


async def stop_reverification_loop() -> None:
    """Cancel the periodic re-verification task and wait for it to unwind."""
    global _reverification_task

    task, _reverification_task = _reverification_task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("RPC identity re-verification loop stopped")


def verified_web3(chain_id: int, chain_rpc_urls: Mapping[int, str]) -> Optional[AsyncWeb3]:
    """Return the shared verified client for ``chain_id``, or None if none is served.

    Only chains in the verified set resolve, even when the caller still holds an
    un-narrowed mapping; callers turn the None into their own "chain not available"
    error. Before the check has run nothing is served at all, so a process that
    skipped it fails closed — unless it called `allow_unverified_urls`, and then
    only while there is no verified set to gate on.
    """
    if _verified_urls:
        url = _verified_urls.get(chain_id)
    elif _allow_unverified:
        url = chain_rpc_urls.get(chain_id)
    else:
        return None
    if not url:
        return None
    return _client_for_url(url)


def allow_unverified_urls() -> None:
    """Serve un-probed URLs while no verified set exists. Tests and scripts only.

    Production never calls this: the lifespan runs the identity check first, and
    committing its result clears the flag.
    """
    global _allow_unverified

    _allow_unverified = True


def reset_verified_chain_rpc_urls() -> None:
    """Drop the verified set, the opt-out, the loop and the clients. For tests."""
    global _verified_urls, _allow_unverified, _configured_urls, _reverification_task

    task, _reverification_task = _reverification_task, None
    if task is not None and not task.done():
        try:
            task.cancel()
        except RuntimeError:
            # Sync fixture teardown can outlive the loop the task was created on.
            pass
    _verified_urls = {}
    _allow_unverified = False
    _configured_urls = {}
    _clients.clear()
