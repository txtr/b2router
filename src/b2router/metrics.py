"""Prometheus metrics for the gateway.

A small vocabulary keeps dashboards predictable: request latency, B2 operation
latency/failures, capacity, upload jobs, routing failures and mutation counts.
Label values are bounded (route rules and stable error codes), never raw URLs.
"""

from __future__ import annotations

from flask import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REQUESTS = Counter(
    "b2router_requests",
    "HTTP requests handled",
    ["method", "route", "status"],
)
REQUEST_LATENCY = Histogram(
    "b2router_request_duration_seconds",
    "HTTP request duration",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

FILES = Gauge("b2router_files", "Unique files tracked in the cache")
USED_BYTES = Gauge("b2router_used_bytes", "Bytes used across the pool")
CAPACITY_BYTES = Gauge("b2router_capacity_bytes", "Configured capacity in bytes")
CAPACITY_UNLIMITED = Gauge(
    "b2router_capacity_unlimited", "1 when at least one account has unlimited capacity"
)
ACCOUNTS = Gauge("b2router_accounts", "Accounts in the pool")

JOBS_QUEUED = Gauge("b2router_jobs_queued", "Upload jobs waiting to run")
JOBS_ACTIVE = Gauge("b2router_jobs_active", "Upload jobs currently running")
UPLOADS = Counter("b2router_uploads", "Upload jobs by result", ["result"])
UPLOAD_BYTES = Counter("b2router_upload_bytes", "Bytes successfully uploaded")
DELETES = Counter("b2router_deletes", "Delete operations by result", ["result"])
ROUTING_FAILURES = Counter("b2router_routing_failures", "Routing failures by reason", ["reason"])
B2_LATENCY = Histogram(
    "b2router_b2_request_duration_seconds",
    "B2 API operation duration",
    ["operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
B2_ERRORS = Counter("b2router_b2_errors", "B2 API errors", ["operation", "code"])

_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def record_request(method: str, route: str, status: int, seconds: float) -> None:
    # Never let an arbitrary method token create unbounded label values.
    safe_method = method if method in _KNOWN_METHODS else "OTHER"
    REQUESTS.labels(method=safe_method, route=route, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=safe_method, route=route).observe(max(seconds, 0.0))


def record_b2(operation: str, seconds: float) -> None:
    B2_LATENCY.labels(operation=operation).observe(max(seconds, 0.0))


def record_b2_error(operation: str, code: str) -> None:
    B2_ERRORS.labels(operation=operation, code=code).inc()


def refresh_pool(pool: object) -> None:
    stats = pool.stats()  # type: ignore[attr-defined]
    FILES.set(stats["files"])
    USED_BYTES.set(stats["used_bytes"])
    capacity = stats["capacity_bytes"]
    if capacity is None:
        CAPACITY_BYTES.set(0)
        CAPACITY_UNLIMITED.set(1)
    else:
        CAPACITY_BYTES.set(capacity)
        CAPACITY_UNLIMITED.set(0)
    ACCOUNTS.set(stats["accounts"])


def refresh_jobs(manager: object) -> None:
    JOBS_QUEUED.set(manager.queued_count())  # type: ignore[attr-defined]
    JOBS_ACTIVE.set(manager.active_count())  # type: ignore[attr-defined]


def metrics_response() -> Response:
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
