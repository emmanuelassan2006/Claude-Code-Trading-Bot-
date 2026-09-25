"""Async token-bucket rate limiter shared by all REST calls."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, rate_per_s: float, burst: int) -> None:
        self.rate = rate_per_s
        self.capacity = max(1, burst)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


def backoff(attempt: int, initial: float, maximum: float) -> float:
    """Exponential backoff with equal jitter."""
    import random

    capped = min(maximum, initial * (2 ** attempt))
    return capped / 2 + random.random() * capped / 2
