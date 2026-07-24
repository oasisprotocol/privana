"""Startup task: register configured tokens on the Accounting contract.

`setTokenInfo` is gated by `onlyROFL` on-chain, so it must be submitted via
`RoflAppdClient.submit_tx` (see `AccountingContractService.set_token_info`) to
carry a valid ROFL-attested signature — a plain admin key will not pass the
modifier.

This module reconciles the two: at startup, read the desired token list from
the `ACCOUNTING_TOKEN_INFO` JSON env var (see `config._build_token_infos`),
compute each token's id, and submit `setTokenInfo(...)` for any token not yet
registered. Idempotent — a token_id is a hash of its type+data, so an already
registered token_id implies the on-chain data already matches.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from src.services.accounting_contract import AccountingContractService

logger = logging.getLogger(__name__)

# Retries per token when reading or submitting the ROFL-authenticated tx fails
# (rofl-appd may not be reachable yet right after startup).
_MAX_ATTEMPTS = 5
_BASE_RETRY_DELAY = 1.0


def _describe(chain_id: int, token_address: str | None) -> str:
    if token_address is None:
        return f"chain {chain_id} (native)"
    return f"chain {chain_id} token {token_address}"


async def bootstrap_token_info(
    service: AccountingContractService,
    token_infos: List[Dict[str, Any]],
) -> None:
    """Register any configured token that isn't already registered on-chain.

    Best-effort per token: a failure registering one token is retried with
    exponential backoff, then logged, and does not block the others or abort
    startup.
    """
    if not token_infos:
        logger.info("ACCOUNTING_TOKEN_INFO not configured; skipping token registration")
        return

    for entry in token_infos:
        chain_id = entry["chain_id"]
        token_address = entry.get("token_address")
        await _register_token(service, chain_id, token_address)


async def _register_token(
    service: AccountingContractService, chain_id: int, token_address: str | None
) -> None:
    """Register one token, retrying transient read/submit failures."""
    label = _describe(chain_id, token_address)
    for attempt in range(_MAX_ATTEMPTS):
        try:
            data, token_id = await service.get_token_data_and_id(chain_id, token_address)
            if await service.is_token_registered(token_id):
                logger.info("Token already registered: %s (token_id=%s)", label, token_id.hex())
                return

            token_type = 0 if token_address is None else 1
            logger.info("Registering token: %s (token_id=%s)", label, token_id.hex())
            await service.set_token_info(token_type, data)
            logger.info("Token registered on-chain: %s (token_id=%s)", label, token_id.hex())
            return
        except Exception as e:
            if attempt < _MAX_ATTEMPTS - 1:
                delay = _BASE_RETRY_DELAY * (2**attempt)
                logger.warning(
                    "Failed to register token %s, retrying in %ss (attempt %s/%s): %s",
                    label,
                    delay,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    e,
                )
                await asyncio.sleep(delay)
            else:
                logger.exception(
                    "Failed to register token %s after %s attempts", label, _MAX_ATTEMPTS
                )
