"""Durable record of Accounting contract calls and whether Privana saw them succeed."""

import os
from dataclasses import asdict, dataclass
from typing import Literal, Optional

from diskcache import Cache

DEFAULT_STATE_DIR = os.getenv("ACCOUNTING_STATE_DIR", "/data/accounting")

# A week, so a caller resending after a long outage still gets the original reply.
_RECORD_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class AccountingCall:
    """A submitted Accounting call: pending until Privana sees it succeed."""

    status: Literal["pending", "executed"]
    submission_id: Optional[str] = None


class AccountingStore:
    """Disk-backed Accounting calls, keyed by the hash of their calldata."""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR) -> None:
        # No size eviction, and FULL sync keeps a pending record through a host crash.
        self._cache = Cache(
            state_dir,
            eviction_policy="none",
            sqlite_synchronous=2,
        )

    def get(self, key: str) -> Optional[AccountingCall]:
        value = self._cache.get(key)
        if value is None:
            return None
        return AccountingCall(**value)

    def put(self, key: str, call: AccountingCall) -> None:
        self._cache.set(key, asdict(call), expire=_RECORD_TTL_SECONDS)

    def delete(self, key: str) -> None:
        self._cache.delete(key)
