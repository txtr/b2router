"""Streaming content responses shared by ``/api`` and ``/ui``.

The API identifies callers with a bearer token, while the UI uses a session
cookie. Both end up here so ETag/range/Content-Disposition behaviour cannot
drift between the two surfaces.
"""

from __future__ import annotations

from typing import Any

from flask import Request, Response

from b2router.api.validation import (
    content_disposition,
    etag_matches,
    parse_range,
    safe_content_type,
)
from b2router.errors import Problem
from b2router.logging_setup import log_event
from b2router.runtime import Runtime
from b2router.services.account import StorageError
from b2router.services.pool import FileEntry


def _quoted(etag: str) -> str:
    return etag if etag.startswith('"') else f'"{etag}"'


def not_modified(etag: str) -> Response:
    response = Response(status=304)
    response.headers["ETag"] = _quoted(etag)
    return response


def content_headers(entry: FileEntry) -> dict[str, str]:
    return {
        "Content-Type": safe_content_type(entry.content_type),
        "Content-Disposition": content_disposition(entry.file_name),
        "Accept-Ranges": "bytes",
        "ETag": _quoted(entry.etag),
        "Cache-Control": "private, max-age=0, must-revalidate",
    }


def content_response(rt: Runtime, file_name: str, request: Request) -> Response:
    entry = rt.pool.get_file(file_name)
    if entry is None:
        raise Problem("file_not_found", f"File {file_name!r} is not in the pool.", status=404)
    if etag_matches(request.headers.get("If-None-Match"), entry.etag):
        return not_modified(entry.etag)

    account = rt.pool.get(entry.accounts[0])
    if account is None:
        raise Problem("file_not_found", "The owning account is no longer in the pool.", status=404)

    byte_range = parse_range(request.headers.get("Range"), entry.size)
    headers = content_headers(entry)
    if byte_range is not None:
        start, end = byte_range
        headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
        headers["Content-Length"] = str(end - start + 1)
        status = 206
    else:
        start = end = 0
        headers["Content-Length"] = str(entry.size)
        status = 200

    if request.method == "HEAD":
        return Response(status=status, headers=headers)

    try:
        handle = account.download(
            entry.file_id,
            entry.file_name,
            range_=(start, end) if byte_range is not None else None,
        )
    except StorageError as exc:
        raise Problem(exc.code, exc.message, status=502) from exc

    def generate() -> Any:
        try:
            yield from handle.iter_chunks()
        except Exception as exc:
            # Headers have already been sent, so a clean HTTP error is no longer
            # possible. Propagating aborts the connection rather than making a
            # truncated body look like a successful download.
            log_event(
                "download.stream_failed",
                level=30,
                file_name=file_name,
                error=type(exc).__name__,
            )
            raise
        finally:
            handle.close()

    return Response(generate(), status=status, headers=headers)
