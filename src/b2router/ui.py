"""Server-rendered operator console under ``/ui``.

The console is a thin, same-origin client of the in-process runtime. It never
invents a second source of truth: reads come from the pool/manager/audit
objects, mutations call the same services as the JSON API, and downloads use the
same streaming code. HTMX supplies fragment navigation and live polling.
"""

from __future__ import annotations

import contextlib
import hmac
import math
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from b2router import metrics
from b2router.api.validation import spool_upload, validate_file_name
from b2router.errors import Problem
from b2router.logging_setup import log_event, request_id_var
from b2router.ratelimit import RateLimiter
from b2router.runtime import Runtime
from b2router.services.content import content_response
from b2router.services.files import delete_file as delete_file_entry
from b2router.services.uploads import ACTIVE_STATES, JobState, UploadRejected
from b2router.utils.formatters import format_pool_stats

ui_bp = Blueprint("ui", __name__, url_prefix="/ui")
_login_limiter = RateLimiter(12, burst=12)

FILE_SORTS = {"name", "size", "uploaded"}
ORDERS = {"asc", "desc"}
AUDIT_ACTIONS = {"upsert", "delete"}
AUDIT_RESULTS = {"success", "partial", "failed"}
EMPTY_JOB_FRAGMENT = '<div class="job-missing"></div>'


def _runtime() -> Runtime:
    runtime: Runtime = current_app.extensions["b2router"]
    return runtime


def _settings() -> Any:
    return _runtime().settings


def _csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def _csrf_valid() -> bool:
    supplied = request.form.get("_csrf", "").encode("utf-8")
    expected = session.get("_csrf", "").encode("utf-8")
    return bool(expected) and hmac.compare_digest(supplied, expected)


def _safe_next(candidate: str | None) -> str:
    if not candidate:
        return url_for("ui.dashboard")
    parsed = urlparse(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or not (candidate == "/ui" or candidate.startswith("/ui/"))
        or parsed.path in {"/ui/login", "/ui/logout"}
        or parsed.path.startswith("/ui/fragments/")
    ):
        return url_for("ui.dashboard")
    return candidate


