"""Durable record of submitted operations and whether Privana saw them succeed."""

import os
from dataclasses import asdict, dataclass
from typing import Literal, Optional

from diskcache import Cache

DEFAULT_STATE_DIR = os.getenv("OPERATION_STATE_DIR", "/data/operations")

# A week, so a caller resending after a long outage still gets the original reply.
_RECORD_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class OperationRecord:
    """A submitted operation: pending until Privana sees it succeed."""

    status: Literal["pending", "executed"]
    submission_id: Optional[str] = None


class OperationStore:
    """Disk-backed operation records, keyed by the operation's EIP-712 digest."""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR) -> None:
        # Records leave only by expiring, and FULL sync keeps a pending record through a host crash.
        self._cache = Cache(
            state_dir,
            eviction_policy="none",
            sqlite_journal_mode="WAL",
            sqlite_synchronous=2,
        )

    def get(self, operation_id: str) -> Optional[OperationRecord]:
        value = self._cache.get(operation_id, default=None)
        if value is None:
            return None
        return OperationRecord(**value)

    def put(self, operation_id: str, record: OperationRecord) -> None:
        self._cache.set(operation_id, asdict(record), expire=_RECORD_TTL_SECONDS)

    def delete(self, operation_id: str) -> None:
        self._cache.delete(operation_id)
