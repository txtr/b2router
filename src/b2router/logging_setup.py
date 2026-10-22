"""Structured logging and request-scoped context.

Logs are JSON by default in containers and human-friendly locally. Request and
client identifiers are carried in context variables so every log line emitted
while handling a request (including background worker logs) can be correlated.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
client_label_var: ContextVar[str | None] = ContextVar("client_label", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

_STANDARD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        client = client_label_var.get()
        job_id = job_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if client:
            payload["client"] = client
        if job_id:
            payload["job_id"] = job_id
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in {"data"} and not key.startswith("_"):
                payload.setdefault(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class PrettyFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        data = getattr(record, "data", None)
        if isinstance(data, dict) and data:
            line += " " + " ".join(f"{key}={value}" for key, value in data.items())
        request_id = request_id_var.get()
        if request_id:
            line += f" request_id={request_id}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", fmt: str = "pretty") -> None:
    """Install a single root handler; safe to call repeatedly (tests)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_b2router", False):
            root.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(sys.stderr)
    handler._b2router = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if fmt == "json" else PrettyFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event with arbitrary fields."""
    logging.getLogger("b2router").log(level, event, extra={"data": fields})
