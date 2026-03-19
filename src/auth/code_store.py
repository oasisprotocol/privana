"""Short-lived single-use authorization-code store."""

import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from diskcache import Cache

from src.config import load_settings


@dataclass(frozen=True)
class AuthCodeRecord:
    """Single-use authorization-code record."""

    user_address: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str


class AuthCodeStore:
    """Disk-backed one-time authorization-code storage."""

    def __init__(self) -> None:
        settings = load_settings()
        storage_root = Path(settings.auth_token_storage_dir)
        self._cache = Cache(
            str(storage_root / "auth_codes"),
            disk_min_file_size=0,
            sqlite_journal_mode="WAL",
        )
        self._ttl_seconds = settings.auth_code_ttl_seconds

    def close(self) -> None:
        """Close the underlying diskcache handle."""
        self._cache.close()

    def create_code(self, record: AuthCodeRecord) -> str:
        """Create and store a new authorization code."""
        code = secrets.token_urlsafe(32)
        self._cache.set(f"auth_code:{code}", asdict(record), expire=self._ttl_seconds)
        return code

    def get_code(self, code: str) -> Optional[AuthCodeRecord]:
        """Return an authorization-code record without consuming it."""
        value = self._cache.get(f"auth_code:{code}", default=None)
        if value is None:
            return None
        return AuthCodeRecord(**value)

    def consume_code(self, code: str) -> Optional[AuthCodeRecord]:
        """Atomically consume and return an authorization-code record."""
        value = self._cache.pop(f"auth_code:{code}", default=None)
        if value is None:
            return None
        return AuthCodeRecord(**value)

    @property
    def ttl_seconds(self) -> int:
        """Configured authorization-code lifetime."""
        return self._ttl_seconds


_auth_code_store_instance: Optional[AuthCodeStore] = None


def get_auth_code_store() -> AuthCodeStore:
    """Return the process-wide auth-code store singleton."""
    global _auth_code_store_instance
    if _auth_code_store_instance is None:
        _auth_code_store_instance = AuthCodeStore()
    return _auth_code_store_instance
