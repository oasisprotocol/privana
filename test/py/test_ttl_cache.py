"""Tests for AsyncTTLCache behavior."""

import asyncio
import threading
import time

import pytest

from src.services.cache import AsyncTTLCache


class TestAsyncTTLCacheBasic:
    """Basic AsyncTTLCache functionality tests."""

    def test_get_returns_none_for_missing_key(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        assert cache.get("missing") is None

    def test_set_and_get_returns_value(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        cache.set("key", 42)
        assert cache.get("key") == 42

    def test_set_overwrites_existing_value(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        cache.set("key", 1)
        cache.set("key", 2)
        assert cache.get("key") == 2

    def test_invalidate_removes_key(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        cache.set("key", 42)
        cache.invalidate("key")
        assert cache.get("key") is None

    def test_invalidate_nonexistent_key_does_not_raise(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        cache.invalidate("missing")  # Should not raise

    def test_clear_removes_all_keys(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None
        assert cache.get("c") is None


class TestAsyncTTLCacheExpiration:
    """TTL expiration tests."""

    def test_value_expires_after_ttl(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=0.05)  # 50ms TTL
        cache.set("key", 42)
        assert cache.get("key") == 42
        time.sleep(0.06)  # Wait for expiration
        assert cache.get("key") is None

    def test_value_accessible_before_ttl(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=1.0)
        cache.set("key", 42)
        time.sleep(0.1)
        assert cache.get("key") == 42  # Still valid


class TestAsyncTTLCacheLRUEviction:
    """LRU eviction tests."""

    def test_evicts_oldest_when_at_capacity(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # Cache is full: [a, b, c]

        cache.set("d", 4)
        # Should evict "a" (oldest): [b, c, d]

        assert cache.get("a") is None  # Evicted
        assert cache.get("b") == 2
        assert cache.get("c") == 3
        assert cache.get("d") == 4

    def test_access_updates_lru_order(self):
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=3, ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # Order: [a, b, c]

        cache.get("a")  # Access "a", moves to end
        # Order: [b, c, a]

        cache.set("d", 4)
        # Should evict "b" (now oldest): [c, a, d]

        assert cache.get("a") == 1  # Still present
        assert cache.get("b") is None  # Evicted
        assert cache.get("c") == 3
        assert cache.get("d") == 4


class TestAsyncTTLCacheThreadSafety:
    """Thread safety tests."""

    def test_concurrent_writes_do_not_corrupt_cache(self):
        cache: AsyncTTLCache[int, int] = AsyncTTLCache(maxsize=1000, ttl=60)
        num_threads = 10
        writes_per_thread = 100

        def writer(thread_id: int):
            for i in range(writes_per_thread):
                key = thread_id * writes_per_thread + i
                cache.set(key, key * 2)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify some values are correct (cache may have evicted some)
        correct_count = 0
        for i in range(num_threads * writes_per_thread):
            value = cache.get(i)
            if value is not None:
                assert value == i * 2
                correct_count += 1

        # At least maxsize entries should be present
        assert correct_count >= 100


class TestAsyncTTLCacheTupleKeys:
    """Test cache with tuple keys (as used in balance cache)."""

    def test_tuple_keys_work_correctly(self):
        cache: AsyncTTLCache[tuple, int] = AsyncTTLCache(maxsize=10, ttl=60)

        cache.set(("user1", "token1"), 100)
        cache.set(("user1", "token2"), 200)
        cache.set(("user2", "token1"), 300)

        assert cache.get(("user1", "token1")) == 100
        assert cache.get(("user1", "token2")) == 200
        assert cache.get(("user2", "token1")) == 300
        assert cache.get(("user2", "token2")) is None


class TestAsyncTTLCacheAsyncMethods:
    """Test the async-specific methods of AsyncTTLCache."""

    @pytest.mark.asyncio
    async def test_get_or_set_async_basic(self):
        """Test basic async get_or_set functionality."""
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        call_count = 0

        async def async_factory():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # Simulate async work
            return 42

        # First call - factory invoked
        result1 = await cache.get_or_set_async("key", async_factory)
        assert result1 == 42
        assert call_count == 1

        # Second call - cached
        result2 = await cache.get_or_set_async("key", async_factory)
        assert result2 == 42
        assert call_count == 1  # Not called again

    @pytest.mark.asyncio
    async def test_get_or_set_async_concurrent(self):
        """Test concurrent async get_or_set calls."""
        cache: AsyncTTLCache[int, int] = AsyncTTLCache(maxsize=10, ttl=60)
        call_counts: dict[int, int] = {}

        async def async_factory(key: int):
            call_counts[key] = call_counts.get(key, 0) + 1
            await asyncio.sleep(0.02)  # Simulate async work
            return key * 2

        # Launch multiple concurrent requests for different keys
        tasks = [cache.get_or_set_async(i, lambda i=i: async_factory(i)) for i in range(5)]
        results = await asyncio.gather(*tasks)

        # All results should be correct
        assert results == [0, 2, 4, 6, 8]

        # Each key should have been fetched at least once
        for i in range(5):
            assert call_counts.get(i, 0) >= 1

    @pytest.mark.asyncio
    async def test_get_or_set_async_with_ttl_expiration(self):
        """Test that async get_or_set respects TTL."""
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=0.05)  # 50ms TTL
        call_count = 0

        async def async_factory():
            nonlocal call_count
            call_count += 1
            return call_count * 10

        # First call
        result1 = await cache.get_or_set_async("key", async_factory)
        assert result1 == 10
        assert call_count == 1

        # Wait for TTL to expire
        await asyncio.sleep(0.06)

        # Second call after TTL - factory invoked again
        result2 = await cache.get_or_set_async("key", async_factory)
        assert result2 == 20  # New value
        assert call_count == 2


class TestAsyncTTLCacheLifecycleEdges:
    """Test interactions between invalidate/clear and in-flight factories."""

    @pytest.mark.asyncio
    async def test_invalidate_during_inflight_factory(self):
        """Test that invalidate() during in-flight factory doesn't break computation."""
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        factory_started = asyncio.Event()
        factory_continue = asyncio.Event()

        async def slow_factory():
            factory_started.set()
            await factory_continue.wait()
            return 42

        # Start factory in background
        task = asyncio.create_task(cache.get_or_set_async("key", slow_factory))

        # Wait for factory to start
        await factory_started.wait()

        # Invalidate while factory is running - lock should be held, so not removed
        cache.invalidate("key")

        # Let factory complete
        factory_continue.set()
        result = await task

        # Factory should complete successfully
        assert result == 42
        # Value should be cached (set happened after invalidate)
        assert cache.get("key") == 42

    @pytest.mark.asyncio
    async def test_clear_during_inflight_factory(self):
        """Test that clear() during in-flight factory doesn't break computation."""
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        factory_started = asyncio.Event()
        factory_continue = asyncio.Event()

        # Pre-populate cache with other entries
        cache.set("other1", 1)
        cache.set("other2", 2)

        async def slow_factory():
            factory_started.set()
            await factory_continue.wait()
            return 42

        # Start factory in background
        task = asyncio.create_task(cache.get_or_set_async("key", slow_factory))

        # Wait for factory to start
        await factory_started.wait()

        # Clear while factory is running
        cache.clear()

        # Other entries should be gone
        assert cache.get("other1") is None
        assert cache.get("other2") is None

        # Let factory complete
        factory_continue.set()
        result = await task

        # Factory should complete successfully
        assert result == 42
        # Value should be cached (set happened after clear)
        assert cache.get("key") == 42

    @pytest.mark.asyncio
    async def test_concurrent_same_key_with_invalidate(self):
        """Test that concurrent requests for same key work correctly with invalidate."""
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=10, ttl=60)
        call_count = 0
        factory_started = asyncio.Event()
        factory_continue = asyncio.Event()

        async def slow_factory():
            nonlocal call_count
            call_count += 1
            factory_started.set()
            await factory_continue.wait()
            return 42

        # Start first request
        task1 = asyncio.create_task(cache.get_or_set_async("key", slow_factory))
        await factory_started.wait()

        # Start second request for same key (should wait on lock)
        task2 = asyncio.create_task(cache.get_or_set_async("key", slow_factory))

        # Give task2 time to reach lock
        await asyncio.sleep(0.01)

        # Invalidate while both are in progress
        cache.invalidate("key")

        # Let factory complete
        factory_continue.set()

        result1, result2 = await asyncio.gather(task1, task2)

        # Both should get same value
        assert result1 == 42
        assert result2 == 42
        # Factory should only be called once (second request gets cached value)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_lock_not_evicted_during_computation(self):
        """Test that lock eviction doesn't happen while factory is running."""
        # Use maxsize=1 to trigger potential lock eviction
        cache: AsyncTTLCache[str, int] = AsyncTTLCache(maxsize=1, ttl=60)
        factory_a_started = asyncio.Event()
        factory_a_continue = asyncio.Event()
        call_counts = {"a": 0, "b": 0}

        async def factory_a():
            call_counts["a"] += 1
            factory_a_started.set()
            await factory_a_continue.wait()
            return 1

        async def factory_b():
            call_counts["b"] += 1
            return 2

        # Start factory for key "a"
        task_a = asyncio.create_task(cache.get_or_set_async("a", factory_a))
        await factory_a_started.wait()

        # Insert key "b" - this could potentially evict key "a"'s lock in old impl
        result_b = await cache.get_or_set_async("b", factory_b)
        assert result_b == 2

        # Let factory_a complete
        factory_a_continue.set()
        result_a = await task_a

        # Factory A should complete successfully
        assert result_a == 1
        # Factory A should only be called once (lock wasn't evicted)
        assert call_counts["a"] == 1
