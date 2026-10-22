"""HTTP contract tests for the JSON API."""

from __future__ import annotations

import io
from dataclasses import replace

import pytest

from b2router.services.account import StorageError
from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool
from b2router.tests.conftest import wait_for_job
from b2router.web import create_app


def _upload(client, name: str, data: bytes, content_type: str = "text/plain"):
    return client.post(
        "/api/files",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )


def _upload_and_wait(client, name: str, data: bytes, content_type: str = "text/plain"):
    response = _upload(client, name, data, content_type)
    assert response.status_code == 202, response.get_json()
    job_id = response.get_json()["jobs"][0]["id"]
    return wait_for_job(client, job_id)


def test_health_ready_and_service_info(client) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.get_json()["status"] == "ready"

    info = client.get("/")
    assert info.status_code == 200
    assert info.get_json()["service"] == "b2router"


def test_ready_reports_draining(client, manager) -> None:
    manager.shutdown()

    response = client.get("/ready")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["code"] == "not_ready"
    assert payload["checks"]["accepting_mutations"] is False


def test_create_app_does_not_replace_an_empty_pool(settings, tmp_path) -> None:
    empty = AccountPool()
    app = create_app(
        replace(settings, temp_dir=str(tmp_path / "empty-tmp")),
        pool=empty,
        audit=AuditLog(),
        configure_logs=False,
    )
    app.testing = True

    assert app.extensions["b2router"].pool is empty
    assert not empty
    assert app.test_client().get("/ready").status_code == 503


def test_ready_does_not_depend_on_metrics(
    pool, manager, settings, tmp_path
) -> None:
    no_metrics = replace(
        settings,
        temp_dir=str(tmp_path / "no-metrics"),
        metrics_enabled=False,
    )
    app = create_app(
        no_metrics,
        pool=pool,
        manager=manager,
        audit=manager.audit,
        configure_logs=False,
    )
    app.testing = True
    client = app.test_client()

    response = client.get("/ready")

    assert response.status_code == 200
    assert "metrics_enabled" not in response.get_json()["checks"]


def test_metrics_endpoint(client) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert b"b2router_requests" in response.data


def test_stats_and_accounts_with_etags(client) -> None:
    stats = client.get("/api/stats")
    assert stats.status_code == 200
    body = stats.get_json()
    assert body["accounts"] == 2
    assert body["files"] == 0
    etag = stats.headers["ETag"]
    assert client.get("/api/stats", headers={"If-None-Match": etag}).status_code == 304

    accounts = client.get("/api/accounts")
    assert accounts.status_code == 200
    assert {item["name"] for item in accounts.get_json()["items"]} == {"a", "b"}


def test_upload_list_detail_and_content(client) -> None:
    job = _upload_and_wait(client, "hello.txt", b"hello world")
    assert job["state"] == "complete"

    listing = client.get("/api/files")
    assert listing.status_code == 200
    payload = listing.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["file_name"] == "hello.txt"
    assert payload["items"][0]["size"] == 11

    detail = client.get("/api/files/hello.txt")
    assert detail.status_code == 200
    assert detail.get_json()["accounts"] == [job["account_name"]]

    content = client.get("/api/files/hello.txt/content")
    assert content.status_code == 200
    assert content.data == b"hello world"
    assert content.headers["Content-Type"] == "text/plain"
    assert "attachment" in content.headers["Content-Disposition"]
    assert content.headers["Accept-Ranges"] == "bytes"

    head = client.head("/api/files/hello.txt/content")
    assert head.status_code == 200
    assert head.data == b""
    assert head.headers["Content-Length"] == "11"

    ranged = client.get("/api/download/hello.txt", headers={"Range": "bytes=0-4"})
    assert ranged.status_code == 206
    assert ranged.data == b"hello"
    assert ranged.headers["Content-Range"] == "bytes 0-4/11"

    invalid_range = client.get("/api/download/hello.txt", headers={"Range": "bytes=99-100"})
    assert invalid_range.status_code == 416
    assert invalid_range.content_type == "application/problem+json"


def test_content_etag_and_not_modified(client) -> None:
    _upload_and_wait(client, "etag.txt", b"data")
    detail = client.get("/api/files/etag.txt")
    content = client.get("/api/download/etag.txt")
    etag = content.headers["ETag"]
    assert etag.startswith('"')
    assert detail.headers["ETag"] == etag
    assert detail.get_json()["etag"] == etag.strip('"')
    assert client.get("/api/download/etag.txt", headers={"If-None-Match": etag}).status_code == 304
    assert client.get("/api/files/etag.txt", headers={"If-None-Match": etag}).status_code == 304


