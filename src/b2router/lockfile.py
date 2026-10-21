"""Single-writer enforcement.

The cache, reservations and job registry live in memory, so two processes
sharing the same configuration would silently diverge. An advisory lock on a
small file makes that impossible instead of merely documenting it.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # pragma: no cover - platform guard
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class LockError(Exception):
    pass


class InstanceLock:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._fh: object | None = None

    def acquire(self) -> None:
        if fcntl is None:  # pragma: no cover
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - fd must outlive this method
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise LockError(
                f"Another b2router instance holds {self.path}. "
                "The in-memory cache requires exactly one writer process."
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            self._fh.close()  # type: ignore[attr-defined]
            self._fh = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
