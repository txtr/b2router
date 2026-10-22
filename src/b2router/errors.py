"""RFC 9457 problem-details error handling.

Every API failure is expressed as ``application/problem+json`` with a stable
``code`` extension, the request id, and an optional list of field errors.
Flask's default HTML error pages are never allowed to leak from the API.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, Response, render_template, request
from werkzeug.exceptions import HTTPException

from b2router.logging_setup import request_id_var

logger = logging.getLogger("b2router.errors")

PROBLEMS_DOC = "https://github.com/txtr/b2router/blob/main/docs/problems.md"

# code -> (status, title)
PROBLEM_CATALOG: dict[str, tuple[int, str]] = {
    "bad_request": (400, "Malformed request"),
    "validation_error": (400, "Validation failed"),
    "unauthorized": (401, "Authentication required"),
    "forbidden": (403, "Forbidden"),
    "not_found": (404, "Resource not found"),
    "file_not_found": (404, "File not found"),
    "account_not_found": (404, "Account not found"),
    "job_not_found": (404, "Upload job not found"),
    "method_not_allowed": (405, "Method not allowed"),
    "conflict": (409, "Conflict"),
    "duplicate_filename": (409, "Duplicate file name"),
    "duplicate_in_request": (409, "Duplicate file name in request"),
    "job_active": (409, "Operation already in progress"),
    "file_changed": (409, "File changed while operating on it"),
    "state_changed": (409, "Pool state changed"),
    "no_account_can_fit": (409, "No account can fit this file"),
    "precondition_failed": (412, "Precondition failed"),
    "payload_too_large": (413, "Payload too large"),
    "unsupported_media_type": (415, "Unsupported media type"),
    "range_not_satisfiable": (416, "Range not satisfiable"),
    "queue_full": (429, "Upload queue is full"),
    "too_many_requests": (429, "Too many requests"),
    "internal_error": (500, "Internal server error"),
    "b2_error": (502, "Storage backend error"),
    "b2_unauthorized": (502, "Storage backend authorization failed"),
    "b2_not_found": (502, "Object missing from storage backend"),
    "b2_rate_limited": (503, "Storage backend rate limited the request"),
    "b2_unavailable": (503, "Storage backend unavailable"),
    "b2_timeout": (504, "Storage backend timed out"),
    "not_ready": (503, "Service is not ready"),
    "service_unavailable": (503, "Service unavailable"),
}

HTTP_CODE_MAP: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    412: "precondition_failed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    416: "range_not_satisfiable",
    429: "too_many_requests",
    500: "internal_error",
    503: "service_unavailable",
}


class Problem(Exception):
    """An error that renders as an RFC 9457 problem document."""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        status: int | None = None,
        title: str | None = None,
        extensions: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        instance: str | None = None,
    ) -> None:
        known_status, known_title = PROBLEM_CATALOG.get(code, (500, "Internal server error"))
        self.code = code
        self.status = status or known_status
        self.title = title or known_title
        self.detail = detail
        self.extensions = extensions or {}
        self.headers = headers or {}
        self.instance = instance
        super().__init__(detail or self.title)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": f"{PROBLEMS_DOC}#{self.code}",
            "title": self.title,
            "status": self.status,
        }
        if self.detail:
            body["detail"] = self.detail
        if self.instance:
            body["instance"] = self.instance
        body["code"] = self.code
        body["request_id"] = request_id_var.get() or "-"
        body.update(self.extensions)
        return body

    def response(self) -> Response:
        body = self.to_dict()
        headers = {"Content-Type": "application/problem+json", **self.headers}
        return Response(_json(body), status=self.status, headers=headers)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def current_request_id() -> str:
    try:
        return request_id_var.get() or "-"
    except LookupError:  # pragma: no cover - outside app context
        return "-"


def problem_response(problem: Problem) -> Response:
    return problem.response()


def _http_problem(exc: HTTPException) -> Problem:
    status = exc.code or 500
    code = HTTP_CODE_MAP.get(status, "internal_error" if status >= 500 else "bad_request")
    friendly = {
        400: "The request could not be understood.",
        401: "Authentication is required for this resource.",
        403: "You are not allowed to access this resource.",
        404: "The requested resource does not exist.",
        405: "This method is not allowed for the requested resource.",
        409: "The request conflicts with the current state.",
        412: "A precondition in the request was not met.",
        413: "The request body is too large.",
        415: "The request has an unsupported content type.",
        416: "The requested range cannot be satisfied.",
        429: "Too many requests.",
    }
    detail = friendly.get(status) if status < 500 else None
    headers: dict[str, str] = {}
    if status == 405:
        allow = exc.get_response().headers.get("Allow")
        if allow:
            headers["Allow"] = allow
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="b2router"'
    return Problem(code, detail, status=status, headers=headers)


def _ui_problem_response(exc: Problem) -> Response | None:
    """Render HTML instead of JSON for browser-facing failures.

    HTMX requests receive a small alert fragment so a failed form submission
    does not replace the page with a full document. Normal navigations receive
    the shared error page.
    """
    if not request.path.startswith("/ui"):
        return None
    context = {
        "problem": exc,
        "title": exc.title,
        "detail": exc.detail,
        "code": exc.code,
        "request_id": request_id_var.get(),
    }
    if request.headers.get("HX-Request") == "true":
        headers = {
            "HX-Retarget": "#flash",
            "HX-Reswap": "innerHTML",
            **exc.headers,
        }
        return Response(
            render_template("ui/fragments/problem.html", **context),
            status=exc.status,
            headers=headers,
        )
    return Response(
        render_template("ui/error.html", **context),
        status=exc.status,
        headers=exc.headers,
    )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(Problem)
    def _handle_problem(exc: Problem) -> Response:
        if exc.status >= 500:
            logger.error(
                "request.failed",
                extra={"data": {"code": exc.code, "status": exc.status, "detail": exc.detail}},
            )
        rendered = _ui_problem_response(exc)
        return rendered if rendered is not None else problem_response(exc)

    @app.errorhandler(HTTPException)
    def _handle_http(exc: HTTPException) -> Response:
        problem = _http_problem(exc)
        rendered = _ui_problem_response(problem)
        return rendered if rendered is not None else problem_response(problem)

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            problem = _http_problem(exc)
        else:
            logger.exception(
                "request.crashed",
                extra={"data": {"error": type(exc).__name__}},
            )
            problem = Problem(
                "internal_error", "An unexpected error occurred. Check the server logs."
            )
        rendered = _ui_problem_response(problem)
        return rendered if rendered is not None else problem_response(problem)


def validation_problem(detail: str, errors: list[dict[str, Any]] | None = None) -> Problem:
    return Problem(
        "validation_error",
        detail,
        extensions={"errors": errors} if errors else None,
    )


def bad_request(detail: str) -> Problem:
    return Problem("bad_request", detail)
