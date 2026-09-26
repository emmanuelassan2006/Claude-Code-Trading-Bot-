"""Time-ordered price history shared by all price feeds and the model."""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Tick:
    ts: float      # source timestamp, seconds
    value: float


class PriceHistory:
    """Time-ordered ticks for one symbol with candidate price-to-beat rules."""

    def __init__(self, max_age_s: float = 7200.0) -> None:
        self.max_age_s = max_age_s
        self._ts: deque[float] = deque()
        self._vals: deque[float] = deque()

    def add(self, tick: Tick) -> None:
        if self._ts and tick.ts < self._ts[-1]:
            # out-of-order: insert (rare; keeps the series sorted)
            ts, vals = list(self._ts), list(self._vals)
            i = bisect.bisect_right(ts, tick.ts)
            ts.insert(i, tick.ts)
            vals.insert(i, tick.value)
            self._ts, self._vals = deque(ts), deque(vals)
        else:
            self._ts.append(tick.ts)
            self._vals.append(tick.value)
        cutoff = tick.ts - self.max_age_s
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()
            self._vals.popleft()

    def latest(self) -> Tick | None:
        return Tick(self._ts[-1], self._vals[-1]) if self._ts else None

    def at_or_after(self, t: float) -> Tick | None:
        ts = list(self._ts)
        i = bisect.bisect_left(ts, t)
        return Tick(ts[i], self._vals[i]) if i < len(ts) else None

    def at_or_before(self, t: float) -> Tick | None:
        ts = list(self._ts)
        i = bisect.bisect_right(ts, t) - 1
        return Tick(ts[i], self._vals[i]) if i >= 0 else None

    def twap(self, start: float, end: float) -> float | None:
        """Time-weighted average over [start, end], treating ticks as a step function.

        Returns None unless a price is known at `start` and data extends to `end`.
        """
        ts, vals = list(self._ts), list(self._vals)
        if end <= start or not ts or ts[-1] < end:
            return None
        i = bisect.bisect_right(ts, start) - 1
        if i < 0:
            return None
        total, t = 0.0, start
        while t < end:
            seg_end = min(ts[i + 1], end) if i + 1 < len(ts) else end
            total += vals[i] * (seg_end - t)
            t = seg_end
            i += 1
        return total / (end - start)

    def average(self, start: float, end: float) -> float | None:
        """Like twap() but carries the last known price forward past the last tick.

        Used for the already-realized part of a settlement average.
        """
        ts, vals = list(self._ts), list(self._vals)
        if end <= start or not ts:
            return None
        i = bisect.bisect_right(ts, start) - 1
        if i < 0:
            return None
        total, t = 0.0, start
        while t < end:
            seg_end = min(ts[i + 1], end) if i + 1 < len(ts) else end
            total += vals[i] * (seg_end - t)
            t = seg_end
            i += 1
        return total / (end - start)

    def series(self) -> tuple[list[float], list[float]]:
        return list(self._ts), list(self._vals)

    def candidates(self, boundary: float, twap_s: float) -> dict[str, float | None]:
        """Candidate reference prices at a window boundary (open or close)."""
        first = self.at_or_after(boundary)
        last = self.at_or_before(boundary)
        return {
            "first_tick_after": first.value if first else None,
            "last_tick_before": last.value if last else None,
            "twap_ending": self.twap(boundary - twap_s, boundary),
            "twap_starting": self.twap(boundary, boundary + twap_s),
        }
