"""Flask application factory and cross-cutting HTTP concerns.

The API blueprint owns resources; this module owns identity (request/client
ids), authentication, rate limiting, CORS, metrics, logging and problem
responses. It is also the only place that knows about the runtime container.
"""

from __future__ import annotations

import hmac
import logging
import math
import re
import secrets
import time
import uuid

from flask import Flask, Response, g, request

from b2router import metrics
from b2router.api.routes import bp
from b2router.api.validation import ensure_upload_space
from b2router.errors import Problem, register_error_handlers
from b2router.logging_setup import (
    client_label_var,
    configure_logging,
    job_id_var,
    log_event,
    request_id_var,
)
from b2router.ratelimit import RateLimiter
from b2router.runtime import Runtime
from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool
from b2router.services.uploads import UploadManager
from b2router.settings import Settings

logger = logging.getLogger("b2router.web")

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9._ @-]{1,64}$")

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, HEAD, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": (
        "Authorization, Content-Type, If-Match, If-None-Match, "
        "Idempotency-Key, X-Client-Name, X-Request-ID"
    ),
    "Access-Control-Expose-Headers": (
        "ETag, Location, Retry-After, X-Request-ID, X-B2Router-Revision"
    ),
    "Access-Control-Max-Age": "600",
}


def _client_address(settings: Settings) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or "unknown"
    return request.remote_addr or "unknown"


def _client_label(settings: Settings) -> str:
    explicit = request.headers.get("X-Client-Name", "").strip()
    if explicit and CLIENT_RE.match(explicit):
        return explicit
    if settings.api_token:
        return "operator"
    return _client_address(settings)


def _allowed_origin(settings: Settings) -> str | None:
    origin = request.headers.get("Origin")
    if origin and origin in settings.cors_origins:
        return origin
    return None


def create_app(
    settings: Settings | None = None,
    *,
    pool: AccountPool | None = None,
    manager: UploadManager | None = None,
    audit: AuditLog | None = None,
    configure_logs: bool = True,
) -> Flask:
    settings = settings or Settings.from_env()
    if configure_logs:
        configure_logging(settings.log_level, settings.log_format)

    if pool is None:
        pool = manager.pool if manager is not None else AccountPool()
    if audit is None:
        audit = (
            manager.audit
            if manager is not None
            else AuditLog(maxlen=max(settings.job_history_max, 1000))
        )
    if manager is None:
        manager = UploadManager(pool, audit, settings)
    elif manager.pool is not pool or manager.audit is not audit:
        raise ValueError(
            "create_app received a manager whose pool/audit differs from the runtime dependencies"
        )
    runtime = Runtime(settings=settings, pool=pool, manager=manager, audit=audit)

    app = Flask(__name__)
    app.extensions["b2router"] = runtime
    app.config["MAX_CONTENT_LENGTH"] = (
        settings.max_upload_bytes * settings.max_files_per_request + 1024 * 1024
    )
    app.config["SETTINGS"] = settings
    app.secret_key = settings.secret_key or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
    )
    app.json.sort_keys = False  # type: ignore[attr-defined]

    app.register_blueprint(bp)
    register_error_handlers(app)

    limiter = RateLimiter(settings.mutation_rate_per_minute)
    app.extensions["b2router_limiter"] = limiter
    app.extensions["b2router_metrics_enabled"] = settings.metrics_enabled

    @app.before_request
    def _before() -> Response | None:
        request_id = request.headers.get("X-Request-ID", "")
        if not REQUEST_ID_RE.match(request_id):
            request_id = f"req_{uuid.uuid4().hex[:16]}"
        g.request_id = request_id
        request_id_var.set(request_id)
        job_id_var.set(None)

        client = _client_label(settings)
        g.client_label = client
        g.client_address = _client_address(settings)
        client_label_var.set(client)
        g.request_started = time.monotonic()

        origin = _allowed_origin(settings)
        if request.method == "OPTIONS" and origin:
            response = Response(status=204)
            response.headers.update(_CORS_HEADERS)
            response.headers["Access-Control-Allow-Origin"] = origin
            return response

        mutating_api = request.method in {"POST", "DELETE"} and request.path.startswith("/api/")
        if mutating_api:
            # Limit before authentication so failed bearer guesses cannot
            # bypass the mutation budget. Key by network identity, never by the
            # self-declared X-Client-Name header.
            allowed, retry_after = limiter.allow(_client_address(settings))
            if not allowed:
                raise Problem(
                    "too_many_requests",
                    "Too many mutations; slow down and retry.",
                    headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
                    extensions={"retry_after_seconds": round(retry_after, 3)},
                )

        protected = request.path.startswith("/api/") or request.path == "/metrics"
        if settings.api_token and protected and request.method != "OPTIONS":
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {settings.api_token}"
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                raise Problem(
                    "unauthorized",
                    "A valid bearer token is required.",
                    headers={"WWW-Authenticate": 'Bearer realm="b2router"'},
                )

        upload_request = request.method == "POST" and request.path == "/api/files"
        if upload_request:
            if manager.queued_count() >= settings.queue_max:
                # Reject before Flask parses/spools the multipart body. This keeps a
                # saturated queue from consuming the full aggregate body allowance.
                raise Problem(
                    "queue_full",
                    "The upload queue is full; retry once active uploads finish.",
                    headers={"Retry-After": "5"},
                    extensions={"queue_max": settings.queue_max},
                )
            ensure_upload_space(settings.temp_dir, request.content_length)
        return None

    @app.after_request
    def _after(response: Response) -> Response:
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        response.headers["X-B2Router-Revision"] = str(runtime.pool.revision)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
            "font-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-eval'; connect-src 'self'"
        )
        if request.path.startswith("/api/") or request.path in {"/health", "/ready"}:
            response.headers.setdefault("Cache-Control", "no-store")
        origin = _allowed_origin(settings)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            for key, value in _CORS_HEADERS.items():
                response.headers.setdefault(key, value)

        started = getattr(g, "request_started", time.monotonic())
        duration = time.monotonic() - started
        route = request.url_rule.rule if request.url_rule is not None else "unmatched"
        status = response.status_code
        metrics.record_request(request.method, route, status, duration)
        noisy = request.path in {"/health", "/ready", "/metrics"}
        log_event(
            "http.request",
            level=logging.DEBUG if noisy else (logging.WARNING if status >= 500 else logging.INFO),
            method=request.method,
            path=request.path,
            route=route,
            status=status,
            duration_ms=round(duration * 1000, 2),
            bytes=response.content_length if response.content_length is not None else -1,
            client=getattr(g, "client_label", None),
        )
        return response

    @app.teardown_request
    def _teardown(_exc: BaseException | None) -> None:
        request_id_var.set(None)
        client_label_var.set(None)
        job_id_var.set(None)

    return app
