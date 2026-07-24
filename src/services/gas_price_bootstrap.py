"""Startup task: keep per-chain gas prices on the Accounting contract in sync with config.

`setGasPrice` is gated by `onlyROFL` on-chain, so it must be submitted via
`RoflAppdClient.submit_tx` (see `AccountingContractService.set_gas_price`) to
carry a valid ROFL-attested signature — a plain admin key will not pass the
modifier.

This module reconciles the two: at startup, read the desired per-chain gas
price from the `ACCOUNTING_GAS_PRICE` JSON env var (see `config._build_gas_prices_wei`),
compare to the on-chain value, and submit `setGasPrice(...)` for any chain
that differs. Idempotent — subsequent starts no-op when values already match.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict

from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)

# Retries per chain when reading or submitting the ROFL-authenticated tx fails
# (rofl-appd may not be reachable yet right after startup).
_MAX_ATTEMPTS = 5
_BASE_RETRY_DELAY = 1.0


async def bootstrap_gas_prices(
    service: AccountingContractService,
    gas_prices_wei: Dict[int, int],
) -> None:
    """Ensure on-chain gas prices match the configured per-chain values.

    Best-effort per chain: a failure updating one chain is retried with
    exponential backoff, then logged, and does not block the others or abort
    startup — unlike the ROFL signer address, a stale gas price degrades
    sweep/withdrawal cost accuracy for that chain only, rather than breaking
    all confidential reads.
    """
    if not gas_prices_wei:
        logger.info("ACCOUNTING_GAS_PRICE not configured; skipping gas price sync")
        return

    for chain_id, desired in gas_prices_wei.items():
        await _sync_chain_gas_price(service, chain_id, desired)


async def _sync_chain_gas_price(
    service: AccountingContractService, chain_id: int, desired: int
) -> None:
    """Sync one chain's gas price, retrying transient read/submit failures."""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            current = await service.get_gas_price(chain_id)
            if current == desired:
                logger.info("Gas price for chain %s already in sync: %s wei", chain_id, desired)
                return

            logger.info(
                "Updating gas price for chain %s: %s wei -> %s wei", chain_id, current, desired
            )
            await service.set_gas_price(chain_id, desired)
            logger.info("Gas price for chain %s published on-chain: %s wei", chain_id, desired)
            return
        except Exception as e:
            if attempt < _MAX_ATTEMPTS - 1:
                delay = _BASE_RETRY_DELAY * (2**attempt)
                logger.warning(
                    "Failed to sync gas price for chain %s, retrying in %ss (attempt %s/%s): %s",
                    chain_id,
                    delay,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    e,
                )
                await asyncio.sleep(delay)
            else:
                logger.exception(
                    "Failed to sync gas price for chain %s after %s attempts",
                    chain_id,
                    _MAX_ATTEMPTS,
                )
