"""HTTP API for the b2router gateway.

JSON only (RFC 9457 for failures). The UI is served separately under ``/ui``;
nothing here returns HTML.

Route families:
  /health /ready /metrics        operational endpoints
  /api/stats /api/accounts       read-only pool state
  /api/files[...]                cache + content, upload/upsert/delete
  /api/routing-plans             side-effect-free placement preview
  /api/jobs[...]                 asynchronous upload lifecycle
  /api/audit                     recent mutations
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request

from b2router import metrics
from b2router.api.validation import (
    SpooledUpload,
    choice_arg,
    etag_matches,
    int_arg,
    json_body,
    make_etag,
    parse_etag,
    spool_upload,
    str_arg,
    validate_file_name,
    validate_size,
)
from b2router.errors import Problem, validation_problem
from b2router.logging_setup import request_id_var
from b2router.runtime import Runtime
from b2router.services.content import content_response
from b2router.services.files import delete_file as delete_file_entry
from b2router.services.pool import FileEntry
from b2router.services.uploads import TERMINAL_STATES, JobState, UploadRejected

bp = Blueprint("b2router_api", __name__)


def runtime() -> Runtime:
    return current_app.extensions["b2router"]  # type: ignore[no-any-return]


def _quoted(etag: str) -> str:
    return f'"{etag}"'


def _not_modified(etag: str) -> Response:
    return Response(status=304, headers={"ETag": _quoted(etag)})


def _error_doc(code: str, message: str, **details: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {"code": code, "message": message}
    if details:
        doc["details"] = details
    return doc


# ---------------------------------------------------------------------------
# Operational endpoints
# ---------------------------------------------------------------------------
@bp.get("/health")
def health() -> Response:
    rt = runtime()
    return jsonify(
        {
            "status": "ok",
            "version": rt.settings.version,
            "started_at": rt.started_iso,
            "uptime_seconds": rt.uptime_seconds,
            "pool_revision": rt.pool.revision,
            "jobs_revision": rt.manager.revision,
        }
    )


@bp.get("/ready")
def ready() -> Response:
    rt = runtime()
    checks = {
        "pool_loaded": len(rt.pool) > 0,
        "pool_has_files_or_empty": True,
        "upload_workers": rt.settings.upload_workers > 0,
        "accepting_mutations": not rt.manager.closed,
    }
    if not all(checks.values()):
        raise Problem(
            "not_ready",
            "The gateway is not ready to serve traffic.",
            status=503,
            extensions={"checks": checks},
        )
    return jsonify(
        {
            "status": "ready",
            "checks": checks,
            "accounts": len(rt.pool),
            "files": rt.pool.stats()["files"],
            "pool_revision": rt.pool.revision,
        }
    )


@bp.get("/metrics")
def prometheus_metrics() -> Response:
    if not runtime().settings.metrics_enabled:
        raise Problem("not_found", "Metrics are disabled.", status=404)
    return metrics.metrics_response()


@bp.get("/")
def service_info() -> Response:
    rt = runtime()
    return jsonify(
        {
            "service": "b2router",
            "version": rt.settings.version,
            "api": {
                "files": "/api/files",
                "jobs": "/api/jobs",
                "stats": "/api/stats",
                "accounts": "/api/accounts",
                "routing_plans": "/api/routing-plans",
                "audit": "/api/audit",
                "download": "/api/download/{file_name}",
                "health": "/health",
                "metrics": "/metrics",
            },
        }
    )


# ---------------------------------------------------------------------------
# Pool state
# ---------------------------------------------------------------------------
@bp.get("/api/stats")
def stats() -> Response:
    rt = runtime()
    body = rt.pool.stats()
    revision = rt.pool.revision
    etag = make_etag("stats", revision)
    if etag_matches(request.headers.get("If-None-Match"), etag):
        return _not_modified(etag)
    metrics.refresh_pool(rt.pool)
    response = jsonify({**body, "revision": revision})
    response.headers["ETag"] = _quoted(etag)
    return response


@bp.get("/api/accounts")
def accounts() -> Response:
    rt = runtime()
    revision = rt.pool.revision
    etag = make_etag("accounts", revision)
    if etag_matches(request.headers.get("If-None-Match"), etag):
        return _not_modified(etag)
    items = [state.to_dict() for state in rt.pool.account_states()]
    response = jsonify({"items": items, "total": len(items), "revision": revision})
    response.headers["ETag"] = _quoted(etag)
    return response


@bp.get("/api/accounts/<name>")
def account_detail(name: str) -> Response:
    state = runtime().pool.get_account_state(name)
    if state is None:
        raise Problem("account_not_found", f"Account {name!r} is not in the pool.", status=404)
    return jsonify(state.to_dict())


# ---------------------------------------------------------------------------
# File cache
# ---------------------------------------------------------------------------
def _filtered_files(rt: Runtime) -> list[FileEntry]:
    entries = rt.pool.list_files()
    query = str_arg(request, "q", max_length=200).casefold()
    account = str_arg(request, "account", max_length=128)
    if query:
        entries = [entry for entry in entries if query in entry.file_name.casefold()]
    if account:
        entries = [entry for entry in entries if account in entry.accounts]
    sort = choice_arg(request, "sort", {"name", "size", "uploaded_at"}, default="name")
    order = choice_arg(request, "order", {"asc", "desc"}, default="asc")
    if sort == "size":
        entries.sort(key=lambda entry: (entry.size, entry.file_name), reverse=order == "desc")
    elif sort == "uploaded_at":
        entries.sort(
            key=lambda entry: (entry.upload_timestamp, entry.file_name),
            reverse=order == "desc",
        )
    else:
        entries.sort(key=lambda entry: entry.file_name, reverse=order == "desc")
    return entries


@bp.get("/api/files")
def list_files() -> Response:
    rt = runtime()
    entries = _filtered_files(rt)
    total = len(entries)
    limit = int_arg(request, "limit", default=100, minimum=1, maximum=1000)
    cursor = request.args.get("after")
    offset = int_arg(request, "offset", default=0, minimum=0)
    start = offset
    if cursor:
        if len(cursor) > 1024:
            raise validation_problem("The 'after' cursor is too long.")
        after = cursor
        start = next(
            (index + 1 for index, entry in enumerate(entries) if entry.file_name == after),
            total,
        )
    page = entries[start : start + limit]
    next_cursor = page[-1].file_name if page and start + limit < total else None
    revision = rt.pool.revision
    etag = make_etag(
        "files",
        revision,
        request.args.get("q", ""),
        request.args.get("account", ""),
        request.args.get("sort", "name"),
        request.args.get("order", "asc"),
        limit,
        start,
    )
    if etag_matches(request.headers.get("If-None-Match"), etag):
        return _not_modified(etag)
    response = jsonify(
        {
            "items": [entry.to_dict() for entry in page],
            "total": total,
            "limit": limit,
            "next_cursor": next_cursor,
            "revision": revision,
        }
    )
    response.headers["ETag"] = _quoted(etag)
    return response


@bp.get("/api/files/<path:file_name>")
def file_detail(file_name: str) -> Response:
    rt = runtime()
    entry = rt.pool.get_file(file_name)
    if entry is None:
        raise Problem("file_not_found", f"File {file_name!r} is not in the pool.", status=404)
    etag = make_etag("file", entry.file_id, entry.upload_timestamp)
    if etag_matches(request.headers.get("If-None-Match"), etag):
        return _not_modified(etag)
    response = jsonify(entry.to_dict())
    response.headers["ETag"] = _quoted(etag)
    return response


@bp.route("/api/files/<path:file_name>/content", methods=["GET", "HEAD"])
@bp.route("/api/download/<path:file_name>", methods=["GET", "HEAD"])
def file_content(file_name: str) -> Response:
    return content_response(runtime(), file_name, request)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def _spool_and_submit(
    rt: Runtime,
    storage: Any,
    *,
    file_name: str,
    operation: str,
    expected_file_id: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    spooled: SpooledUpload | None = None
    try:
        spooled = spool_upload(
            storage,
            file_name=file_name,
            settings_max_bytes=rt.settings.max_upload_bytes,
            temp_dir=rt.settings.temp_dir,
        )
        job = rt.manager.submit(
            file_name=file_name,
            temp_path=spooled.path,
            file_size=spooled.size,
            content_type=spooled.content_type,
            operation=operation,
            request_id=request_id_var.get(),
            client=getattr(g, "client_label", None),
            expected_file_id=expected_file_id,
        )
        return job.snapshot(), None
    except UploadRejected as exc:
        if spooled is not None:
            _unlink(spooled.path)
        return None, _error_doc(exc.code, exc.message, **exc.details)
    except Problem as exc:
        if spooled is not None:
            _unlink(spooled.path)
        return None, _error_doc(exc.code, str(exc), **(exc.extensions or {}))
    except Exception:
        if spooled is not None:
            _unlink(spooled.path)
        raise


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _rejections_problem(rejected: list[dict[str, Any]]) -> Problem:
    codes = {item.get("error", {}).get("code") for item in rejected}
    code = codes.pop() if len(codes) == 1 else None
    return Problem(
        code or "bad_request",
        "No files were accepted for upload.",
        extensions={"errors": rejected},
    )


@bp.post("/api/files")
def upload_files() -> Response:
    rt = runtime()
    parts = request.files.getlist("file")
    if not parts:
        raise Problem(
            "validation_error",
            "Attach at least one file using the multipart field 'file'.",
        )
    if len(parts) > rt.settings.max_files_per_request:
        raise Problem(
            "validation_error",
            f"At most {rt.settings.max_files_per_request} files may be uploaded per request.",
        )

    jobs: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in parts:
        name = part.filename or ""
        try:
            validate_file_name(name, max_bytes=rt.settings.max_filename_bytes)
        except Problem as exc:
            rejected.append({"file_name": name, "error": _error_doc(exc.code, str(exc))})
            continue
        if name in seen:
            rejected.append(
                {
                    "file_name": name,
                    "error": _error_doc(
                        "duplicate_in_request", "The same file name appears more than once."
                    ),
                }
            )
            continue
        seen.add(name)
        job, error = _spool_and_submit(rt, part, file_name=name, operation="upload")
        if job is not None:
            jobs.append(job)
        if error is not None:
            rejected.append({"file_name": name, "error": error})

    if not jobs:
        raise _rejections_problem(rejected)

    metrics.refresh_pool(rt.pool)
    response = jsonify({"jobs": jobs, "rejected": rejected})
    response.status_code = 202
    if len(jobs) == 1:
        response.headers["Location"] = f"/api/jobs/{jobs[0]['id']}"
    return response


@bp.put("/api/files/<path:file_name>")
def upsert_file(file_name: str) -> Response:
    rt = runtime()
    validate_file_name(file_name, max_bytes=rt.settings.max_filename_bytes)
    entry = rt.pool.get_file(file_name)
    expected = parse_etag(request.headers.get("If-Match"))
    if expected is not None:
        if expected == "*":
            if entry is None:
                raise Problem("precondition_failed", "The file does not exist.", status=412)
        elif entry is None or expected != entry.file_id:
            raise Problem(
                "precondition_failed",
                "The file changed since it was read; refresh and retry.",
                status=412,
                extensions={
                    "expected_file_id": expected,
                    "actual_file_id": entry.file_id if entry else None,
                },
            )
    parts = request.files.getlist("file")
    if len(parts) != 1:
        raise validation_problem("Attach exactly one file using the multipart field 'file'.")
    expected_file_id = entry.file_id if expected is not None and entry is not None else None
    job, error = _spool_and_submit(
        rt,
        parts[0],
        file_name=file_name,
        operation="upsert",
        expected_file_id=expected_file_id,
    )
    if job is None:
        raise Problem(
            error["code"] if error else "bad_request",
            error["message"] if error else "The file could not be queued.",
            extensions=error.get("details") if error else None,
        )
    metrics.refresh_pool(rt.pool)
    response = jsonify(job)
    response.status_code = 202
    response.headers["Location"] = f"/api/jobs/{job['id']}"
    return response


@bp.delete("/api/files/<path:file_name>")
def delete_file(file_name: str) -> Response:
    rt = runtime()
    validate_file_name(file_name, max_bytes=rt.settings.max_filename_bytes)
    delete_file_entry(
        rt,
        file_name,
        expected_file_id=parse_etag(request.headers.get("If-Match")),
        request_id=request_id_var.get(),
        client=getattr(g, "client_label", None),
    )
    return Response(status=204)


# ---------------------------------------------------------------------------
# Routing preview
# ---------------------------------------------------------------------------
@bp.post("/api/routing-plans")
def routing_plan() -> Response:
    rt = runtime()
    body = json_body(request)
    files = body.get("files")
    if not isinstance(files, list):
        raise validation_problem("'files' must be a list of {name, size} objects.")
    if len(files) > 100:
        raise validation_problem("At most 100 files may be planned per request.")
    operation = body.get("operation", "upload")
    if operation not in {"upload", "upsert"}:
        raise validation_problem("'operation' must be 'upload' or 'upsert'.")

    descriptors: list[tuple[str, int]] = []
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        pointer = f"/files/{index}"
        if not isinstance(item, dict):
            errors.append({"pointer": pointer, "detail": "Each file must be an object."})
            continue
        try:
            name = validate_file_name(item.get("name"), max_bytes=rt.settings.max_filename_bytes)
            size = validate_size(item.get("size"), max_bytes=rt.settings.max_upload_bytes)
            descriptors.append((name, size))
        except Problem as exc:
            errors.append({"pointer": pointer, "code": exc.code, "detail": str(exc)})
    if errors:
        raise validation_problem("The routing request is invalid.", errors)

    allow_existing = [name for name, _ in descriptors] if operation == "upsert" else []
    plan = rt.pool.plan(descriptors, allow_existing=allow_existing)
    payload = plan.to_dict()
    payload["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _job_state_filter() -> set[JobState] | None:
    raw = request.args.get("state")
    active = request.args.get("active")
    if active and active.lower() in {"1", "true", "yes"}:
        return {
            JobState.QUEUED,
            JobState.UPLOADING,
            JobState.REPLACING,
        }
    if not raw:
        return None
    states: set[JobState] = set()
    for value in raw.split(","):
        value = value.strip()
        try:
            states.add(JobState(value))
        except ValueError as exc:
            raise validation_problem(
                f"Unknown job state {value!r}. Valid states: "
                + ", ".join(state.value for state in JobState)
            ) from exc
    return states


@bp.get("/api/jobs")
def list_jobs() -> Response:
    rt = runtime()
    states = _job_state_filter()
    file_name = str_arg(request, "file_name", max_length=1000) or None
    updated_since = int_arg(request, "updated_since", default=0, minimum=0)
    limit = int_arg(request, "limit", default=100, minimum=1, maximum=500)
    offset = int_arg(request, "offset", default=0, minimum=0)
    jobs, total = rt.manager.list(
        states=states,
        file_name=file_name,
        updated_since=updated_since,
        limit=limit,
        offset=offset,
    )
    revision = rt.manager.revision
    etag = make_etag(
        "jobs",
        revision,
        request.args.get("state", ""),
        request.args.get("active", ""),
        request.args.get("file_name", ""),
        updated_since,
        limit,
        offset,
    )
    if etag_matches(request.headers.get("If-None-Match"), etag):
        return _not_modified(etag)
    response = jsonify(
        {
            "items": [job.snapshot() for job in jobs],
            "total": total,
            "active": rt.manager.active_count(),
            "queued": rt.manager.queued_count(),
            "revision": revision,
        }
    )
    response.headers["ETag"] = _quoted(etag)
    return response


@bp.get("/api/jobs/<job_id>")
def job_detail(job_id: str) -> Response:
    job = runtime().manager.get(job_id)
    if job is None:
        raise Problem("job_not_found", f"Upload job {job_id!r} was not found.", status=404)
    return jsonify(job.snapshot())


@bp.delete("/api/jobs/<job_id>")
def job_delete(job_id: str) -> Response:
    rt = runtime()
    job = rt.manager.get(job_id)
    if job is None:
        raise Problem("job_not_found", f"Upload job {job_id!r} was not found.", status=404)
    try:
        if job.state in TERMINAL_STATES:
            rt.manager.dismiss(job_id)
            return Response(status=204)
        cancelled = rt.manager.cancel(job_id)
    except UploadRejected as exc:
        raise Problem(exc.code, exc.message, extensions=exc.details) from exc
    return jsonify(cancelled.snapshot())


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
@bp.get("/api/audit")
def audit_list() -> Response:
    rt = runtime()
    limit = int_arg(request, "limit", default=50, minimum=1, maximum=500)
    offset = int_arg(request, "offset", default=0, minimum=0)
    entries, total = rt.audit.list(limit=limit, offset=offset)
    return jsonify(
        {
            "items": [entry.to_dict() for entry in entries],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )
