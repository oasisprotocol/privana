"""Tests for the fail-closed startup RPC identity check (M1.2).

The check is the only thing standing between a mis-filed endpoint and a service
that verifies deposits on one chain while signing transactions for another, so
every test here asserts the *exclusion*, not just the log line. The verified set
is process-wide state; `test/conftest.py` resets it between tests.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.rpc_identity as rpc_identity
from src.services.deposit_discovery import DepositDiscoveryService, DiscoveryNotConfiguredError
from src.services.deposit_verifier import DepositVerifier
from src.services.rpc_identity import (
    NoVerifiedChainsError,
    initialize_verified_chain_rpc_urls,
    verified_web3,
    verify_chain_rpc_urls,
)

GOOD_CHAIN = 84532
GOOD_URL = "https://base-sepolia.example.invalid/key"
SAPPHIRE_CHAIN = 23295
SAPPHIRE_URL = "https://testnet.sapphire.example.invalid"


def _probe(reported: dict[str, object]):
    """Stub the network seam: map URL -> reported chain ID, or an exception to raise."""

    async def probe(url: str, timeout: float) -> int:
        answer = reported[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return probe


def _settings(chain_rpc_urls: dict[int, str]) -> SimpleNamespace:
    # Never the real load_settings() singleton: initialize narrows the mapping in
    # place, and a narrowed singleton would leak into every later test.
    return SimpleNamespace(chain_rpc_urls=dict(chain_rpc_urls))


async def test_matching_endpoints_are_verified_and_share_one_client(monkeypatch):
    monkeypatch.setattr(
        rpc_identity,
        "_probe_chain_id",
        _probe({GOOD_URL: GOOD_CHAIN, SAPPHIRE_URL: SAPPHIRE_CHAIN}),
    )
    settings = _settings({GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL})

    served = await initialize_verified_chain_rpc_urls(settings)

    assert served == {GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL}
    assert settings.chain_rpc_urls == served
    # One client per endpoint, reused by every consumer: the client handed out is
    # the one whose identity was probed.
    client = verified_web3(SAPPHIRE_CHAIN, {})
    assert client is not None
    assert verified_web3(SAPPHIRE_CHAIN, {}) is client
    assert verified_web3(GOOD_CHAIN, {}) is not client


async def test_mismatched_endpoint_is_excluded_with_both_ids_logged(monkeypatch, caplog):
    # Sapphire testnet URL filed under the Sapphire chain ID but answering with
    # mainnet's: the URL mix-up this check exists for.
    monkeypatch.setattr(
        rpc_identity,
        "_probe_chain_id",
        _probe({GOOD_URL: GOOD_CHAIN, SAPPHIRE_URL: 23294}),
    )
    settings = _settings({GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL})

    with caplog.at_level("ERROR", logger="src.services.rpc_identity"):
        served = await initialize_verified_chain_rpc_urls(settings)

    assert served == {GOOD_CHAIN: GOOD_URL}
    assert SAPPHIRE_CHAIN not in settings.chain_rpc_urls
    # Fail closed even for a caller still holding the un-narrowed mapping.
    assert verified_web3(SAPPHIRE_CHAIN, {SAPPHIRE_CHAIN: SAPPHIRE_URL}) is None

    mismatch_logs = [r.getMessage() for r in caplog.records if "mismatch" in r.getMessage()]
    assert len(mismatch_logs) == 1
    assert str(SAPPHIRE_CHAIN) in mismatch_logs[0]
    assert "23294" in mismatch_logs[0]
    # URLs carry provider API keys and must never reach the log.
    assert SAPPHIRE_URL not in caplog.text


async def test_unreachable_endpoint_is_excluded_like_a_mismatch(monkeypatch, caplog):
    monkeypatch.setattr(
        rpc_identity,
        "_probe_chain_id",
        _probe({GOOD_URL: GOOD_CHAIN, SAPPHIRE_URL: TimeoutError("no answer")}),
    )
    settings = _settings({GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL})

    with caplog.at_level("ERROR", logger="src.services.rpc_identity"):
        served = await initialize_verified_chain_rpc_urls(settings)

    # An endpoint that could not be reached cannot be told apart from one filed
    # under the wrong chain, so it is dropped rather than trusted. The reachable
    # chain keeps serving — one dead endpoint does not take the deployment down.
    assert served == {GOOD_CHAIN: GOOD_URL}
    assert verified_web3(SAPPHIRE_CHAIN, settings.chain_rpc_urls) is None
    assert verified_web3(GOOD_CHAIN, settings.chain_rpc_urls) is not None
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)


async def test_startup_aborts_when_no_endpoint_verifies(monkeypatch):
    monkeypatch.setattr(
        rpc_identity,
        "_probe_chain_id",
        _probe({GOOD_URL: 1, SAPPHIRE_URL: 1}),
    )
    settings = _settings({GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL})

    with pytest.raises(NoVerifiedChainsError, match="refusing to start"):
        await initialize_verified_chain_rpc_urls(settings)

    # Committed before the raise: swallowing the error still serves nothing.
    assert settings.chain_rpc_urls == {}
    assert verified_web3(GOOD_CHAIN, {GOOD_CHAIN: GOOD_URL}) is None


async def test_no_configured_endpoints_does_not_abort_startup(monkeypatch):
    async def unreachable_probe(url: str, timeout: float) -> int:
        raise AssertionError("nothing to probe")

    monkeypatch.setattr(rpc_identity, "_probe_chain_id", unreachable_probe)

    # A deployment with no endpoints has nothing to mis-serve; it already
    # refuses every chain at the call site, so startup is not the place to fail.
    assert await initialize_verified_chain_rpc_urls(_settings({})) == {}
    assert verified_web3(GOOD_CHAIN, {GOOD_CHAIN: GOOD_URL}) is None


async def test_verify_reports_without_committing(monkeypatch):
    monkeypatch.setattr(
        rpc_identity,
        "_probe_chain_id",
        _probe({GOOD_URL: GOOD_CHAIN, SAPPHIRE_URL: 1}),
    )
    configured = {GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL}

    assert await verify_chain_rpc_urls(configured) == {GOOD_CHAIN: GOOD_URL}
    # Reporting neither narrows the caller's mapping nor arms the gate: with no
    # verified set committed, the excluded chain still resolves from the mapping.
    assert configured == {GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL}
    assert verified_web3(SAPPHIRE_CHAIN, configured) is not None


async def test_consumers_refuse_an_excluded_chain(monkeypatch):
    monkeypatch.setattr(
        rpc_identity,
        "_probe_chain_id",
        _probe({GOOD_URL: GOOD_CHAIN, SAPPHIRE_URL: 1}),
    )
    settings = _settings({GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL})
    await initialize_verified_chain_rpc_urls(settings)

    # Services built with the pre-check mapping still fail closed, because the
    # verified set — not the caller's dict key — decides what is served.
    stale = {GOOD_CHAIN: GOOD_URL, SAPPHIRE_CHAIN: SAPPHIRE_URL}
    verifier = DepositVerifier(dict(stale))
    discovery = DepositDiscoveryService(
        accounting_service=MagicMock(list_all_tokens=AsyncMock(return_value=[])),
        sweep_engine=MagicMock(),
        chain_rpc_urls=dict(stale),
    )

    with pytest.raises(ValueError, match=f"No verified RPC endpoint for chain {SAPPHIRE_CHAIN}"):
        verifier._get_web3(SAPPHIRE_CHAIN)
    with pytest.raises(DiscoveryNotConfiguredError, match="No verified RPC endpoint"):
        discovery._get_web3(SAPPHIRE_CHAIN)

    # The verified chain resolves, and every consumer shares that one client.
    assert verifier._get_web3(GOOD_CHAIN) is discovery._get_web3(GOOD_CHAIN)


async def test_consumers_fall_back_to_configured_urls_when_check_never_ran():
    # Unit tests and one-off scripts never run the startup check; there is no
    # verified set to gate on, so the injected mapping is used as-is.
    verifier = DepositVerifier({GOOD_CHAIN: GOOD_URL})

    assert verifier._get_web3(GOOD_CHAIN) is not None
    with pytest.raises(ValueError, match="No verified RPC endpoint"):
        verifier._get_web3(SAPPHIRE_CHAIN)
