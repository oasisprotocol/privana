"""Tests for `src.utils.eth_logs.paginated_get_logs`."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.utils.eth_logs import SAPPHIRE_GETLOGS_WINDOW, paginated_get_logs


def _make_event_obj(per_chunk: list[list[str]]) -> MagicMock:
    """Build an event-like object whose `.get_logs(**kwargs)` returns the
    next list from `per_chunk` on each call. Records the kwargs for assertions."""
    event_obj = MagicMock()
    event_obj.get_logs = AsyncMock(side_effect=per_chunk)
    return event_obj


@pytest.mark.asyncio
async def test_splits_range_into_window_sized_chunks_in_order() -> None:
    event_obj = _make_event_obj([["a0", "a1"], ["b0"], ["c0", "c1", "c2"]])
    out = await paginated_get_logs(event_obj, from_block=0, to_block=250, window=100)

    calls = event_obj.get_logs.await_args_list
    assert [c.kwargs["from_block"] for c in calls] == [0, 100, 200]
    assert [c.kwargs["to_block"] for c in calls] == [99, 199, 250]
    assert out == ["a0", "a1", "b0", "c0", "c1", "c2"]


@pytest.mark.asyncio
async def test_empty_range_returns_empty_list_without_calls() -> None:
    event_obj = _make_event_obj([])
    out = await paginated_get_logs(event_obj, from_block=10, to_block=9)

    assert out == []
    event_obj.get_logs.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_chunk_when_range_fits_in_window() -> None:
    event_obj = _make_event_obj([["x"]])
    out = await paginated_get_logs(event_obj, from_block=50, to_block=120, window=100)

    calls = event_obj.get_logs.await_args_list
    assert len(calls) == 1
    assert calls[0].kwargs == {"from_block": 50, "to_block": 120}
    assert out == ["x"]


@pytest.mark.asyncio
async def test_mid_chunk_failure_propagates_without_partial_result() -> None:
    event_obj = MagicMock()
    event_obj.get_logs = AsyncMock(side_effect=[["ok-0"], RuntimeError("rpc boom")])

    with pytest.raises(RuntimeError, match="rpc boom"):
        await paginated_get_logs(event_obj, from_block=0, to_block=199, window=100)

    # Two calls attempted; no partial list visible to the caller.
    assert event_obj.get_logs.await_count == 2


@pytest.mark.asyncio
async def test_argument_filters_forwarded_to_every_chunk() -> None:
    filters = {"depositId": b"\xab" * 32}
    event_obj = _make_event_obj([[], [], []])
    await paginated_get_logs(
        event_obj,
        from_block=0,
        to_block=250,
        window=100,
        argument_filters=filters,
    )

    for call in event_obj.get_logs.await_args_list:
        assert call.kwargs["argument_filters"] == filters


@pytest.mark.asyncio
async def test_default_window_is_sapphire_cap() -> None:
    assert SAPPHIRE_GETLOGS_WINDOW == 100
    event_obj = _make_event_obj([[], []])
    await paginated_get_logs(event_obj, from_block=0, to_block=150)

    calls = event_obj.get_logs.await_args_list
    # Default window=100 → two chunks: 0–99, 100–150.
    assert [c.kwargs["to_block"] for c in calls] == [99, 150]


@pytest.mark.asyncio
async def test_invalid_window_rejected() -> None:
    event_obj = _make_event_obj([])
    with pytest.raises(ValueError, match="window must be positive"):
        await paginated_get_logs(event_obj, from_block=0, to_block=10, window=0)
