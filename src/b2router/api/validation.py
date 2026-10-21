"""Request validation and small HTTP helpers.

All validators raise ``Problem`` with a stable code so route handlers stay
linear and error responses stay consistent.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from flask import Request
from werkzeug.datastructures import FileStorage

from b2router.errors import Problem, bad_request, validation_problem

CONTROL_CHARS = {chr(code) for code in range(32)} | {"\x7f"}


def json_body(request: Request) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > 1_000_000:
        raise Problem("payload_too_large", "JSON request bodies are limited to 1 MB.")
    if not request.is_json:
        raise Problem(
            "unsupported_media_type",
            "Expected an application/json request body.",
        )
    # Content-Length is advisory (chunked bodies omit it), so cap the stream
    # itself rather than trusting the header.
    raw = request.stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise Problem("payload_too_large", "JSON request bodies are limited to 1 MB.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise bad_request("The request body is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise validation_problem("The JSON body must be an object.")
    return data


def validate_file_name(name: object, *, max_bytes: int = 1000) -> str:
    if not isinstance(name, str) or not name:
        raise validation_problem("A file name is required.")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:  # pragma: no cover - surrogate input
        raise validation_problem("The file name is not valid UTF-8.") from exc
    if len(encoded) > max_bytes:
        raise validation_problem(f"The file name must be at most {max_bytes} bytes.")
    if name.startswith("/") or name.endswith("/"):
        raise validation_problem("The file name must not start or end with '/'.")
    if "\\" in name:
        raise validation_problem("The file name must not contain backslashes.")
    segments = name.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise validation_problem("The file name contains an empty, '.' or '..' path segment.")
    if any(char in CONTROL_CHARS for char in name):
        raise validation_problem("The file name must not contain control characters.")
    return name


def validate_size(value: object, *, max_bytes: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise validation_problem("File size must be an integer number of bytes.")
    if value < 0:
        raise validation_problem("File size must not be negative.")
    if value > max_bytes:
        raise validation_problem(f"File size must be at most {max_bytes} bytes.")
    return value


def int_arg(
    request: Request,
    name: str,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise validation_problem(f"Query parameter {name!r} must be an integer.") from exc
    if value < minimum:
        raise validation_problem(f"Query parameter {name!r} must be >= {minimum}.")
    if maximum is not None and value > maximum:
        raise validation_problem(f"Query parameter {name!r} must be <= {maximum}.")
    return value


def str_arg(request: Request, name: str, *, default: str = "", max_length: int = 200) -> str:
    value = request.args.get(name, default) or ""
    if len(value) > max_length:
        raise validation_problem(
            f"Query parameter {name!r} must be at most {max_length} characters."
        )
    return value


def choice_arg(request: Request, name: str, choices: set[str], *, default: str) -> str:
    value = str_arg(request, name, default=default)
    if value not in choices:
        raise validation_problem(
            f"Query parameter {name!r} must be one of: {', '.join(sorted(choices))}."
        )
    return value


def parse_etag(header: str | None) -> str | None:
    """Return the identifier from ``If-Match``/``If-None-Match``."""
    if header is None:
        return None
    value = header.strip()
    if value == "*":
        return "*"
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def etag_matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    stripped = header.strip()
    if stripped == "*":
        return True
    candidates = [part.strip() for part in stripped.split(",")]
    normalised = {candidate.removeprefix("W/").strip('"') for candidate in candidates}
    return etag in normalised


def make_etag(*parts: object) -> str:
    import hashlib

    digest = hashlib.sha256("\x00".join(str(part) for part in parts).encode()).hexdigest()[:20]
    return digest


def content_disposition(name: str) -> str:
    fallback = (
        name.encode("ascii", "replace")
        .decode("ascii")
        .replace("\\", "_")
        .replace('"', "_")
        .replace("\r", "_")
        .replace("\n", "_")
    )
    encoded = quote(name, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def safe_content_type(value: str | None) -> str:
    if not value:
        return "application/octet-stream"
    if any(char in CONTROL_CHARS for char in value) or len(value) > 255:
        return "application/octet-stream"
    if "/" not in value:
        return "application/octet-stream"
    return value


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header, returning inclusive bounds."""
    if not header:
        return None
    if not header.startswith("bytes="):
        raise Problem("range_not_satisfiable", "Only byte ranges are supported.")
    spec = header[len("bytes=") :].strip()
    if "," in spec:
        raise Problem("range_not_satisfiable", "Multiple ranges are not supported.")
    try:
        start_raw, end_raw = spec.split("-", 1)
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0 or size == 0:
                raise ValueError
            start = max(size - suffix, 0)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
    except ValueError as exc:
        raise Problem("range_not_satisfiable", "The byte range is malformed.") from exc
    if size == 0 or start < 0 or start >= size or end < start:
        raise Problem(
            "range_not_satisfiable",
            "The requested range is outside the file.",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    return start, end


@dataclass(frozen=True)
class SpooledUpload:
    file_name: str
    content_type: str
    path: str
    size: int


def spool_upload(
    storage: FileStorage,
    *,
    file_name: str,
    settings_max_bytes: int,
    temp_dir: str,
) -> SpooledUpload:
    """Stream a multipart part to a private temp file, enforcing the size cap."""
    os.makedirs(temp_dir, exist_ok=True)
    free = shutil.disk_usage(temp_dir).free
    if free < settings_max_bytes:
        raise Problem(
            "service_unavailable",
            "The upload staging area is low on disk space.",
        )
    descriptor, path = tempfile.mkstemp(prefix="b2router-up-", dir=temp_dir)
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while True:
                chunk = storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings_max_bytes:
                    raise Problem(
                        "payload_too_large",
                        f"File exceeds the {settings_max_bytes}-byte limit.",
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return SpooledUpload(
        file_name=file_name,
        content_type=safe_content_type(storage.content_type),
        path=path,
        size=size,
    )
