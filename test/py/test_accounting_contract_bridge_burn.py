"""Tests for the bridge-burn reservation accessors on AccountingContractService.

`get_bridge_burn_nonce` reads via the typed `getBridgeBurnRequest` getter on
BridgeModule (routed through the Accounting proxy fallback); the legacy
`from_block=0` event scan it replaced was blocked by Sapphire's 100-block
`eth_getLogs` cap. `list_bridge_burn_reservations` keeps the event scan but
paginates through `paginated_get_logs`.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from _helpers import AwaitableValue
from web3 import Web3

from src.services.accounting_contract import (
    BRIDGE_BURN_RESERVATION_LOOKBACK_BLOCKS,
    AccountingContractService,
    BridgeBurnReservation,
)

BASE_CHAIN_ID = 84532
SAPPHIRE_CHAIN_ID = 23295
ROFL_BRIDGE = Web3.to_checksum_address("0x000000000000000000000000000000000000c0fe")
OTHER_BRIDGE = Web3.to_checksum_address("0x000000000000000000000000000000000000d00d")
DEPOSIT_ID_A = bytes.fromhex("a" * 64)
DEPOSIT_ID_B = bytes.fromhex("b" * 64)


def _make_service(
    *,
    reader: MagicMock | None = None,
    reader_w3: MagicMock | None = None,
) -> AccountingContractService:
    service = AccountingContractService.__new__(AccountingContractService)
    service.contract_reader = reader
    service.reader_w3 = reader_w3
    return service


def _bridge_burn_reader(call_return) -> MagicMock:
    reader = MagicMock()
    reader.functions.getBridgeBurnRequest.return_value.call = AsyncMock(return_value=call_return)
    return reader


# ─── get_bridge_burn_nonce ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_bridge_burn_nonce_returns_nonce_when_exists() -> None:
    reader = _bridge_burn_reader((BASE_CHAIN_ID, ROFL_BRIDGE, 1_000, 7, True))
    service = _make_service(reader=reader)

    nonce = await service.get_bridge_burn_nonce(DEPOSIT_ID_A)

    assert nonce == 7
    reader.functions.getBridgeBurnRequest.assert_called_once_with(DEPOSIT_ID_A)


@pytest.mark.asyncio
async def test_get_bridge_burn_nonce_raises_when_not_exists() -> None:
    reader = _bridge_burn_reader((0, "0x" + "00" * 20, 0, 0, False))
    service = _make_service(reader=reader)

    with pytest.raises(ValueError) as exc_info:
        await service.get_bridge_burn_nonce(DEPOSIT_ID_A)

    assert DEPOSIT_ID_A.hex() in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_bridge_burn_nonce_rejects_wrong_length_deposit_id() -> None:
    service = _make_service(reader=MagicMock())

    with pytest.raises(ValueError, match="32 bytes"):
        await service.get_bridge_burn_nonce(b"\x01\x02\x03")


@pytest.mark.asyncio
async def test_get_bridge_burn_nonce_propagates_rpc_failure() -> None:
    reader = MagicMock()
    reader.functions.getBridgeBurnRequest.return_value.call = AsyncMock(
        side_effect=RuntimeError("rpc down")
    )
    service = _make_service(reader=reader)

    with pytest.raises(RuntimeError, match="rpc down"):
        await service.get_bridge_burn_nonce(DEPOSIT_ID_A)


# ─── list_bridge_burn_reservations ──────────────────────────────────────────


def _event(deposit_id: bytes, chain_id: int, bridge: str, amount: int, nonce: int) -> dict:
    return {
        "args": {
            "depositId": deposit_id,
            "chainId": chain_id,
            "bridge": bridge,
            "amount": amount,
            "nonce": nonce,
        }
    }


@pytest.mark.asyncio
async def test_list_bridge_burn_reservations_paginates_and_orders() -> None:
    chunk_calls: list[dict] = []

    async def fake_get_logs(**kwargs):
        chunk_calls.append(kwargs)
        if kwargs["from_block"] == 0:
            return [_event(DEPOSIT_ID_A, BASE_CHAIN_ID, ROFL_BRIDGE, 100, 1)]
        return [_event(DEPOSIT_ID_B, BASE_CHAIN_ID, ROFL_BRIDGE, 200, 2)]

    reader = MagicMock()
    reader.events.BridgeBurnReserved.get_logs = AsyncMock(side_effect=fake_get_logs)

    reader_w3 = MagicMock()
    reader_w3.eth.block_number = AwaitableValue(150)
    service = _make_service(reader=reader, reader_w3=reader_w3)

    out = await service.list_bridge_burn_reservations()

    assert [c["from_block"] for c in chunk_calls] == [0, 100]
    assert [c["to_block"] for c in chunk_calls] == [99, 150]
    assert [r.nonce for r in out] == [1, 2]
    assert all(isinstance(r, BridgeBurnReservation) for r in out)


@pytest.mark.asyncio
async def test_list_bridge_burn_reservations_filters_by_chain_id() -> None:
    reader = MagicMock()
    reader.events.BridgeBurnReserved.get_logs = AsyncMock(
        return_value=[
            _event(DEPOSIT_ID_A, BASE_CHAIN_ID, ROFL_BRIDGE, 100, 1),
            _event(DEPOSIT_ID_B, SAPPHIRE_CHAIN_ID, OTHER_BRIDGE, 200, 2),
        ]
    )

    reader_w3 = MagicMock()
    reader_w3.eth.block_number = AwaitableValue(50)
    service = _make_service(reader=reader, reader_w3=reader_w3)

    base_only = await service.list_bridge_burn_reservations(chain_id=BASE_CHAIN_ID)

    assert [r.deposit_id for r in base_only] == [DEPOSIT_ID_A]
    assert base_only[0].chain_id == BASE_CHAIN_ID


@pytest.mark.asyncio
async def test_list_bridge_burn_reservations_requires_reader_w3() -> None:
    service = _make_service(reader=MagicMock(), reader_w3=None)

    with pytest.raises(ValueError, match="SAPPHIRE_RPC_URL"):
        await service.list_bridge_burn_reservations()


@pytest.mark.asyncio
async def test_list_bridge_burn_reservations_applies_default_lookback_floor() -> None:
    """When head > lookback, scan starts at `head - lookback`, not 0."""
    chunk_calls: list[dict] = []

    async def fake_get_logs(**kwargs):
        chunk_calls.append(kwargs)
        return []

    reader = MagicMock()
    reader.events.BridgeBurnReserved.get_logs = AsyncMock(side_effect=fake_get_logs)

    head = BRIDGE_BURN_RESERVATION_LOOKBACK_BLOCKS + 50
    reader_w3 = MagicMock()
    reader_w3.eth.block_number = AwaitableValue(head)
    service = _make_service(reader=reader, reader_w3=reader_w3)

    await service.list_bridge_burn_reservations()

    assert chunk_calls[0]["from_block"] == head - BRIDGE_BURN_RESERVATION_LOOKBACK_BLOCKS
    assert chunk_calls[-1]["to_block"] == head


@pytest.mark.asyncio
async def test_list_bridge_burn_reservations_lookback_none_scans_full_history() -> None:
    """`lookback_blocks=None` opts into the legacy from_block=0 scan."""
    chunk_calls: list[dict] = []

    async def fake_get_logs(**kwargs):
        chunk_calls.append(kwargs)
        return []

    reader = MagicMock()
    reader.events.BridgeBurnReserved.get_logs = AsyncMock(side_effect=fake_get_logs)

    reader_w3 = MagicMock()
    reader_w3.eth.block_number = AwaitableValue(250)
    service = _make_service(reader=reader, reader_w3=reader_w3)

    await service.list_bridge_burn_reservations(lookback_blocks=None)

    assert chunk_calls[0]["from_block"] == 0
    assert chunk_calls[-1]["to_block"] == 250


@pytest.mark.asyncio
async def test_list_bridge_burn_reservations_lookback_int_overrides_default() -> None:
    """Explicit int lookback overrides the module default."""
    chunk_calls: list[dict] = []

    async def fake_get_logs(**kwargs):
        chunk_calls.append(kwargs)
        return []

    reader = MagicMock()
    reader.events.BridgeBurnReserved.get_logs = AsyncMock(side_effect=fake_get_logs)

    reader_w3 = MagicMock()
    reader_w3.eth.block_number = AwaitableValue(1_000)
    service = _make_service(reader=reader, reader_w3=reader_w3)

    await service.list_bridge_burn_reservations(lookback_blocks=200)

    assert chunk_calls[0]["from_block"] == 800
    assert chunk_calls[-1]["to_block"] == 1_000
