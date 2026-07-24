"""Tests for the per-chain gas price startup bootstrap."""

from unittest.mock import AsyncMock

import pytest

import src.services.gas_price_bootstrap as gas_price_bootstrap
from src.services.gas_price_bootstrap import _MAX_ATTEMPTS, bootstrap_gas_prices

CHAIN_A = 84532
CHAIN_B = 11155111


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real backoff sleeps between retry attempts."""
    monkeypatch.setattr(gas_price_bootstrap.asyncio, "sleep", AsyncMock())


def _make_service(on_chain: dict[int, int]) -> AsyncMock:
    service = AsyncMock()
    service.get_gas_price = AsyncMock(side_effect=lambda chain_id: on_chain[chain_id])
    service.set_gas_price = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_noop_when_no_env_configured() -> None:
    """ACCOUNTING_GAS_PRICE unset → nothing to sync, no reads or writes."""
    service = _make_service({})

    await bootstrap_gas_prices(service, {})

    service.get_gas_price.assert_not_awaited()
    service.set_gas_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_publishes_when_unset_on_chain() -> None:
    """Fresh deploy has gasPrices(chainId) == 0 → bootstrap should submit the setter tx."""
    service = _make_service({CHAIN_A: 0})

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000})

    service.set_gas_price.assert_awaited_once_with(CHAIN_A, 1_000_000_000)


@pytest.mark.asyncio
async def test_noop_when_already_synced() -> None:
    """On restart, on-chain value matches desired → bootstrap should not call setter."""
    service = _make_service({CHAIN_A: 1_000_000_000})

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000})

    service.set_gas_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_updates_when_onchain_differs() -> None:
    """If the desired gas price changed, bootstrap publishes the new value."""
    service = _make_service({CHAIN_A: 500_000_000})

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000})

    service.set_gas_price.assert_awaited_once_with(CHAIN_A, 1_000_000_000)


@pytest.mark.asyncio
async def test_syncs_multiple_chains_independently() -> None:
    """Each configured chain is compared and updated independently."""
    service = _make_service({CHAIN_A: 500_000_000, CHAIN_B: 20_000_000_000})

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000, CHAIN_B: 20_000_000_000})

    service.set_gas_price.assert_awaited_once_with(CHAIN_A, 1_000_000_000)


@pytest.mark.asyncio
async def test_one_chain_failure_does_not_block_others() -> None:
    """A persistent failure on one chain is retried, logged, and does not stop the others."""
    service = AsyncMock()

    async def _get_gas_price(chain_id: int) -> int:
        if chain_id == CHAIN_A:
            raise RuntimeError("rpc error")
        return 0

    service.get_gas_price = AsyncMock(side_effect=_get_gas_price)
    service.set_gas_price = AsyncMock()

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000, CHAIN_B: 20_000_000_000})

    service.set_gas_price.assert_awaited_once_with(CHAIN_B, 20_000_000_000)


@pytest.mark.asyncio
async def test_submit_failure_is_retried_until_success() -> None:
    """A transient tx submission failure is retried and eventually succeeds."""
    service = _make_service({CHAIN_A: 0})
    service.set_gas_price = AsyncMock(side_effect=[RuntimeError("rofl-appd unavailable"), None])

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000})

    assert service.set_gas_price.await_count == 2
    service.set_gas_price.assert_awaited_with(CHAIN_A, 1_000_000_000)


@pytest.mark.asyncio
async def test_submit_failure_gives_up_after_max_attempts() -> None:
    """A persistent tx submission failure stops after the retry budget is exhausted."""
    service = _make_service({CHAIN_A: 0})
    service.set_gas_price = AsyncMock(side_effect=RuntimeError("rofl-appd unavailable"))

    await bootstrap_gas_prices(service, {CHAIN_A: 1_000_000_000})

    assert service.set_gas_price.await_count == _MAX_ATTEMPTS
