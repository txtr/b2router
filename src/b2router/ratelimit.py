"""Per-client mutation rate limiting.

A token bucket keyed by token fingerprint or client address. Reads and polls
are deliberately not limited; only requests that can mutate B2 are.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    MAX_BUCKETS = 10_000

    def __init__(self, rate_per_minute: int, *, burst: int | None = None) -> None:
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst or rate_per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _bound_locked(self, now: float) -> None:
        """Drop idle buckets, then hard-cap the map even under sustained abuse."""
        if len(self._buckets) <= self.MAX_BUCKETS:
            return
        self._buckets = {
            bucket_key: value
            for bucket_key, value in self._buckets.items()
            if now - value[1] < 3600
        }
        if len(self._buckets) > self.MAX_BUCKETS:
            newest = sorted(
                self._buckets.items(),
                key=lambda item: item[1][1],
                reverse=True,
            )[: self.MAX_BUCKETS]
            self._buckets = dict(newest)

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate_per_second)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                self._bound_locked(now)
                retry_after = max((1.0 - tokens) / self.rate_per_second, 0.001)
                return False, retry_after
            self._buckets[key] = (tokens - 1.0, now)
            self._bound_locked(now)
            return True, 0.0
