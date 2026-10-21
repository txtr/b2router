"""Asynchronous job lifecycle, including upsert and partial-failure semantics."""

from __future__ import annotations

import time
from concurrent.futures import Future
from pathlib import Path

import pytest

from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool
from b2router.services.uploads import (
    JobState,
    UploadManager,
    UploadRejected,
)
from b2router.settings import Settings
from b2router.tests.conftest import FakeStorage, wait_for_job


def _spool(tmp_path: Path, data: bytes, name: str = "upload.bin") -> str:
    path = tmp_path / f"spool-{name}"
    path.write_bytes(data)
    return str(path)


def _wait_state(manager: UploadManager, job_id: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        assert job is not None
        if job.state in {
            JobState.COMPLETE,
            JobState.PARTIAL,
            JobState.FAILED,
            JobState.CANCELLED,
        }:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_upload_job_succeeds(
    manager: UploadManager, pool: AccountPool, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    job = manager.submit(
        file_name="hello.txt",
        temp_path=_spool(tmp_path, b"hello"),
        file_size=5,
        content_type="text/plain",
        operation="upload",
        request_id="req_1",
        client="test",
    )

    finished = _wait_state(manager, job.id)

    assert finished.state is JobState.COMPLETE
    assert finished.result is not None
    assert finished.account_name in accounts
    assert pool.get_file("hello.txt") is not None
    assert accounts[finished.account_name].data[finished.result["file_id"]] == b"hello"
    assert not Path(job.temp_path).exists()


def test_upsert_expected_file_id_is_rechecked_by_the_worker(
    settings: Settings, tmp_path: Path
) -> None:
    fake = FakeStorage("a")
    fake.seed("doc.txt", b"old")
    pool = AccountPool()
    pool.add_account(fake, None)
    manager = UploadManager(pool, AuditLog(), settings)
    path = _spool(tmp_path, b"replacement")
    try:
        job = manager.submit(
            file_name="doc.txt",
            temp_path=path,
            file_size=11,
            content_type="text/plain",
            operation="upsert",
            request_id=None,
            client=None,
            expected_file_id="not-the-cached-id",
        )
        finished = _wait_state(manager, job.id)
        assert finished.state is JobState.FAILED
        assert finished.error is not None
        assert finished.error["code"] == "precondition_failed"
        assert pool.get_file("doc.txt") is not None
        assert fake.files["doc.txt"].file_id == pool.get_file("doc.txt").file_id
        assert not Path(path).exists()
    finally:
        manager.shutdown()


def test_upload_rejects_duplicate_filename(
    settings: Settings, tmp_path: Path
) -> None:
    fake = FakeStorage("a")
    fake.seed("taken.txt", b"old")
    pool = AccountPool()
    pool.add_account(fake, None)
    manager = UploadManager(pool, AuditLog(), settings)
    path = _spool(tmp_path, b"new")

    try:
        with pytest.raises(UploadRejected) as excinfo:
            manager.submit(
                file_name="taken.txt",
                temp_path=path,
                file_size=3,
                content_type="text/plain",
                operation="upload",
                request_id=None,
                client=None,
            )
        assert excinfo.value.code == "duplicate_filename"
    finally:
        Path(path).unlink(missing_ok=True)
        manager.shutdown()


def test_second_job_for_same_name_is_rejected(
    manager: UploadManager, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].upload_delay = 0.4
    first = manager.submit(
        file_name="slow.bin",
        temp_path=_spool(tmp_path, b"1"),
        file_size=1,
        content_type="text/plain",
        operation="upload",
        request_id=None,
        client=None,
    )
    deadline = time.monotonic() + 2
    while manager.get(first.id).state is JobState.QUEUED and time.monotonic() < deadline:  # type: ignore[union-attr]
        time.sleep(0.005)

    with pytest.raises(UploadRejected) as excinfo:
        manager.submit(
            file_name="slow.bin",
            temp_path=_spool(tmp_path, b"2"),
            file_size=1,
            content_type="text/plain",
            operation="upload",
            request_id=None,
            client=None,
        )
    assert excinfo.value.code == "job_active"
    assert _wait_state(manager, first.id).state is JobState.COMPLETE


def test_upsert_replaces_old_version(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake = FakeStorage("a")
    old = fake.seed("doc.txt", b"old")
    pool = AccountPool()
    pool.add_account(fake, None)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="doc.txt",
            temp_path=_spool(tmp_path, b"new content"),
            file_size=11,
            content_type="text/plain",
            operation="upsert",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id)

        assert finished.state is JobState.COMPLETE
        assert finished.replaced is not None
        assert finished.replaced["file_id"] == old.file_id
        assert old.file_id in fake.deleted
        entry = pool.get_file("doc.txt")
        assert entry is not None
        assert entry.file_id != old.file_id
        assert fake.data[entry.file_id] == b"new content"
    finally:
        manager.shutdown()


def test_upsert_on_missing_file_creates_it(
    manager: UploadManager, pool: AccountPool, tmp_path: Path
) -> None:
    job = manager.submit(
        file_name="new.txt",
        temp_path=_spool(tmp_path, b"data"),
        file_size=4,
        content_type="text/plain",
        operation="upsert",
        request_id=None,
        client=None,
    )
    finished = _wait_state(manager, job.id)
    assert finished.state is JobState.COMPLETE
    assert pool.get_file("new.txt") is not None


def test_upsert_partial_when_old_delete_fails(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake = FakeStorage("a")
    fake.seed("stuck.txt", b"old")
    fake.delete_failures_remaining = 10
    pool = AccountPool()
    pool.add_account(fake, None)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="stuck.txt",
            temp_path=_spool(tmp_path, b"much newer data"),
            file_size=15,
            content_type="text/plain",
            operation="upsert",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id, timeout=8.0)

        assert finished.state is JobState.PARTIAL
        assert finished.error is not None
        assert finished.error["code"] == "old_version_retained"
        entry = pool.get_file("stuck.txt")
        assert entry is not None
        assert entry.file_id in fake.data
        # Old bytes and the old file slot remain counted because they still exist in B2.
        assert pool.get_account_state("a").file_count == 2
        assert pool.stats()["used_bytes"] == 15 + 3
    finally:
        manager.shutdown()


def test_upload_failure_releases_reservation(
    manager: UploadManager, pool: AccountPool, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].fail_upload = True
    accounts["b"].fail_upload = True
    path = _spool(tmp_path, b"data")
    job = manager.submit(
        file_name="fail.bin",
        temp_path=path,
        file_size=4,
        content_type="text/plain",
        operation="upload",
        request_id=None,
        client=None,
    )

    finished = _wait_state(manager, job.id)

    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert pool.reservations() == []
    assert not Path(path).exists()


def test_ambiguous_upload_is_recovered(
    manager: UploadManager, pool: AccountPool, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].ambiguous_upload = True
    job = manager.submit(
        file_name="timeout.bin",
        temp_path=_spool(tmp_path, b"landed"),
        file_size=6,
        content_type="text/plain",
        operation="upload",
        request_id=None,
        client=None,
    )

    finished = _wait_state(manager, job.id)

    assert finished.state is JobState.COMPLETE
    entry = pool.get_file("timeout.bin")
    assert entry is not None
    assert accounts["a"].data[entry.file_id] == b"landed"


def test_delete_and_upload_cannot_race(
    manager: UploadManager, pool: AccountPool, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].seed("shared.bin", b"old")
    manager.begin_delete("shared.bin")
    path = _spool(tmp_path, b"new")
    try:
        with pytest.raises(UploadRejected) as excinfo:
            manager.submit(
                file_name="shared.bin",
                temp_path=path,
                file_size=3,
                content_type="text/plain",
                operation="upsert",
                request_id=None,
                client=None,
            )
        assert excinfo.value.code == "job_active"
    finally:
        manager.end_delete("shared.bin")
        Path(path).unlink(missing_ok=True)

    # Once the delete releases the name, an upsert is allowed again.
    job = manager.submit(
        file_name="shared.bin",
        temp_path=_spool(tmp_path, b"new"),
        file_size=3,
        content_type="text/plain",
        operation="upsert",
        request_id=None,
        client=None,
    )
    assert _wait_state(manager, job.id).state is JobState.COMPLETE


def test_queue_limit(
    manager: UploadManager, accounts: dict[str, FakeStorage], settings: Settings, tmp_path: Path
) -> None:
    from dataclasses import replace

    limited = replace(settings, upload_workers=1, queue_max=1)
    pool = AccountPool()
    fake = FakeStorage("a", upload_delay=0.3)
    pool.add_account(fake, None)
    manager = UploadManager(pool, AuditLog(), limited)
    paths = [_spool(tmp_path, b"1"), _spool(tmp_path, b"2"), _spool(tmp_path, b"3")]
    try:
        first = manager.submit(
            file_name="one.bin",
            temp_path=paths[0],
            file_size=1,
            content_type="text/plain",
            operation="upload",
            request_id=None,
            client=None,
        )
        deadline = time.monotonic() + 2
        while manager.get(first.id).state is JobState.QUEUED and time.monotonic() < deadline:  # type: ignore[union-attr]
            time.sleep(0.005)
        second = manager.submit(
            file_name="two.bin",
            temp_path=paths[1],
            file_size=1,
            content_type="text/plain",
            operation="upload",
            request_id=None,
            client=None,
        )
        with pytest.raises(UploadRejected) as excinfo:
            manager.submit(
                file_name="three.bin",
                temp_path=paths[2],
                file_size=1,
                content_type="text/plain",
                operation="upload",
                request_id=None,
                client=None,
            )
        assert excinfo.value.code == "queue_full"
        _wait_state(manager, first.id)
        _wait_state(manager, second.id)
    finally:
        manager.shutdown()
        for path in paths:
            Path(path).unlink(missing_ok=True)


def test_running_job_cannot_be_cancelled(
    manager: UploadManager, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].upload_delay = 0.5
    job = manager.submit(
        file_name="running.bin",
        temp_path=_spool(tmp_path, b"1"),
        file_size=1,
        content_type="text/plain",
        operation="upload",
        request_id=None,
        client=None,
    )
    deadline = time.monotonic() + 2
    while manager.get(job.id).state is JobState.QUEUED and time.monotonic() < deadline:  # type: ignore[union-attr]
        time.sleep(0.005)
    with pytest.raises(UploadRejected) as excinfo:
        manager.cancel(job.id)
    assert excinfo.value.code == "job_active"
    _wait_state(manager, job.id)


class NeverExecutor:
    def submit(self, fn, *args, **kwargs):  # noqa: ANN001
        return Future()

    def shutdown(self, wait=True, cancel_futures=False):  # noqa: ANN001
        pass


def test_cancel_queued_job(pool: AccountPool, settings: Settings, tmp_path: Path) -> None:
    from dataclasses import replace

    settings = replace(settings, upload_workers=1, queue_max=5)
    manager = UploadManager(pool, AuditLog(), settings, executor=NeverExecutor())  # type: ignore[arg-type]
    path = _spool(tmp_path, b"data")
    job = manager.submit(
        file_name="queued.bin",
        temp_path=path,
        file_size=4,
        content_type="text/plain",
        operation="upload",
        request_id=None,
        client=None,
    )

    cancelled = manager.cancel(job.id)

    assert cancelled.state is JobState.CANCELLED
    assert not Path(path).exists()
    assert manager.active_for_name("queued.bin") is None
    with pytest.raises(UploadRejected):
        manager.cancel(job.id)


def test_dismiss_only_terminal_jobs(
    manager: UploadManager, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].upload_delay = 0.3
    job = manager.submit(
        file_name="x.bin",
        temp_path=_spool(tmp_path, b"x"),
        file_size=1,
        content_type="text/plain",
        operation="upload",
        request_id=None,
        client=None,
    )
    with pytest.raises(UploadRejected):
        manager.dismiss(job.id)
    _wait_state(manager, job.id)
    manager.dismiss(job.id)
    assert manager.get(job.id) is None


def test_api_upload_flow(client) -> None:
    response = client.post(
        "/api/files",
        data={"file": (__import__("io").BytesIO(b"api bytes"), "api.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    payload = response.get_json()
    job = wait_for_job(client, payload["jobs"][0]["id"])
    assert job["state"] == "complete"
