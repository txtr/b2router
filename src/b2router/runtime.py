"""Runtime container stored on ``app.extensions``."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool
from b2router.services.uploads import UploadManager
from b2router.settings import Settings


@dataclass
class Runtime:
    settings: Settings
    pool: AccountPool
    manager: UploadManager
    audit: AuditLog
    started_at: float = field(default_factory=time.time)
    lock: object | None = None

    @property
    def uptime_seconds(self) -> float:
        return round(time.time() - self.started_at, 3)

    @property
    def started_iso(self) -> str:
        return (
            datetime.fromtimestamp(self.started_at, tz=UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    def shutdown(self) -> None:
        # Keep the writer lock until accepted uploads and deletes are drained.
        self.manager.shutdown()
        release = getattr(self.lock, "release", None)
        if callable(release):
            release()
