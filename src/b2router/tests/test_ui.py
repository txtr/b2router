"""The server-rendered console: auth, CSRF, fragments, and shared mutations."""

from __future__ import annotations

import io
import re
import time
from dataclasses import replace
from pathlib import Path

from b2router.services.pool import AccountPool
from b2router.services.uploads import UploadManager
from b2router.web import create_app

CSRF_RE = re.compile(rb'name="_csrf" value="([^"]+)"')
JOB_RE = re.compile(rb'data-job-ids="(job_[a-f0-9]+)')


def _csrf(client) -> str:
    response = client.get("/ui/uploads")
    assert response.status_code == 200
    match = CSRF_RE.search(response.data)
    assert match is not None
    return match.group(1).decode()


def _upload(client, csrf: str, name: str, data: bytes):
    response = client.post(
        "/ui/upload",
        data={"_csrf": csrf, "file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    return response


def _job_id(response) -> str:
    match = JOB_RE.search(response.data)
    assert match is not None
    return match.group(1).decode()


def _wait(client, job_id: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/ui/fragments/job/{job_id}")
        assert response.status_code == 200
        if b"job-complete" in response.data or b"job-failed" in response.data:
            return
        time.sleep(0.01)
    raise AssertionError(f"UI job {job_id} did not finish")


def _create(client, csrf: str, name: str, data: bytes) -> None:
    _wait(client, _job_id(_upload(client, csrf, name, data)))


def test_pages_and_vendored_assets(client) -> None:
    for path in (
        "/ui/",
        "/ui/files",
        "/ui/uploads",
        "/ui/accounts",
        "/ui/audit",
        "/static/app.css",
        "/static/app.js",
        "/static/vendor/htmx.min.js",
        "/static/vendor/bootstrap.min.css",
        "/static/vendor/bootstrap.bundle.min.js",
        "/static/vendor/bootstrap-icons.min.css",
        "/static/vendor/fonts/bootstrap-icons.woff2",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.data, path

    page = client.get("/ui/").data
    assert b"b2router" in page
    assert b"https://cdn" not in page
    assert b"unpkg" not in page


def test_dashboard_is_compact_and_filters_start_collapsed(client) -> None:
    page = client.get("/ui/")
    assert page.data.count(b'id="pool-stats"') == 1
    assert b"Quick upload" not in page.data
    assert b"Account pool" not in page.data
    assert b"Recent activity" not in page.data

    files_page = client.get("/ui/files")
    assert b'class="filters"' in files_page.data
    assert b'class="filters" open' not in files_page.data

    filtered = client.get("/ui/files", query_string={"sort": "size"})
    assert b'class="filters" open' in filtered.data

    audit_page = client.get("/ui/audit")
    assert b'name="limit"' not in audit_page.data


def test_search_and_empty_fragment(client) -> None:
    csrf = _csrf(client)
    _create(client, csrf, "reports/q3.pdf", b"pdf")
    _create(client, csrf, "images/logo.png", b"png")

    response = client.get("/ui/fragments/files", query_string={"q": "q3"})
    assert response.status_code == 200
    assert b"reports/q3.pdf" in response.data
    assert b"images/logo.png" not in response.data

    response = client.get("/ui/fragments/files", query_string={"q": "nothing"})
    assert response.status_code == 200
    assert b"No files match this view" in response.data


def test_upload_fragment_delete_and_download_proxy(client) -> None:
    csrf = _csrf(client)
    _create(client, csrf, "old.txt", b"old bytes")

    response = client.get("/ui/download/old.txt")
    assert response.status_code == 200
    assert response.data == b"old bytes"
    assert response.headers["Content-Disposition"].startswith("attachment;")

    response = _upload(client, csrf, "new.txt", b"new bytes")
    assert b"upsert queued" in response.data
    _wait(client, _job_id(response))

    response = client.get("/ui/download/new.txt")
    assert response.status_code == 200
    assert response.data == b"new bytes"

    response = client.post(
        "/ui/files/delete",
        data={"_csrf": csrf, "name": "new.txt"},
    )
    assert response.status_code == 200
    assert b"Deleted new.txt" in response.data
    assert client.get("/ui/download/new.txt").status_code == 404


def test_upsert_fragment_replaces_old_object_in_another_account(client, accounts) -> None:
    csrf = _csrf(client)
    _create(client, csrf, "report.txt", b"v1")

    response = _upload(client, csrf, "report.txt", b"v2")
    job_id = _job_id(response)
    _wait(client, job_id)

    response = client.get("/ui/download/report.txt")
    assert response.status_code == 200
    assert response.data == b"v2"
    assert len(accounts["a"].uploads) == 1
    assert len(accounts["a"].deleted) == 1
    assert len(accounts["b"].uploads) == 1
    card = client.get(f"/ui/fragments/job/{job_id}")
    assert b"Replaced" in card.data


def test_multi_file_upsert_accepts_own_filenames(client) -> None:
    csrf = _csrf(client)
    response = client.post(
        "/ui/upload",
        data={
            "_csrf": csrf,
            "file": [
                (io.BytesIO(b"one"), "one.txt"),
                (io.BytesIO(b"two"), "two.txt"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"2 upserts queued" in response.data
    assert b"target_name" not in client.get("/ui/uploads").data


def test_ui_404_is_html(client) -> None:
    response = client.get("/ui/not-a-real-page")
    assert response.status_code == 404
    assert response.content_type.startswith("text/html")
    assert b"Resource not found" in response.data


def test_token_login_and_csrf(settings, pool: AccountPool, manager: UploadManager) -> None:
    token_settings = replace(settings, api_token="s3cret", secret_key="test-secret")
    app = create_app(
        token_settings,
        pool=pool,
        manager=manager,
        audit=manager._audit,
        configure_logs=False,
    )
    app.testing = True
    client = app.test_client()

    response = client.get("/ui/")
    assert response.status_code == 302
    assert "/ui/login" in response.headers["Location"]

    response = client.get("/ui/login")
    assert response.status_code == 200
    match = CSRF_RE.search(response.data)
    assert match is not None
    csrf = match.group(1).decode()

    response = client.post("/ui/login", data={"_csrf": csrf, "token": "wrong"})
    assert response.status_code == 401
    assert b"not valid" in response.data

    response = client.post(
        "/ui/login",
        data={"_csrf": csrf, "token": "s3cret", "next": "/ui/fragments/files"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/ui/"

    response = client.get("/ui/")
    assert response.status_code == 200
    assert b"Overview" in response.data

    response = client.post(
        "/ui/upload",
        data={"file": (io.BytesIO(b"x"), "x.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    assert response.content_type.startswith("text/html")
    assert b"CSRF" in response.data


def test_unknown_job_fragments(client) -> None:
    csrf = _csrf(client)
    injection = '%22%3E%3Cimg%20src%3Dx%3E'
    response = client.get(f"/ui/fragments/job/{injection}")
    assert response.status_code == 200
    assert b"<img" not in response.data
    assert response.data == b'<div class="job-missing"></div>'

    response = client.post("/ui/jobs/job_missing/cancel", data={"_csrf": csrf})
    assert response.status_code == 404
    assert b"job_not_found" in response.data
    assert b"<img" not in response.data

    response = client.post("/ui/jobs/job_missing/dismiss", data={"_csrf": csrf})
    assert response.status_code == 200
    assert response.data == b'<div class="job-missing"></div>'


def test_htmx_problem_keeps_status_and_targets_flash(client) -> None:
    csrf = _csrf(client)

    response = client.post(
        "/ui/files/delete",
        data={"_csrf": csrf, "name": "missing.txt"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 404
    assert response.headers["HX-Retarget"] == "#flash"
    assert response.headers["HX-Reswap"] == "innerHTML"
    assert b"file_not_found" in response.data


def test_error_flash_does_not_interpolate_html() -> None:
    source = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")

    assert "flash.innerHTML" not in source
    assert "textContent = message" in source
    assert "flash.replaceChildren(alert)" in source


def test_ui_queue_full_is_checked_before_csrf_and_body_parsing(
    client, manager, settings, monkeypatch
) -> None:
    monkeypatch.setattr(manager, "queued_count", lambda: settings.queue_max)

    response = client.post(
        "/ui/upload",
        data=b"this is not valid multipart",
        content_type="multipart/form-data; boundary=broken",
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert b"queue_full" in response.data


def test_upload_result_does_not_duplicate_job_cards(client) -> None:
    csrf = _csrf(client)
    response = _upload(client, csrf, "once.txt", b"one")
    job_id = _job_id(response)

    assert b'id="job-' not in response.data
    jobs = client.get("/ui/fragments/jobs")
    assert jobs.data.count(f'id="job-{job_id}"'.encode()) == 1
    _wait(client, job_id)


def test_upsert_rejects_empty_filename_part(client) -> None:
    csrf = _csrf(client)
    _create(client, csrf, "keep.txt", b"old")

    response = client.post(
        "/ui/upload",
        data={"_csrf": csrf, "file": (io.BytesIO(b""), "")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"not queued" in response.data
    assert client.get("/ui/download/keep.txt").data == b"old"


def test_non_ascii_csrf_is_rejected_without_server_error(client) -> None:
    response = client.post(
        "/ui/files/delete",
        data={"_csrf": "\u00e9", "name": "anything.txt"},
    )

    assert response.status_code == 403
    assert response.content_type.startswith("text/html")


def test_audit_filter_fragment(client) -> None:
    csrf = _csrf(client)
    _create(client, csrf, "keep.txt", b"x")
    client.post("/ui/files/delete", data={"_csrf": csrf, "name": "keep.txt"})

    response = client.get("/ui/fragments/audit", query_string={"action": "delete"})
    assert response.status_code == 200
    assert b"Delete" in response.data
    assert b"keep.txt" in response.data

    response = client.get("/ui/fragments/audit", query_string={"result": "failed"})
    assert response.status_code == 200
    assert b"No mutations recorded" in response.data