def _clamp_int(raw: str | None, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _choice(raw: str | None, choices: set[str], default: str) -> str:
    return raw if raw in choices else default


def _file_view() -> dict[str, Any]:
    rt = _runtime()
    q = (request.values.get("q") or "").strip()[:200]
    account = (request.values.get("account") or "").strip()
    sort = _choice(request.values.get("sort"), FILE_SORTS, "name")
    order = _choice(request.values.get("order"), ORDERS, "asc")
    limit = _clamp_int(request.values.get("limit"), 25, minimum=5, maximum=200)
    page = _clamp_int(request.values.get("page"), 1, minimum=1, maximum=100_000)

    files = rt.pool.list_files()
    if q:
        needle = q.casefold()
        files = [entry for entry in files if needle in entry.file_name.casefold()]
    if account:
        files = [entry for entry in files if account in entry.accounts]

    key = {
        "name": lambda entry: entry.file_name.casefold(),
        "size": lambda entry: entry.size,
        "uploaded": lambda entry: entry.upload_timestamp,
    }[sort]
    files.sort(key=key, reverse=order == "desc")

    total = len(files)
    pages = max(1, math.ceil(total / limit)) if total else 1
    page = min(page, pages)
    start = (page - 1) * limit
    return {
        "q": q,
        "account": account,
        "sort": sort,
        "order": order,
        "limit": limit,
        "page": page,
        "pages": pages,
        "total": total,
        "files": files[start : start + limit],
    }


def _files_context() -> dict[str, Any]:
    rt = _runtime()
    view = _file_view()
    return {
        "view": view,
        "files": view["files"],
        "show_actions": True,
        "account_names": [state.name for state in rt.pool.account_states()],
    }


def _job_context() -> dict[str, Any]:
    rt = _runtime()
    active_filter = request.values.get("active", "") in {"1", "true", "yes"}
    states = set(ACTIVE_STATES) | {JobState.QUEUED} if active_filter else None
    jobs, total = rt.manager.list(states=states, limit=100, newest_first=True)
    return {
        "jobs": [job.snapshot() for job in jobs],
        "total_jobs": total,
        "active_jobs": rt.manager.active_count(),
        "queued_jobs": rt.manager.queued_count(),
        "active_filter": active_filter,
        "jobs_url": url_for("ui.fragments_jobs", active=1 if active_filter else 0),
    }


def _audit_context() -> dict[str, Any]:
    rt = _runtime()
    action = _choice(request.values.get("action"), AUDIT_ACTIONS, "")
    result = _choice(request.values.get("result"), AUDIT_RESULTS, "")
    page = _clamp_int(request.values.get("page"), 1, minimum=1, maximum=10_000)
    entries, _ = rt.audit.list(limit=10**9)
    if action:
        entries = [entry for entry in entries if entry.action == action]
    if result:
        entries = [entry for entry in entries if entry.result == result]
    total = len(entries)
    size = 50
    pages = max(1, math.ceil(total / size)) if total else 1
    page = min(page, pages)
    start = (page - 1) * size
    return {
        "audit_entries": [entry.to_dict() for entry in entries[start : start + size]],
        "audit_total": total,
        "audit_action": action,
        "audit_result": result,
        "audit_page": page,
        "audit_pages": pages,
    }


def _stats_context() -> dict[str, Any]:
    rt = _runtime()
    stats = format_pool_stats(rt.pool.stats())
    jobs, total_jobs = rt.manager.list(limit=1)
    return {
        "stats": stats,
        "active_jobs": rt.manager.active_count(),
        "queued_jobs": rt.manager.queued_count(),
        "total_jobs": total_jobs,
    }


@ui_bp.context_processor
def _inject_shell() -> dict[str, Any]:
    settings = _settings()
    endpoint = request.endpoint or ""
    active = endpoint.rsplit(".", 1)[-1]
    if active not in {"dashboard", "files", "uploads", "accounts", "audit"}:
        active = ""
    return {
        "active": active,
        "ready": len(_runtime().pool) > 0,
        "api_token_enabled": bool(settings.api_token),
        "authenticated": bool(session.get("authenticated")),
        "csrf_token": _csrf_token(),
        "request_id": request_id_var.get(),
        "version": settings.version,
        "max_upload_bytes": settings.max_upload_bytes,
        "max_files_per_request": settings.max_files_per_request,
    }


@ui_bp.before_request
def _guard() -> Any:
    settings = _settings()
    if request.endpoint in {"ui.login", "ui.login_submit"}:
        if request.method == "POST" and not _csrf_valid():
            raise Problem("forbidden", "The sign-in form expired; reload and try again.")
        return None

    if settings.api_token and not session.get("authenticated"):
        target = request.full_path if request.query_string else request.path
        login_url = url_for("ui.login", next=_safe_next(target))
        if request.headers.get("HX-Request") == "true":
            return Response(status=401, headers={"HX-Redirect": login_url})
        return redirect(login_url)

    if request.method == "POST" and request.path == "/ui/upload":
        rt = _runtime()
        if rt.manager.queued_count() >= rt.settings.queue_max:
            # Check before _csrf_valid() reads request.form, which would parse
            # and spool the entire multipart body first.
            raise Problem(
                "queue_full",
                "The upload queue is full; retry once active uploads finish.",
                headers={"Retry-After": "5"},
                extensions={"queue_max": rt.settings.queue_max},
            )

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_valid():
        raise Problem("forbidden", "Invalid or missing CSRF token. Reload the page and retry.")
    return None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@ui_bp.get("/")
def dashboard() -> str:
    rt = _runtime()
    files = sorted(rt.pool.list_files(), key=lambda entry: entry.upload_timestamp, reverse=True)
    recent_jobs, _ = rt.manager.list(limit=6, newest_first=True)
    view = {
        "q": "",
        "account": "",
        "sort": "uploaded",
        "order": "desc",
        "limit": 6,
        "page": 1,
        "pages": 1,
        "total": len(files),
    }
    return render_template(
        "ui/dashboard.html",
        **_stats_context(),
        files=files[:6],
        view=view,
        show_actions=False,
        recent_jobs=[job.snapshot() for job in recent_jobs],
    )


@ui_bp.get("/files")
def files() -> str:
    return render_template("ui/files.html", **_files_context())


@ui_bp.get("/uploads")
def uploads() -> str:
    return render_template("ui/uploads.html", **_job_context())


@ui_bp.get("/accounts")
def accounts() -> str:
    return render_template("ui/accounts.html", accounts=_runtime().pool.account_states())


@ui_bp.get("/audit")
def audit() -> str:
    return render_template("ui/audit.html", **_audit_context())


@ui_bp.get("/login")
def login() -> Any:
    settings = _settings()
    if not settings.api_token:
        return redirect(url_for("ui.dashboard"))
    next_url = _safe_next(request.values.get("next"))
    if session.get("authenticated"):
        return redirect(next_url)
    return render_template(
        "ui/login.html",
        error=None,
        next_url=next_url,
        csrf_token=_csrf_token(),
        version=settings.version,
    )


@ui_bp.post("/login")
def login_submit() -> Any:
    settings = _settings()
    next_url = _safe_next(request.form.get("next"))
    if not settings.api_token:
        return redirect(url_for("ui.dashboard"))

    allowed, retry_after = _login_limiter.allow(f"login:{request.remote_addr or 'unknown'}")
    if not allowed:
        time.sleep(min(retry_after, 1.0))
        return (
            render_template(
                "ui/login.html",
                error="Too many sign-in attempts. Wait a minute and try again.",
                next_url=next_url,
                csrf_token=_csrf_token(),
                version=settings.version,
            ),
            429,
        )

    supplied = request.form.get("token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), settings.api_token.encode("utf-8")):
        log_event("ui.login_failed", level=30, client=getattr(g, "client_label", None))
        time.sleep(0.15)
        return (
            render_template(
                "ui/login.html",
                error="That token is not valid.",
                next_url=next_url,
                csrf_token=_csrf_token(),
                version=settings.version,
            ),
            401,
        )

    session.clear()
    session["authenticated"] = True
    log_event("ui.login_succeeded", client=getattr(g, "client_label", None))
    return redirect(next_url)


@ui_bp.post("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("ui.login"))


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------
@ui_bp.get("/fragments/stats")
def fragments_stats() -> str:
    return render_template("ui/fragments/stats.html", **_stats_context())


@ui_bp.get("/fragments/files")
def fragments_files() -> str:
    return render_template("ui/fragments/files_panel.html", **_files_context())


@ui_bp.get("/fragments/jobs")
def fragments_jobs() -> str:
    return render_template("ui/fragments/jobs_list.html", **_job_context())


@ui_bp.get("/fragments/job/<job_id>")
def fragment_job(job_id: str) -> Any:
    rt = _runtime()
    job = rt.manager.get(job_id)
    if job is None:
        return Response(status=200, response=EMPTY_JOB_FRAGMENT)
    payload = job.snapshot()
    response = Response(render_template("ui/fragments/job_card.html", job=payload))
    if payload["state"] in {"complete", "partial", "failed", "cancelled"}:
        response.headers["HX-Trigger"] = "jobs-changed"
    return response


@ui_bp.get("/fragments/accounts")
def fragments_accounts() -> str:
    return render_template(
        "ui/fragments/accounts_cards.html", accounts=_runtime().pool.account_states()
    )


@ui_bp.get("/fragments/audit")
def fragments_audit() -> str:
    return render_template("ui/fragments/audit_panel.html", **_audit_context())


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def _queue_upload(
    rt: Runtime,
    storage: Any,
    *,
    file_name: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    spooled = None
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
            request_id=request_id_var.get(),
            client=getattr(g, "client_label", None),
        )
        return job.snapshot(), None
    except UploadRejected as exc:
        if spooled is not None:
            _unlink(spooled.path)
        return None, {"code": exc.code, "message": exc.message, "details": exc.details}
    except Problem as exc:
        if spooled is not None:
            _unlink(spooled.path)
        return None, {"code": exc.code, "message": str(exc), "details": exc.extensions}
    except Exception:
        if spooled is not None:
            _unlink(spooled.path)
        raise


def _unlink(path: str) -> None:
    import os

    with contextlib.suppress(OSError):
        os.unlink(path)


@ui_bp.post("/upload")
def upload() -> Any:
    rt = _runtime()
    jobs: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    parts = request.files.getlist("file")

    if not parts:
        rejected.append(
            {
                "file_name": "(request)",
                "error": {"code": "validation_error", "message": "Attach at least one file."},
            }
        )
    elif len(parts) > rt.settings.max_files_per_request:
        rejected.append(
            {
                "file_name": "(request)",
                "error": {
                    "code": "validation_error",
                    "message": (
                        f"At most {rt.settings.max_files_per_request} "
                        "files may be upserted at once."
                    ),
                },
            }
        )
    else:
        seen: set[str] = set()
        for part in parts:
            name = part.filename or ""
            try:
                validate_file_name(name, max_bytes=rt.settings.max_filename_bytes)
            except Problem as exc:
                rejected.append(
                    {"file_name": name, "error": {"code": exc.code, "message": str(exc)}}
                )
                continue
            if name in seen:
                rejected.append(
                    {
                        "file_name": name,
                        "error": {
                            "code": "duplicate_in_request",
                            "message": "The same file name appears more than once.",
                        },
                    }
                )
                continue
            seen.add(name)
            job, error = _queue_upload(rt, part, file_name=name)
            if job is not None:
                jobs.append(job)
            if error is not None:
                rejected.append({"file_name": name, "error": error})

    if jobs:
        metrics.refresh_pool(rt.pool)
    response = Response(
        render_template("ui/fragments/upload_result.html", jobs=jobs, rejected=rejected)
    )
    if jobs:
        response.headers["HX-Trigger"] = "jobs-changed"
    return response


@ui_bp.post("/files/delete")
def delete_file() -> Any:
    rt = _runtime()
    name = request.form.get("name", "")
    validate_file_name(name, max_bytes=rt.settings.max_filename_bytes)
    delete_file_entry(
        rt,
        name,
        expected_etag=request.form.get("etag") or None,
        request_id=request_id_var.get(),
        client=getattr(g, "client_label", None),
    )
    alert = {
        "level": "success",
        "icon": "bi-check2-circle",
        "message": f"Deleted {name}.",
    }

    view = _file_view()
    response = Response(
        render_template(
            "ui/fragments/files_panel.html",
            view=view,
            files=view["files"],
            show_actions=True,
            account_names=[state.name for state in rt.pool.account_states()],
            alert=alert,
        )
    )
    # The response already contains the refreshed panel. Emitting jobs-changed
    # here would immediately replace it and erase the success alert.
    return response


@ui_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str) -> Any:
    rt = _runtime()
    try:
        rt.manager.cancel(job_id)
    except UploadRejected as exc:
        raise Problem(exc.code, exc.message, extensions=exc.details) from exc
    job = rt.manager.get(job_id)
    if job is None:
        return Response(status=200, response=EMPTY_JOB_FRAGMENT)
    response = Response(render_template("ui/fragments/job_card.html", job=job.snapshot()))
    response.headers["HX-Trigger"] = "jobs-changed"
    return response


@ui_bp.post("/jobs/<job_id>/dismiss")
def dismiss_job(job_id: str) -> Any:
    rt = _runtime()
    with contextlib.suppress(UploadRejected):
        rt.manager.dismiss(job_id)
    response = Response(status=200, response=EMPTY_JOB_FRAGMENT)
    response.headers["HX-Trigger"] = "jobs-changed"
    return response


@ui_bp.get("/download/<path:file_name>")
def download(file_name: str) -> Any:
    return content_response(_runtime(), file_name, request)
