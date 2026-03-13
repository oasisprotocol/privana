"""Async-friendly caching utilities."""

import asyncio
import time
from typing import Awaitable, Callable, Generic, Optional, TypeVar

from cachetools import TTLCache as CachetoolsTTLCache

K = TypeVar("K")
V = TypeVar("V")


class AsyncTTLCache(Generic[K, V]):
    """Async-friendly LRU cache with TTL expiration and bounded size.

    Wraps cachetools.TTLCache for use in async contexts. No thread locks needed
    since asyncio runs in a single thread. Uses asyncio.Lock for the async
    get_or_set pattern to avoid duplicate computation under concurrent access.
    Uses time.monotonic() for TTL calculations to avoid issues with clock adjustments.
    """

    def __init__(self, maxsize: int, ttl: float) -> None:
        self._cache: CachetoolsTTLCache[K, V] = CachetoolsTTLCache(
            maxsize=maxsize, ttl=ttl, timer=time.monotonic
        )
        self._locks: dict[K, asyncio.Lock] = {}
        self._maxsize = maxsize

    def get(self, key: K) -> Optional[V]:
        """Get value if present and not expired, otherwise return None."""
        return self._cache.get(key)

    def set(self, key: K, value: V) -> None:
        """Set value with TTL, evicting oldest if at capacity."""
        self._cache[key] = value

    async def get_or_set_async(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        """Get value from cache, or compute and cache it if missing/expired.

        Uses per-key locking to avoid duplicate computation for the same key
        while allowing concurrent computation for different keys.
        """
        # Fast path: check cache
        value = self._cache.get(key)
        if value is not None:
            return value

        # Lazy cleanup: remove unheld locks for keys no longer in cache
        if len(self._locks) > self._maxsize * 2:
            stale_keys = [k for k in self._locks if k not in self._cache]
            for k in stale_keys:
                lock = self._locks.get(k)
                if lock is not None and not lock.locked():
                    self._locks.pop(k, None)

        # Get or create lock for this key
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        lock = self._locks[key]

        async with lock:
            # Check again after acquiring lock
            value = self._cache.get(key)
            if value is not None:
                return value

            # Compute and cache
            value = await factory()
            self._cache[key] = value
            return value

    def invalidate(self, key: K) -> None:
        """Remove a specific key from cache and its lock (if not held)."""
        self._cache.pop(key, None)
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)

    def clear(self) -> None:
        """Clear all entries and locks."""
        self._cache.clear()
        self._locks.clear()
