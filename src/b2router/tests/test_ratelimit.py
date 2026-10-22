"""The mutation limiter must not grow without bound under key churn."""

from __future__ import annotations

from b2router.ratelimit import RateLimiter


def test_bucket_map_is_capped_on_every_admission_path() -> None:
    limiter = RateLimiter(60, burst=1)
    limiter.MAX_BUCKETS = 5

    for index in range(10):
        allowed, _ = limiter.allow(f"client-{index}")
        assert allowed

    assert len(limiter._buckets) <= limiter.MAX_BUCKETS


def test_denied_request_still_cleans_idle_buckets() -> None:
    limiter = RateLimiter(60, burst=1)
    limiter.MAX_BUCKETS = 5

    for index in range(6):
        limiter.allow(f"client-{index}")

    limited, retry_after = limiter.allow("client-5")
    assert not limited
    assert retry_after > 0
    assert len(limiter._buckets) <= limiter.MAX_BUCKETS
