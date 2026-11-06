"""In-memory, least-busy key pool for one approved upstream model endpoint."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class KeySlot:
    index: int
    key: str
    in_flight: int = 0
    cooldown_until: float = 0.0
    requests_total: int = 0
    requests_success: int = 0
    requests_rate_limited: int = 0
    requests_error: int = 0
    cooldown_count: int = 0
    latency_seconds_total: float = 0.0


def parse_keys(primary: str, pooled: str) -> list[str]:
    """Parse comma/newline keys, falling back to the legacy primary key."""
    raw = pooled.replace("\n", ",").split(",") if pooled else []
    values = [value.strip() for value in raw if value.strip()] or [primary.strip()]
    if not all(values):
        raise ValueError("UPSTREAM_API_KEY or UPSTREAM_API_KEYS is required")
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


class KeyPool:
    def __init__(self, keys: list[str], cooldown_seconds: float = 30.0):
        if not keys:
            raise ValueError("at least one upstream key is required")
        self._slots = [KeySlot(index=i, key=key) for i, key in enumerate(keys)]
        self._cooldown_seconds = cooldown_seconds
        self._next = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> KeySlot:
        async with self._lock:
            now = time.monotonic()
            healthy = [slot for slot in self._slots if slot.cooldown_until <= now]
            candidates = healthy or self._slots
            min_in_flight = min(slot.in_flight for slot in candidates)
            tied = [slot for slot in candidates if slot.in_flight == min_in_flight]
            slot = min(tied, key=lambda item: (item.index - self._next) % len(self._slots))
            self._next = (slot.index + 1) % len(self._slots)
            slot.in_flight += 1
            slot.requests_total += 1
            return slot

    async def release(self, slot: KeySlot, status_code: int | None, latency_seconds: float = 0.0) -> None:
        async with self._lock:
            slot.in_flight = max(0, slot.in_flight - 1)
            slot.latency_seconds_total += max(0.0, latency_seconds)
            if status_code is not None and 200 <= status_code < 400:
                slot.requests_success += 1
            elif status_code == 429:
                slot.requests_rate_limited += 1
            else:
                slot.requests_error += 1
            if status_code in (429, 500, 502, 503, 504):
                slot.cooldown_count += 1
                slot.cooldown_until = time.monotonic() + self._cooldown_seconds

    async def metrics(self) -> list[dict[str, int | float]]:
        async with self._lock:
            now = time.monotonic()
            return [
                {
                    "index": slot.index,
                    "in_flight": slot.in_flight,
                    "requests_total": slot.requests_total,
                    "requests_success": slot.requests_success,
                    "requests_rate_limited": slot.requests_rate_limited,
                    "requests_error": slot.requests_error,
                    "cooldown_count": slot.cooldown_count,
                    "latency_seconds_total": round(slot.latency_seconds_total, 6),
                    "cooldown_seconds": max(0.0, round(slot.cooldown_until - now, 3)),
                }
                for slot in self._slots
            ]
