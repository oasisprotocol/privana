"""Bridge-withdrawal submit service.

Validates a user-signed submit payload against the authoritative quote config
(same object the quote endpoint reads) plus live on-chain reads, then calls
``Accounting.requestBridgeWithdrawal``. Every rejection branch returns before
the contract call is made.

EIP-712 signature recovery is intentionally on-chain only; the contract's
``verifyBridgeWithdrawSignature`` is the authoritative check. Re-implementing
it off-chain would risk drift with the verifier.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.config.bridge import (
    MAX_SAPPHIRE_RELEASE_RESERVE_WEI,
    get_bridge_quote_config,
    quote_config_version,
)
from src.models.accounting import BridgeWithdrawSubmitRequest
from src.services.accounting_contract import (
    AccountingContractService,
    SubmissionResult,
    get_accounting_contract_service,
)
from src.services.bridge_quote import _ZERO_ADDRESS, SettingsProvider

logger = logging.getLogger(__name__)


class BridgeSubmitError(ValueError):
    """Raised when a submit is rejected before any contract call."""


class BridgeSubmitService:
    """Validate and dispatch a user-signed bridge withdrawal."""

    def __init__(
        self,
        settings_provider: SettingsProvider,
        accounting: AccountingContractService,
    ) -> None:
        self._settings_provider = settings_provider
        self._accounting = accounting

    async def submit(self, req: BridgeWithdrawSubmitRequest) -> SubmissionResult:
        cfg = get_bridge_quote_config(self._settings_provider())
        is_sapphire = req.dest_chain_id == cfg.sapphire_chain_id

        expected_version = quote_config_version(cfg)
        if req.quote_config_version != expected_version:
            raise BridgeSubmitError(
                "quote_config_version mismatch — re-quote required "
                f"(got {req.quote_config_version}, current {expected_version})"
            )

        now = datetime.now(timezone.utc)
        if req.expires_at <= now:
            raise BridgeSubmitError(
                f"quote expired at {req.expires_at.isoformat()} (now {now.isoformat()})"
            )

        if not is_sapphire and req.dest_chain_id not in cfg.destination_chain_ids:
            raise BridgeSubmitError(
                f"dest_chain_id={req.dest_chain_id} is not a registered destination"
            )

        if is_sapphire:
            expected_route = _ZERO_ADDRESS
        else:
            raw_route = await self._accounting.get_rofl_bridge_address(req.dest_chain_id)
            try:
                route_int = int(raw_route, 16)
            except (TypeError, ValueError) as exc:
                raise BridgeSubmitError(
                    f"invalid route address returned for destChainId={req.dest_chain_id}: "
                    f"{raw_route!r}"
                ) from exc
            if route_int == 0:
                raise BridgeSubmitError(f"no route registered for destChainId={req.dest_chain_id}")
            expected_route = raw_route.lower()
        if req.route_address.lower() != expected_route:
            raise BridgeSubmitError(
                "route_address has rotated since quote — re-quote required "
                f"(got {req.route_address.lower()}, current {expected_route})"
            )

        nonce_info = await self._accounting.get_withdrawal_nonce(req.user_address)
        on_chain_nonce = int(nonce_info["nonce"])
        if req.user_nonce != on_chain_nonce:
            raise BridgeSubmitError(
                "user_nonce mismatch — re-quote required "
                f"(got {req.user_nonce}, current {on_chain_nonce})"
            )

        if is_sapphire:
            if not (0 < req.max_gas_cost <= MAX_SAPPHIRE_RELEASE_RESERVE_WEI):
                raise BridgeSubmitError(
                    f"max_gas_cost={req.max_gas_cost} out of allowed range "
                    f"(0, {MAX_SAPPHIRE_RELEASE_RESERVE_WEI}]"
                )
            if req.amount <= req.max_gas_cost:
                raise BridgeSubmitError(
                    "amount must exceed max_gas_cost on Sapphire branch "
                    f"(amount={req.amount}, max_gas_cost={req.max_gas_cost})"
                )
        elif req.max_gas_cost != 0:
            raise BridgeSubmitError(
                f"max_gas_cost must be 0 on registered-route branch, got {req.max_gas_cost}"
            )

        return await self._accounting.request_bridge_withdrawal(req.model_dump())


_bridge_submit_service: Optional[BridgeSubmitService] = None


def get_bridge_submit_service() -> BridgeSubmitService:
    """Return the process-singleton ``BridgeSubmitService``.

    Built lazily on first call so module import does not require live settings.
    """
    global _bridge_submit_service
    if _bridge_submit_service is None:
        from src.config import load_settings

        _bridge_submit_service = BridgeSubmitService(
            load_settings, get_accounting_contract_service()
        )
    return _bridge_submit_service