def test_streaming_failure_aborts_instead_of_truncating(client, accounts) -> None:
    job = _upload_and_wait(client, "broken.txt", b"complete body")
    accounts[job["account_name"]].stream_failure = True

    response = client.get("/api/files/broken.txt/content")

    with pytest.raises(StorageError, match="mid-stream"):
        _ = response.data


def test_queue_full_is_rejected_before_multipart_parsing(
    client, manager, settings, monkeypatch
) -> None:
    monkeypatch.setattr(manager, "queued_count", lambda: settings.queue_max)

    response = client.post(
        "/api/files",
        data=b"this is not valid multipart",
        content_type="multipart/form-data; boundary=broken",
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.get_json()["code"] == "queue_full"


def test_existing_name_becomes_a_cross_account_upsert(client) -> None:
    first = _upload_and_wait(client, "dupe.txt", b"one")
    response = _upload(client, "dupe.txt", b"two")
    assert response.status_code == 202
    job = wait_for_job(client, response.get_json()["jobs"][0]["id"])

    assert job["state"] == "complete"
    assert job["operation"] == "upsert"
    assert job["replaced"]["file_id"] == first["result"]["file_id"]
    assert job["account_name"] != first["account_name"]
    assert client.get("/api/download/dupe.txt").data == b"two"


def test_put_file_is_not_supported(client) -> None:
    response = client.put(
        "/api/files/doc.txt",
        data={"file": (io.BytesIO(b"new"), "ignored.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 405
    assert response.content_type == "application/problem+json"
    assert response.get_json()["code"] == "method_not_allowed"


def test_delete_file_and_preconditions(client) -> None:
    _upload_and_wait(client, "remove.txt", b"bytes")
    detail = client.get("/api/files/remove.txt").get_json()

    wrong = client.delete("/api/files/remove.txt", headers={"If-Match": '"nope"'})
    assert wrong.status_code == 412

    # If-Match requires strong comparison; a weak validator must not match.
    weak = client.delete(
        "/api/files/remove.txt", headers={"If-Match": f'W/"{detail["etag"]}"'}
    )
    assert weak.status_code == 412

    deleted = client.delete(
        "/api/files/remove.txt", headers={"If-Match": f'"{detail["etag"]}"'}
    )
    assert deleted.status_code == 204
    assert client.get("/api/files/remove.txt").status_code == 404
    assert client.delete("/api/files/remove.txt").status_code == 404


def test_upload_validation_and_problem_shape(client) -> None:
    response = client.post(
        "/api/files",
        data={"file": (io.BytesIO(b"x"), "../escape")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.content_type == "application/problem+json"
    payload = response.get_json()
    assert payload["code"] == "validation_error"
    assert payload["request_id"]
    assert "application/problem+json" in response.content_type

    no_file = client.post("/api/files", data={}, content_type="multipart/form-data")
    assert no_file.status_code == 400
    assert no_file.content_type == "application/problem+json"


def test_unknown_route_and_method_are_problems(client) -> None:
    missing = client.get("/api/does-not-exist")
    assert missing.status_code == 404
    assert missing.content_type == "application/problem+json"
    assert missing.get_json()["code"] == "not_found"

    method = client.post("/api/stats")
    assert method.status_code == 405
    assert method.content_type == "application/problem+json"
    assert method.get_json()["code"] == "method_not_allowed"


def test_jobs_listing_polling_and_dismissal(client) -> None:
    job = _upload_and_wait(client, "jobs.txt", b"job")

    listing = client.get("/api/jobs?state=complete")
    assert listing.status_code == 200
    payload = listing.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == job["id"]

    since = client.get(f"/api/jobs?updated_since={job['seq']}")
    assert since.get_json()["total"] == 0

    bad_state = client.get("/api/jobs?state=nope")
    assert bad_state.status_code == 400

    dismissed = client.delete(f"/api/jobs/{job['id']}")
    assert dismissed.status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_routing_plans(client) -> None:
    response = client.post(
        "/api/routing-plans",
        json={"files": [{"name": "one.bin", "size": 10}, {"name": "two.bin", "size": 10}]},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["all_routable"] is True
    assert len(payload["routable"]) == 2

    created = _upload_and_wait(client, "taken.bin", b"x")
    replacement = client.post(
        "/api/routing-plans",
        json={"files": [{"name": "taken.bin", "size": 10}]},
    )
    assert replacement.get_json()["all_routable"] is True
    routed = replacement.get_json()["routable"][0]
    assert routed["source_account"] == created["account_name"]
    assert routed["account_name"] != created["account_name"]
    assert routed["replaces"] is True

    invalid = client.post(
        "/api/routing-plans",
        json={"files": [{"name": "../bad", "size": 10}, {"name": "ok", "size": -1}]},
    )
    assert invalid.status_code == 400
    assert len(invalid.get_json()["errors"]) == 2


def test_audit_records_mutations(client) -> None:
    _upload_and_wait(client, "audit.txt", b"x")
    response = client.get("/api/audit")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] >= 1
    assert payload["items"][0]["file_name"] == "audit.txt"
    assert payload["items"][0]["request_id"]


def test_bearer_token_auth(pool, manager, settings, tmp_path) -> None:
    secured = replace(settings, api_token="secret", temp_dir=str(tmp_path / "secured"))
    app = create_app(secured, pool=pool, manager=manager, audit=manager.audit, configure_logs=False)
    app.testing = True
    client = app.test_client()

    assert client.get("/health").status_code == 200
    unauthorized = client.get("/api/files")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == 'Bearer realm="b2router"'
    assert client.get("/api/files", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_failed_bearer_auth_consumes_mutation_limit(pool, manager, settings, tmp_path) -> None:
    secured = replace(
        settings,
        temp_dir=str(tmp_path / "auth-limited"),
        api_token="secret",
        mutation_rate_per_minute=1,
    )
    app = create_app(secured, pool=pool, manager=manager, audit=manager.audit, configure_logs=False)
    app.testing = True
    client = app.test_client()
    headers = {"Authorization": "Bearer wrong"}

    first = client.post("/api/routing-plans", json={"files": []}, headers=headers)
    assert first.status_code == 401
    second = client.post("/api/routing-plans", json={"files": []}, headers=headers)
    assert second.status_code == 429
    assert second.get_json()["code"] == "too_many_requests"


def test_rate_limit_mutations(pool, manager, settings, tmp_path) -> None:
    limited = replace(
        settings,
        temp_dir=str(tmp_path / "limited"),
        mutation_rate_per_minute=1,
    )
    app = create_app(limited, pool=pool, manager=manager, audit=manager.audit, configure_logs=False)
    app.testing = True
    client = app.test_client()

    first = client.post("/api/routing-plans", json={"files": []})
    assert first.status_code == 200
    second = client.post("/api/routing-plans", json={"files": []})
    assert second.status_code == 429
    assert second.headers["Retry-After"]
    assert second.get_json()["code"] == "too_many_requests"


def test_cors_preflight_and_response_headers(pool, manager, settings, tmp_path) -> None:
    cors = replace(
        settings,
        temp_dir=str(tmp_path / "cors"),
        cors_origins=("http://example.test",),
    )
    app = create_app(cors, pool=pool, manager=manager, audit=manager.audit, configure_logs=False)
    client = app.test_client()

    preflight = client.options(
        "/api/files",
        headers={
            "Origin": "http://example.test",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == "http://example.test"

    normal = client.get("/api/files", headers={"Origin": "http://example.test"})
    assert normal.headers["Access-Control-Allow-Origin"] == "http://example.test"


def test_payload_too_large(pool, manager, settings, tmp_path) -> None:
    tiny = replace(settings, temp_dir=str(tmp_path / "tiny"), max_upload_bytes=10)
    app = create_app(tiny, pool=pool, manager=manager, audit=manager.audit, configure_logs=False)
    client = app.test_client()

    response = client.post(
        "/api/files",
        data={"file": (io.BytesIO(b"x" * 100), "big.bin")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert response.content_type == "application/problem+json"
    assert response.get_json()["code"] == "payload_too_large"


def test_file_names_with_slashes(client) -> None:
    job = _upload_and_wait(client, "folder/nested.txt", b"nested")
    assert job["state"] == "complete"
    assert client.get("/api/files/folder/nested.txt").status_code == 200
    assert client.get("/api/download/folder/nested.txt").data == b"nested"
