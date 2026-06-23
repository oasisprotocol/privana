"""Bridge-withdrawal quote builder.

Produces the EIP-712 envelope the user signs and the gross/max-gas/net
breakdown the frontend displays. Branches on Sapphire native release vs.
registered route. Reads ``withdrawalNonces`` and ``roflBridgeAddress`` from
chain — never trusts the request to supply those values.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.config.bridge import (
    BRIDGE_QUOTE_TTL_SECONDS,
    MAX_SAPPHIRE_RELEASE_RESERVE_WEI,
    SAPPHIRE_RELEASE_GAS_LIMIT,
    SAPPHIRE_RELEASE_GAS_SAFETY_MARGIN_BPS,
    BridgeQuoteConfig,
    get_bridge_quote_config,
    quote_config_version,
)
from src.config.bridge_validation import BASIS_POINTS_DENOMINATOR
from src.models.accounting import (
    BridgeWithdrawAdvisory,
    BridgeWithdrawEip712Envelope,
    BridgeWithdrawEip712Message,
    BridgeWithdrawQuoteRequest,
    BridgeWithdrawQuoteResponse,
)
from src.models.types import Settings
from src.services.accounting_contract import (
    AccountingContractService,
    get_accounting_contract_service,
)

SettingsProvider = Callable[[], Settings]

logger = logging.getLogger(__name__)

_ZERO_ADDRESS = "0x" + "00" * 20


class BridgeQuoteError(ValueError):
    """Raised when a quote cannot be built (validation, missing route, etc.)."""


class BridgeQuoteService:
    """Build a signed-payload-ready quote envelope from on-chain reads."""

    def __init__(
        self,
        settings_provider: SettingsProvider,
        accounting: AccountingContractService,
    ) -> None:
        # ``settings_provider`` is called per request so ``quote_config_version``
        # reflects live config, not a snapshot frozen at singleton init.
        self._settings_provider = settings_provider
        self._accounting = accounting

    async def build_quote(self, req: BridgeWithdrawQuoteRequest) -> BridgeWithdrawQuoteResponse:
        cfg = get_bridge_quote_config(self._settings_provider())
        is_sapphire = req.dest_chain_id == cfg.sapphire_chain_id

        if not is_sapphire and req.dest_chain_id not in cfg.destination_chain_ids:
            raise BridgeQuoteError(
                f"dest_chain_id={req.dest_chain_id} is not a registered destination"
            )

        nonce_info = await self._accounting.get_withdrawal_nonce(req.user_address)
        on_chain_nonce = int(nonce_info["nonce"])

        if is_sapphire:
            route_address = _ZERO_ADDRESS
        else:
            route_address = await self._accounting.get_rofl_bridge_address(req.dest_chain_id)
            try:
                route_int = int(route_address, 16)
            except (TypeError, ValueError) as exc:
                raise BridgeQuoteError(
                    f"invalid route address returned for destChainId={req.dest_chain_id}: "
                    f"{route_address!r}"
                ) from exc
            if route_int == 0:
                raise BridgeQuoteError(f"no route registered for destChainId={req.dest_chain_id}")

        if is_sapphire:
            advisory, max_gas_cost = await self._compute_sapphire_reserve(cfg)
            fee_model = cfg.fee_model_sapphire
        else:
            advisory = None
            max_gas_cost = 0
            fee_model = cfg.fee_model_registered

        net_amount = req.gross_amount - max_gas_cost
        if net_amount <= 0:
            raise BridgeQuoteError(
                "gross_amount must exceed max_gas_cost; "
                f"got gross={req.gross_amount}, max_gas_cost={max_gas_cost}"
            )
        if is_sapphire and not (0 < max_gas_cost <= MAX_SAPPHIRE_RELEASE_RESERVE_WEI):
            raise BridgeQuoteError(
                f"max_gas_cost={max_gas_cost} out of allowed range "
                f"(0, {MAX_SAPPHIRE_RELEASE_RESERVE_WEI}]"
            )

        route_address_lc = route_address.lower()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=BRIDGE_QUOTE_TTL_SECONDS)

        return BridgeWithdrawQuoteResponse(
            dest_chain_id=req.dest_chain_id,
            route_address=route_address_lc,
            fee_model=fee_model,
            gross_amount=str(req.gross_amount),
            max_gas_cost=str(max_gas_cost),
            net_amount=str(net_amount),
            user_nonce=str(on_chain_nonce),
            advisory=advisory,
            quote_config_version=quote_config_version(cfg),
            expires_at=expires_at,
            token_symbol=cfg.token_symbol,
            token_decimals=cfg.token_decimals,
            eip712=BridgeWithdrawEip712Envelope(
                type="BridgeWithdraw",
                message=BridgeWithdrawEip712Message(
                    userAddress=req.user_address,
                    toAddress=req.to_address,
                    destChainId=str(req.dest_chain_id),
                    routeAddress=route_address_lc,
                    amount=str(req.gross_amount),
                    maxGasCost=str(max_gas_cost),
                    nonce=str(on_chain_nonce),
                ),
            ),
        )

    async def _compute_sapphire_reserve(
        self, cfg: BridgeQuoteConfig
    ) -> tuple[BridgeWithdrawAdvisory, int]:
        observed = await self._accounting.get_gas_price(cfg.sapphire_chain_id)
        bare_release_cost = SAPPHIRE_RELEASE_GAS_LIMIT * observed
        if bare_release_cost > MAX_SAPPHIRE_RELEASE_RESERVE_WEI:
            raise BridgeQuoteError(
                f"Sapphire gas price {observed} wei is too high: native-release cost "
                f"{bare_release_cost} exceeds the reserve cap {MAX_SAPPHIRE_RELEASE_RESERVE_WEI}. "
                "The withdrawal would be debited but never resolvable; retry when gas falls."
            )
        recommended = (
            SAPPHIRE_RELEASE_GAS_LIMIT
            * observed
            * SAPPHIRE_RELEASE_GAS_SAFETY_MARGIN_BPS
            // BASIS_POINTS_DENOMINATOR
        )
        max_gas_cost = min(recommended, MAX_SAPPHIRE_RELEASE_RESERVE_WEI)
        safety_margin = f"{SAPPHIRE_RELEASE_GAS_SAFETY_MARGIN_BPS / BASIS_POINTS_DENOMINATOR:.4g}"
        advisory = BridgeWithdrawAdvisory(
            gas_price_seen_wei=str(observed),
            recommended_gas_limit=str(SAPPHIRE_RELEASE_GAS_LIMIT),
            safety_margin=safety_margin,
        )
        return advisory, max_gas_cost


_bridge_quote_service: Optional[BridgeQuoteService] = None


def get_bridge_quote_service() -> BridgeQuoteService:
    """Return the process-singleton ``BridgeQuoteService``.

    Built lazily on first call so module import does not require live settings.
    """
    global _bridge_quote_service
    if _bridge_quote_service is None:
        from src.config import load_settings

        accounting = get_accounting_contract_service()
        _bridge_quote_service = BridgeQuoteService(load_settings, accounting)
    return _bridge_quote_service
