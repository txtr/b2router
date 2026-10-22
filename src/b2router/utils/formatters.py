"""Presentation helpers shared by the JSON API and the server-rendered UI."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def human_size(n_bytes: int | None) -> str:
    if n_bytes is None:
        return "Unlimited"
    size = float(max(n_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - unreachable


def format_timestamp(upload_timestamp_ms: int | None) -> str:
    if upload_timestamp_ms is None:
        return "—"
    try:
        moment = datetime.fromtimestamp(upload_timestamp_ms / 1000, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return str(upload_timestamp_ms)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def format_iso(value: str | None) -> str:
    """Format an ISO-8601 timestamp for dense operator tables."""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def format_pool_stats(stats: dict[str, Any]) -> dict[str, Any]:
    capacity = stats.get("capacity_bytes")
    used = stats.get("used_bytes") or 0
    free = stats.get("free_bytes")
    utilisation = None if not capacity else round((used / capacity) * 100, 1)
    return {
        "files": stats.get("files", 0),
        "size_bytes": stats.get("size_bytes", 0),
        "size_human": human_size(stats.get("size_bytes", 0)),
        "used_bytes": used,
        "used_human": human_size(used),
        "free_bytes": free,
        "free_human": human_size(free),
        "capacity_bytes": capacity,
        "capacity_human": human_size(capacity),
        "accounts": stats.get("accounts", 0),
        "reserved_bytes": stats.get("reserved_bytes", 0),
        "utilization_percent": utilisation,
        "revision": stats.get("revision", 0),
    }
