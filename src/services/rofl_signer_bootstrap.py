"""Startup task: publish the ROFL-derived signer address on-chain.

The on-chain `onlyROFLQuery` modifier authenticates signed view queries by
comparing `msg.sender` to `roflSignerAddress`. The sapphirepy-wrapped reader
(see `accounting_contract._get_confidential_reader_contract`) signs view calls
with the ROFL-managed keypair returned by `RoflAppdClient.get_keypair()`.

This module reconciles the two: at startup, derive the ROFL signer address,
read the on-chain value, and submit `setRoflSignerAddress(...)` if they differ.
Idempotent — subsequent starts no-op.
"""

from __future__ import annotations

import logging

from web3.constants import ADDRESS_ZERO

from src.clients.rofl import ROFL_QUERY_SIGNER_KEY, RoflAppdClient
from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)


async def bootstrap_rofl_signer_address(
    service: AccountingContractService,
    rofl_client: RoflAppdClient | None = None,
) -> None:
    """Ensure roflSignerAddress on-chain matches the ROFL-derived signer key.

    Idempotent: on-chain value already in sync is a no-op. Any failure
    (RPC error, ROFL daemon unavailable, setter revert) propagates — startup
    must not proceed with a broken signer.
    """
    client = rofl_client or RoflAppdClient()
    _, signer_address = await client.get_keypair(ROFL_QUERY_SIGNER_KEY)

    current = await service.get_rofl_signer_address()
    if current.lower() == signer_address.lower():
        logger.info("ROFL signer address already in sync: %s", signer_address)
        return

    if current.lower() != ADDRESS_ZERO:
        logger.warning(
            "On-chain roflSignerAddress (%s) differs from derived (%s); updating.",
            current,
            signer_address,
        )
    else:
        logger.info("Publishing ROFL signer address: %s", signer_address)

    await service.set_rofl_signer_address(signer_address)
    logger.info("ROFL signer address published on-chain: %s", signer_address)
