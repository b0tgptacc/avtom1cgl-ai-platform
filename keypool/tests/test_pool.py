import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_parse_keys_uses_primary_when_pool_is_empty():
    from pool import parse_keys

    assert parse_keys("primary", "") == ["primary"]


def test_parse_keys_deduplicates_multiple_keys():
    from pool import parse_keys

    assert parse_keys("primary", "second, primary\nthird,second") == ["second", "primary", "third"]


def test_round_robin_distributes_idle_requests():
    from pool import KeyPool

    async def scenario():
        pool = KeyPool(["key-a", "key-b"], cooldown_seconds=30)
        first = await pool.acquire()
        await pool.release(first, 200)
        second = await pool.acquire()
        await pool.release(second, 200)
        return first.index, second.index

    assert asyncio.run(scenario()) == (0, 1)


def test_rate_limited_key_is_skipped_while_another_key_is_healthy():
    from pool import KeyPool

    async def scenario():
        pool = KeyPool(["key-a", "key-b"], cooldown_seconds=30)
        first = await pool.acquire()
        await pool.release(first, 429)
        second = await pool.acquire()
        await pool.release(second, 200)
        return first.index, second.index

    assert asyncio.run(scenario()) == (0, 1)
