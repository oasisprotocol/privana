"""Disk-backed auth endpoint rate limiter."""

import ipaddress
import time
from pathlib import Path
from typing import Optional

from diskcache import Cache
from fastapi import Request

from src.config import load_settings


class AuthRateLimiter:
    """Process-safe fixed-window rate limiter for auth endpoints."""

    def __init__(self) -> None:
        settings = load_settings()
        storage_root = Path(settings.auth_token_storage_dir)
        self._cache = Cache(
            str(storage_root / "rate_limits"),
            disk_min_file_size=0,
            sqlite_journal_mode="WAL",
        )

    def close(self) -> None:
        """Close the underlying cache."""
        self._cache.close()

    def hit(self, bucket: str, key: str, limit: int, window_seconds: int) -> Optional[int]:
        """Record a request and return retry-after seconds when over the limit."""
        if limit <= 0 or window_seconds <= 0:
            return None

        now = time.time()
        cache_key = f"rate_limit:{bucket}:{key}"
        with self._cache.transact():
            existing = self._cache.get(cache_key)
            if not isinstance(existing, dict):
                reset_at = now + window_seconds
                self._cache.set(
                    cache_key,
                    {"count": 1, "reset_at": reset_at},
                    expire=window_seconds,
                )
                return None

            reset_at = float(existing.get("reset_at", 0))
            if reset_at <= now:
                reset_at = now + window_seconds
                self._cache.set(
                    cache_key,
                    {"count": 1, "reset_at": reset_at},
                    expire=window_seconds,
                )
                return None

            count = int(existing.get("count", 0))
            if count >= limit:
                return max(1, int(reset_at - now))

            self._cache.set(
                cache_key,
                {"count": count + 1, "reset_at": reset_at},
                expire=max(1, int(reset_at - now)),
            )
            return None


def request_identity(request: Request) -> str:
    """Return the best-effort client identity for rate limiting."""
    settings = load_settings()
    if settings.trust_x_forwarded_for:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            candidate = forwarded_for.split(",")[0].strip()
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


_auth_rate_limiter_instance: Optional[AuthRateLimiter] = None


def get_auth_rate_limiter() -> AuthRateLimiter:
    """Return the process-wide auth rate limiter singleton."""
    global _auth_rate_limiter_instance
    if _auth_rate_limiter_instance is None:
        _auth_rate_limiter_instance = AuthRateLimiter()
    return _auth_rate_limiter_instance
