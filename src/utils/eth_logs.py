"""Block-range-paginated `eth_getLogs` helper.

Sapphire's confidential VM caps `eth_getLogs` at 100 blocks per request, so a
`from_block=0` scan fails once chain history exceeds the cap. This helper
splits the range into fixed windows and concatenates results in block order.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Sapphire confidential VM caps `eth_getLogs` at 100 blocks per request.
SAPPHIRE_GETLOGS_WINDOW = 100


async def paginated_get_logs(
    event_obj: Any,
    *,
    from_block: int,
    to_block: int,
    window: int = SAPPHIRE_GETLOGS_WINDOW,
    argument_filters: Optional[Dict[str, Any]] = None,
) -> list[Any]:
    """Fetch logs over `[from_block, to_block]` in `window`-block chunks.

    Returns events concatenated in block order. An empty range
    (`from_block > to_block`) returns `[]` without making an RPC call.
    Any chunk failure raises immediately; the partial result is discarded.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    if from_block > to_block:
        return []

    out: list[Any] = []
    cursor = from_block
    while cursor <= to_block:
        end = min(cursor + window - 1, to_block)
        kwargs: Dict[str, Any] = {"from_block": cursor, "to_block": end}
        if argument_filters is not None:
            kwargs["argument_filters"] = argument_filters
        out.extend(await event_obj.get_logs(**kwargs))
        cursor = end + 1
    return out
