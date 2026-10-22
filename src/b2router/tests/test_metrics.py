"""Metric labels stay bounded even when clients send arbitrary methods."""

from __future__ import annotations

from prometheus_client import REGISTRY

from b2router import metrics


def test_unknown_http_method_is_normalized() -> None:
    metrics.record_request("FROB", "/api/test-route", 200, 0.01)

    assert (
        REGISTRY.get_sample_value(
            "b2router_requests_total",
            {
                "method": "OTHER",
                "route": "/api/test-route",
                "status": "200",
            },
        )
        == 1.0
    )


def test_known_http_method_is_preserved() -> None:
    metrics.record_request("GET", "/api/test-route", 200, 0.01)

    assert (
        REGISTRY.get_sample_value(
            "b2router_requests_total",
            {
                "method": "GET",
                "route": "/api/test-route",
                "status": "200",
            },
        )
        == 1.0
    )
