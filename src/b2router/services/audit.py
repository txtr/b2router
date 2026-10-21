"""Bounded, thread-safe audit trail for every mutation."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class AuditEntry:
    id: str
    timestamp: str
    action: str
    result: str
    file_name: str | None = None
    account_name: str | None = None
    file_size: int | None = None
    error_code: str | None = None
    request_id: str | None = None
    client: str | None = None
    duration_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "action": self.action,
            "result": self.result,
            "file_name": self.file_name,
            "account_name": self.account_name,
            "file_size": self.file_size,
            "error_code": self.error_code,
            "request_id": self.request_id,
            "client": self.client,
            "duration_ms": self.duration_ms,
            "details": self.details,
        }


class AuditLog:
    def __init__(self, maxlen: int = 1000) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._sequence = 0

    def append(
        self,
        action: str,
        *,
        result: str,
        file_name: str | None = None,
        account_name: str | None = None,
        file_size: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        client: str | None = None,
        duration_ms: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        with self._lock:
            self._sequence += 1
            entry = AuditEntry(
                id=f"aud_{self._sequence:08d}_{uuid.uuid4().hex[:8]}",
                timestamp=datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                action=action,
                result=result,
                file_name=file_name,
                account_name=account_name,
                file_size=file_size,
                error_code=error_code,
                request_id=request_id,
                client=client,
                duration_ms=round(duration_ms, 2) if duration_ms is not None else None,
                details=details or {},
            )
            self._entries.appendleft(entry)
            return entry

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[list[AuditEntry], int]:
        with self._lock:
            items = list(self._entries)
        total = len(items)
        return items[offset : offset + limit], total
