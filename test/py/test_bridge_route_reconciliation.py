"""Tests for the TEE-driven bridge route reconciler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from web3 import Web3
from web3.constants import ADDRESS_ZERO

from src.models.types import Settings
from src.services import bridge_route_reconciler
from src.services.accounting_contract import BridgeBurnReservation
from src.services.sweep_engine import (
    Reconstruction,
    ReconstructionEvidenceError,
    ReconstructionKind,
)

BASE_CHAIN_ID = 84532
ENV_BRIDGE = Web3.to_checksum_address("0x" + "11" * 20)
OLD_BRIDGE = Web3.to_checksum_address("0x" + "22" * 20)
DEPOSIT_ID = bytes.fromhex("aa" * 32)


def _settings(env_bridge: str = ENV_BRIDGE) -> Settings:
    return Settings(
        rofl_bridge_address=env_bridge,
        bridge_route_reconcile_interval=1,
        sapphire_chain_id=23295,
        chain_rpc_urls={23295: "http://sapphire", BASE_CHAIN_ID: "http://base"},
    )


def _credited_reconstruction() -> Reconstruction:
    return Reconstruction(
        deposit_id=DEPOSIT_ID,
        kind=ReconstructionKind.CREDITED,
        reservation=BridgeBurnReservation(
            deposit_id=DEPOSIT_ID,
            chain_id=BASE_CHAIN_ID,
            bridge=OLD_BRIDGE,
            amount=1,
            nonce=0,
        ),
        burn_amount=1,
        credited=True,
        burn_view=True,
    )


def _burn_reserved_only_reconstruction() -> Reconstruction:
    return Reconstruction(
        deposit_id=DEPOSIT_ID,
        kind=ReconstructionKind.BURN_RESERVED_NOT_MINED,
        reservation=BridgeBurnReservation(
            deposit_id=DEPOSIT_ID,
            chain_id=BASE_CHAIN_ID,
            bridge=OLD_BRIDGE,
            amount=1,
            nonce=0,
        ),
        burn_amount=None,
        credited=False,
        burn_view=False,
    )


def _make_mocks(
    *,
    on_chain: str,
    sweep_active: tuple[bool, list[str]] = (False, []),
    burn_pending: tuple[bool, list[str]] = (False, []),
    reservations: list[BridgeBurnReservation] | None = None,
    reconstruction: Reconstruction | None = None,
    reconstruction_error: ReconstructionEvidenceError | None = None,
    set_rofl_bridge_side_effect=None,
):
    accounting = AsyncMock()
    accounting.get_rofl_bridge_address = AsyncMock(return_value=on_chain)
    accounting.list_bridge_burn_reservations = AsyncMock(return_value=reservations or [])
    accounting.set_rofl_bridge = AsyncMock(side_effect=set_rofl_bridge_side_effect)

    sweep_engine = AsyncMock()
    sweep_engine.has_any_active_xrose_bridge_in_flow = MagicMock(return_value=sweep_active)
    if reconstruction_error is not None:
        sweep_engine.reconstruct_xrose_deposit_state = AsyncMock(side_effect=reconstruction_error)
    else:
        sweep_engine.reconstruct_xrose_deposit_state = AsyncMock(
            return_value=reconstruction or _credited_reconstruction()
        )

    custody = AsyncMock()
    custody.has_any_pending_xrose_burn = MagicMock(return_value=burn_pending)

    return accounting, sweep_engine, custody


@pytest.mark.asyncio
async def test_scenario_1_on_chain_matches_env_no_op() -> None:
    accounting, sweep_engine, custody = _make_mocks(on_chain=ENV_BRIDGE)

    await bridge_route_reconciler.reconcile_once(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody,
        settings=_settings(),
        chain_id=BASE_CHAIN_ID,
    )

    accounting.set_rofl_bridge.assert_not_called()


@pytest.mark.asyncio
async def test_scenario_2_on_chain_zero_bootstraps() -> None:
    accounting, sweep_engine, custody = _make_mocks(on_chain=ADDRESS_ZERO)

    await bridge_route_reconciler.reconcile_once(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody,
        settings=_settings(),
        chain_id=BASE_CHAIN_ID,
    )

    accounting.set_rofl_bridge.assert_awaited_once_with(BASE_CHAIN_ID, ENV_BRIDGE)


@pytest.mark.asyncio
async def test_scenario_3_rotation_clean_writes() -> None:
    accounting, sweep_engine, custody = _make_mocks(on_chain=OLD_BRIDGE)

    await bridge_route_reconciler.reconcile_once(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody,
        settings=_settings(),
        chain_id=BASE_CHAIN_ID,
    )

    accounting.set_rofl_bridge.assert_awaited_once_with(BASE_CHAIN_ID, ENV_BRIDGE)


@pytest.mark.asyncio
async def test_scenario_4_rotation_blocked_by_active_sweep(caplog) -> None:
    accounting, sweep_engine, custody = _make_mocks(
        on_chain=OLD_BRIDGE,
        sweep_active=(True, [DEPOSIT_ID.hex()]),
    )

    with caplog.at_level("INFO", logger="src.services.bridge_route_reconciler"):
        await bridge_route_reconciler.reconcile_once(
            accounting=accounting,
            sweep_engine=sweep_engine,
            custody_executor=custody,
            settings=_settings(),
            chain_id=BASE_CHAIN_ID,
        )

    accounting.set_rofl_bridge.assert_not_called()
    assert any(DEPOSIT_ID.hex() in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_scenario_5_rotation_blocked_by_pending_burn() -> None:
    accounting, sweep_engine, custody = _make_mocks(
        on_chain=OLD_BRIDGE,
        burn_pending=(True, [DEPOSIT_ID.hex()]),
    )

    await bridge_route_reconciler.reconcile_once(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody,
        settings=_settings(),
        chain_id=BASE_CHAIN_ID,
    )

    accounting.set_rofl_bridge.assert_not_called()


@pytest.mark.asyncio
async def test_scenario_6_reservation_without_burn_blocks() -> None:
    reservation = BridgeBurnReservation(
        deposit_id=DEPOSIT_ID,
        chain_id=BASE_CHAIN_ID,
        bridge=OLD_BRIDGE,
        amount=1,
        nonce=0,
    )
    accounting, sweep_engine, custody = _make_mocks(
        on_chain=OLD_BRIDGE,
        reservations=[reservation],
        reconstruction=_burn_reserved_only_reconstruction(),
    )

    await bridge_route_reconciler.reconcile_once(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody,
        settings=_settings(),
        chain_id=BASE_CHAIN_ID,
    )

    accounting.set_rofl_bridge.assert_not_called()


@pytest.mark.asyncio
async def test_scenario_7_all_evidence_aligned_writes() -> None:
    reservation = BridgeBurnReservation(
        deposit_id=DEPOSIT_ID,
        chain_id=BASE_CHAIN_ID,
        bridge=OLD_BRIDGE,
        amount=1,
        nonce=0,
    )
    accounting, sweep_engine, custody = _make_mocks(
        on_chain=OLD_BRIDGE,
        reservations=[reservation],
        reconstruction=_credited_reconstruction(),
    )

    await bridge_route_reconciler.reconcile_once(
        accounting=accounting,
        sweep_engine=sweep_engine,
        custody_executor=custody,
        settings=_settings(),
        chain_id=BASE_CHAIN_ID,
    )

    accounting.set_rofl_bridge.assert_awaited_once_with(BASE_CHAIN_ID, ENV_BRIDGE)


@pytest.mark.asyncio
async def test_scenario_8_reconstruction_error_blocks(caplog) -> None:
    reservation = BridgeBurnReservation(
        deposit_id=DEPOSIT_ID,
        chain_id=BASE_CHAIN_ID,
        bridge=OLD_BRIDGE,
        amount=42,
        nonce=0,
    )
    err = ReconstructionEvidenceError(DEPOSIT_ID, "reservation amount 42 != Burned event amount 41")
    accounting, sweep_engine, custody = _make_mocks(
        on_chain=OLD_BRIDGE,
        reservations=[reservation],
        reconstruction_error=err,
    )

    with caplog.at_level("WARNING", logger="src.services.bridge_route_reconciler"):
        await bridge_route_reconciler.reconcile_once(
            accounting=accounting,
            sweep_engine=sweep_engine,
            custody_executor=custody,
            settings=_settings(),
            chain_id=BASE_CHAIN_ID,
        )

    accounting.set_rofl_bridge.assert_not_called()
    assert any("contradictory evidence" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_scenario_9_run_loop_survives_rpc_fault() -> None:
    """A tick that raises must not crash the loop; the next tick proceeds."""
    accounting, sweep_engine, custody = _make_mocks(on_chain=ADDRESS_ZERO)
    call_count = 0

    async def boom_then_ok(chain_id, bridge):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient RPC fault")
        return "ok"

    accounting.set_rofl_bridge = AsyncMock(side_effect=boom_then_ok)

    stop_event = asyncio.Event()
    settings = _settings()
    settings.bridge_route_reconcile_interval = 0  # tight loop

    async def stop_after_two_ticks() -> None:
        # Allow the loop to execute at least twice before stopping.
        while call_count < 2:
            await asyncio.sleep(0)
        stop_event.set()

    await asyncio.gather(
        bridge_route_reconciler.run_loop(
            stop_event,
            accounting=accounting,
            sweep_engine=sweep_engine,
            custody_executor=custody,
            settings=settings,
        ),
        stop_after_two_ticks(),
    )

    assert call_count >= 2


@pytest.mark.asyncio
async def test_scenario_10_run_loop_iterates_destination_set() -> None:
    """``run_loop`` calls ``reconcile_once`` once per destination chain per tick.

    Sapphire is excluded; non-Sapphire RPC-configured chains are visited in
    ascending chain-id order.
    """
    accounting, sweep_engine, custody = _make_mocks(on_chain=ADDRESS_ZERO)
    visited: list[int] = []

    async def record(chain_id, bridge):
        visited.append(chain_id)
        return "ok"

    accounting.set_rofl_bridge = AsyncMock(side_effect=record)

    stop_event = asyncio.Event()
    settings = Settings(
        rofl_bridge_address=ENV_BRIDGE,
        bridge_route_reconcile_interval=0,
        sapphire_chain_id=23295,
        chain_rpc_urls={
            23295: "http://sapphire",
            BASE_CHAIN_ID: "http://base",
            1337: "http://fictional",
        },
    )

    async def stop_after_first_tick() -> None:
        while len(visited) < 2:
            await asyncio.sleep(0)
        stop_event.set()

    await asyncio.gather(
        bridge_route_reconciler.run_loop(
            stop_event,
            accounting=accounting,
            sweep_engine=sweep_engine,
            custody_executor=custody,
            settings=settings,
        ),
        stop_after_first_tick(),
    )

    assert 23295 not in visited
    assert {1337, BASE_CHAIN_ID}.issubset(set(visited))
