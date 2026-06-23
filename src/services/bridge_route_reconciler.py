"""TEE-driven reconciliation of ``Accounting.roflBridgeAddress[chain_id]``.

``settings.rofl_bridge_address`` is the single source of truth across every
registered destination chain. On each tick the loop iterates the destination
set (``destination_chain_ids(settings)``) and per-chain compares the on-chain
value with the configured one — either no-ops, bootstraps (on-chain is zero),
or runs the in-flight drain guard before writing.

The guard reuses ``sweep_engine.reconstruct_xrose_deposit_state`` for the
per-reservation Base-side evidence — contradictions there propagate as
``ReconstructionEvidenceError`` and fail closed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from eth_typing import ChecksumAddress
from web3 import Web3
from web3.constants import ADDRESS_ZERO

from src.config.bridge_validation import destination_chain_ids
from src.models.types import Settings
from src.services.sweep_engine import ReconstructionEvidenceError, ReconstructionKind

if TYPE_CHECKING:
    from src.services.accounting_contract import AccountingContractService
    from src.services.custody_tx_executor import CustodyTxExecutor
    from src.services.sweep_engine import SweepEngine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardReport:
    clean: bool
    unresolved: list[str]


def _checksum(addr: str) -> ChecksumAddress:
    return Web3.to_checksum_address(addr)


def _is_zero(addr: str) -> bool:
    return addr.lower() == ADDRESS_ZERO.lower()


def _norm_deposit_id(value: str) -> str:
    return value.lower().removeprefix("0x")


async def can_rotate_route(
    *,
    accounting: "AccountingContractService",
    sweep_engine: "SweepEngine",
    custody_executor: "CustodyTxExecutor",
    chain_id: int,
) -> GuardReport:
    """Returns ``clean=True`` only when every
    inbound xROSE deposit has reached ``CREDITED``."""
    unresolved: set[str] = set()

    has_sweep, sweep_ids = sweep_engine.has_any_active_xrose_bridge_in_flow()
    if has_sweep:
        unresolved.update(_norm_deposit_id(d) for d in sweep_ids)

    has_burn, burn_ids = custody_executor.has_any_pending_xrose_burn()
    if has_burn:
        unresolved.update(_norm_deposit_id(d) for d in burn_ids)

    reservations = await accounting.list_bridge_burn_reservations(chain_id=chain_id)
    for reservation in reservations:
        deposit_id_hex = _norm_deposit_id(reservation.deposit_id.hex())
        try:
            evidence = await sweep_engine.reconstruct_xrose_deposit_state(reservation.deposit_id)
        except ReconstructionEvidenceError as exc:
            logger.warning(
                "bridge route guard: contradictory evidence for %s: %s",
                deposit_id_hex,
                exc,
            )
            unresolved.add(deposit_id_hex)
            continue

        if evidence.kind is not ReconstructionKind.CREDITED:
            unresolved.add(deposit_id_hex)

    return GuardReport(clean=not unresolved, unresolved=sorted(unresolved))


async def reconcile_once(
    *,
    accounting: "AccountingContractService",
    sweep_engine: "SweepEngine",
    custody_executor: "CustodyTxExecutor",
    settings: Settings,
    chain_id: int,
) -> None:
    expected = _checksum(settings.rofl_bridge_address)
    on_chain_raw = await accounting.get_rofl_bridge_address(chain_id)

    if _is_zero(on_chain_raw):
        logger.info(
            "bridge route reconciler: bootstrapping roflBridgeAddress[%d] = %s",
            chain_id,
            expected,
        )
        await accounting.set_rofl_bridge(chain_id, expected)
        return

    on_chain = _checksum(on_chain_raw)
    if on_chain == expected:
        return

    report = await can_rotate_route(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody_executor,
        chain_id=chain_id,
    )
    if not report.clean:
        sample = report.unresolved[:10]
        more = f" (+{len(report.unresolved) - 10} more)" if len(report.unresolved) > 10 else ""
        logger.info(
            "bridge route reconciler: rotation %s -> %s blocked; unresolved: %s%s",
            on_chain,
            expected,
            ", ".join(sample),
            more,
        )
        return

    logger.info(
        "bridge route reconciler: rotating roflBridgeAddress[%d] %s -> %s",
        chain_id,
        on_chain,
        expected,
    )
    await accounting.set_rofl_bridge(chain_id, expected)


async def run_loop(
    stop_event: asyncio.Event,
    *,
    accounting: "AccountingContractService",
    sweep_engine: "SweepEngine",
    custody_executor: "CustodyTxExecutor",
    settings: Settings,
    interval_seconds: Optional[int] = None,
) -> None:
    """Sleep ``interval_seconds`` between ``reconcile_once`` calls.

    Swallows transient RPC faults so a single bad RPC tick doesn't crash the
    loop; the next tick retries. Exits cleanly when ``stop_event`` is set.
    """
    interval = (
        settings.bridge_route_reconcile_interval if interval_seconds is None else interval_seconds
    )
    logger.info("bridge route reconciler started (interval=%ds)", interval)
    while not stop_event.is_set():
        for chain_id in sorted(destination_chain_ids(settings)):
            try:
                await reconcile_once(
                    accounting=accounting,
                    sweep_engine=sweep_engine,
                    custody_executor=custody_executor,
                    settings=settings,
                    chain_id=chain_id,
                )
            except Exception:
                logger.exception(
                    "bridge route reconciler: tick failed for chain %d; retrying after interval",
                    chain_id,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("bridge route reconciler stopped")
