"""Asynchronous job lifecycle, including upsert and partial-failure semantics."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

import pytest

from b2router.runtime import Runtime
from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool, PoolError
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


def test_remote_upload_with_commit_failure_becomes_partial(
    manager: UploadManager,
    pool: AccountPool,
    accounts: dict[str, FakeStorage],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_commit(*args, **kwargs):  # noqa: ANN002, ANN003
        raise PoolError("cache_error", "Simulated local cache failure.")

    monkeypatch.setattr(pool, "commit_upload", fail_commit)
    job = manager.submit(
        file_name="commit-fail.bin",
        temp_path=_spool(tmp_path, b"payload"),
        file_size=7,
        content_type="text/plain",
        request_id="req_commit_fail",
        client="test",
    )

    finished = _wait_state(manager, job.id)

    assert finished.state is JobState.PARTIAL
    assert finished.error is not None
    assert finished.error["code"] == "cache_error"
    assert finished.result is not None
    account = accounts[finished.account_name]
    assert account.data[finished.result["file_id"]] == b"payload"
    assert pool.get_file("commit-fail.bin") is None
    entries, _ = manager.audit.list(limit=10)
    assert entries[0].result == "partial"
    assert entries[0].details["job_id"] == job.id


def test_size_mismatch_that_cannot_be_cleaned_up_becomes_partial(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeStorage("a")
    pool = AccountPool()
    pool.add_account(fake, None)
    manager = UploadManager(pool, AuditLog(), settings)
    original = fake.upload_local_file

    def wrong_size(*args, **kwargs):  # noqa: ANN002, ANN003
        version = original(*args, **kwargs)
        return replace(version, size=version.size + 1)

    monkeypatch.setattr(fake, "upload_local_file", wrong_size)
    fake.fail_delete = True
    try:
        job = manager.submit(
            file_name="wrong-size.bin",
            temp_path=_spool(tmp_path, b"hello"),
            file_size=5,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id)

        assert finished.state is JobState.PARTIAL
        assert finished.error is not None
        assert finished.error["code"] == "upload_cleanup_failed"
        assert finished.error["details"]["expected"] == 5
        assert finished.error["details"]["actual"] == 6
        assert fake.data[finished.result["file_id"]] == b"hello"
        assert pool.get_file("wrong-size.bin") is None
    finally:
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
            request_id=None,
            client=None,
        )
    assert excinfo.value.code == "job_active"
    assert _wait_state(manager, first.id).state is JobState.COMPLETE


def test_upsert_replaces_old_version_in_another_account(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake_a = FakeStorage("a")
    old = fake_a.seed("doc.txt", b"old")
    fake_b = FakeStorage("b")
    pool = AccountPool()
    pool.add_account(fake_a, None)
    pool.add_account(fake_b, None)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="doc.txt",
            temp_path=_spool(tmp_path, b"new content"),
            file_size=11,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id)

        assert finished.state is JobState.COMPLETE
        assert finished.replaced is not None
        assert finished.replaced["file_id"] == old.file_id
        assert old.file_id in fake_a.deleted
        entry = pool.get_file("doc.txt")
        assert entry is not None
        assert entry.accounts == ("b",)
        assert entry.file_id != old.file_id
        assert fake_b.data[entry.file_id] == b"new content"
        assert pool.get_account_state("a").file_count == 0
        assert pool.get_account_state("b").file_count == 1
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
        request_id=None,
        client=None,
    )
    finished = _wait_state(manager, job.id)
    assert finished.state is JobState.COMPLETE
    assert pool.get_file("new.txt") is not None


def test_upsert_fails_when_source_is_the_only_account(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake = FakeStorage("a")
    old = fake.seed("only.txt", b"old")
    pool = AccountPool()
    pool.add_account(fake, capacity_bytes=1000)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="only.txt",
            temp_path=_spool(tmp_path, b"new"),
            file_size=3,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id)

        assert finished.state is JobState.FAILED
        assert finished.error is not None
        assert finished.error["code"] == "no_replacement_account"
        assert pool.get_file("only.txt").file_id == old.file_id  # type: ignore[union-attr]
        assert fake.data == {old.file_id: b"old"}
        assert pool.get_account_state("a").file_count == 1
        assert pool.stats()["reserved_bytes"] == 0
    finally:
        manager.shutdown()


def test_upsert_moves_to_another_account_and_frees_source(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake_a = FakeStorage("a")
    old = fake_a.seed("only.txt", b"old")
    fake_b = FakeStorage("b")
    pool = AccountPool(max_files_per_account=1)
    pool.add_account(fake_a, capacity_bytes=1000)
    pool.add_account(fake_b, capacity_bytes=1000)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="only.txt",
            temp_path=_spool(tmp_path, b"new"),
            file_size=3,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id)

        assert finished.state is JobState.COMPLETE
        assert old.file_id in fake_a.deleted
        entry = pool.get_file("only.txt")
        assert entry is not None
        assert entry.accounts == ("b",)
        assert entry.file_id != old.file_id
        assert fake_b.data[entry.file_id] == b"new"
        assert pool.get_account_state("a").file_count == 0
        assert pool.get_account_state("a").used_bytes == 0
        assert pool.get_account_state("b").file_count == 1
        assert pool.get_account_state("b").used_bytes == 3
        assert pool.stats()["reserved_bytes"] == 0
    finally:
        manager.shutdown()


def test_ambiguous_upload_without_matching_marker_is_partial(
    manager: UploadManager, pool: AccountPool, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].ambiguous_upload = True
    accounts["a"].omit_upload_marker = True

    job = manager.submit(
        file_name="unknown.bin",
        temp_path=_spool(tmp_path, b"landed"),
        file_size=6,
        content_type="text/plain",
        request_id=None,
        client=None,
    )

    finished = _wait_state(manager, job.id)

    assert finished.state is JobState.PARTIAL
    assert finished.error is not None
    assert finished.error["code"] == "upload_outcome_unknown"
    assert pool.get_file("unknown.bin") is None
    assert pool.stats()["used_bytes"] == 0


def test_upsert_rolls_back_new_version_when_old_delete_fails(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake_a = FakeStorage("a")
    old = fake_a.seed("stuck.txt", b"old")
    # The old-version delete exhausts all retries; the rollback then succeeds.
    fake_a.delete_failures_remaining = 3
    fake_b = FakeStorage("b")
    pool = AccountPool()
    pool.add_account(fake_a, None)
    pool.add_account(fake_b, None)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="stuck.txt",
            temp_path=_spool(tmp_path, b"much newer data"),
            file_size=15,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id, timeout=8.0)

        assert finished.state is JobState.FAILED
        assert finished.error is not None
        assert finished.error["code"] == "old_version_retained"
        assert "rolled back" in finished.error["message"]
        entry = pool.get_file("stuck.txt")
        assert entry is not None
        assert entry.file_id == old.file_id
        assert fake_a.files["stuck.txt"].file_id == old.file_id
        assert fake_a.data == {old.file_id: b"old"}
        assert [version.file_id for version in fake_a.all_versions] == [old.file_id]
        assert fake_b.data == {}
        assert fake_b.all_versions == []
        assert len(fake_b.deleted) == 1
        assert pool.get_account_state("a").file_count == 1
        assert pool.get_account_state("b").file_count == 0
        assert pool.stats()["used_bytes"] == len(b"old")
    finally:
        manager.shutdown()


def test_upsert_accepts_ambiguous_old_delete_when_old_version_is_gone(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_a = FakeStorage("a")
    old = fake_a.seed("ambiguous.txt", b"old")
    fake_b = FakeStorage("b")
    pool = AccountPool()
    pool.add_account(fake_a, None)
    pool.add_account(fake_b, None)
    manager = UploadManager(pool, AuditLog(), settings)

    def delete_old_but_report_failure(entry) -> bool:  # noqa: ANN001
        fake_a.delete_version(entry.file_id, entry.file_name)
        return False

    monkeypatch.setattr(manager, "_delete_old", delete_old_but_report_failure)
    try:
        job = manager.submit(
            file_name="ambiguous.txt",
            temp_path=_spool(tmp_path, b"new"),
            file_size=3,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id)

        assert finished.state is JobState.COMPLETE
        entry = pool.get_file("ambiguous.txt")
        assert entry is not None
        assert entry.accounts == ("b",)
        assert entry.file_id != old.file_id
        assert old.file_id not in fake_a.data
        assert fake_a.all_versions == []
        assert [version.file_id for version in fake_b.all_versions] == [entry.file_id]
    finally:
        manager.shutdown()


def test_upsert_partial_when_old_delete_and_rollback_fail(
    settings: Settings,
    tmp_path: Path,
) -> None:
    fake_a = FakeStorage("a")
    old = fake_a.seed("stuck.txt", b"old")
    fake_a.fail_delete = True
    fake_b = FakeStorage("b")
    fake_b.fail_delete = True
    pool = AccountPool()
    pool.add_account(fake_a, None)
    pool.add_account(fake_b, None)
    manager = UploadManager(pool, AuditLog(), settings)
    try:
        job = manager.submit(
            file_name="stuck.txt",
            temp_path=_spool(tmp_path, b"much newer data"),
            file_size=15,
            content_type="text/plain",
            request_id=None,
            client=None,
        )

        finished = _wait_state(manager, job.id, timeout=8.0)

        assert finished.state is JobState.PARTIAL
        assert finished.error is not None
        assert finished.error["code"] == "old_version_retained"
        assert finished.error["details"]["rollback_failed"] is True
        entry = pool.get_file("stuck.txt")
        assert entry is not None
        assert entry.accounts == ("b",)
        assert entry.file_id != old.file_id
        assert fake_b.data[entry.file_id] == b"much newer data"
        assert fake_a.data[old.file_id] == b"old"
        # Both copies still exist in B2, one in each account.
        assert pool.get_account_state("a").file_count == 1
        assert pool.get_account_state("a").used_bytes == len(b"old")
        assert pool.get_account_state("b").file_count == 1
        assert pool.get_account_state("b").used_bytes == 15
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
            request_id=None,
            client=None,
        )
        with pytest.raises(UploadRejected) as excinfo:
            manager.submit(
                file_name="three.bin",
                temp_path=paths[2],
                file_size=1,
                content_type="text/plain",
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
    settings = replace(settings, upload_workers=1, queue_max=5)
    manager = UploadManager(pool, AuditLog(), settings, executor=NeverExecutor())  # type: ignore[arg-type]
    path = _spool(tmp_path, b"data")
    job = manager.submit(
        file_name="queued.bin",
        temp_path=path,
        file_size=4,
        content_type="text/plain",
        request_id=None,
        client=None,
    )

    cancelled = manager.cancel(job.id)

    assert cancelled.state is JobState.CANCELLED
    assert not Path(path).exists()
    assert manager.active_for_name("queued.bin") is None
    with pytest.raises(UploadRejected):
        manager.cancel(job.id)


def test_shutdown_cancels_queued_jobs(pool: AccountPool, settings: Settings, tmp_path: Path) -> None:
    settings = replace(settings, upload_workers=1, queue_max=5)
    manager = UploadManager(pool, AuditLog(), settings, executor=NeverExecutor())  # type: ignore[arg-type]
    path = _spool(tmp_path, b"queued")
    job = manager.submit(
        file_name="shutdown-queued.bin",
        temp_path=path,
        file_size=6,
        content_type="text/plain",
        request_id=None,
        client=None,
    )

    manager.shutdown()

    assert job.state is JobState.CANCELLED
    assert not Path(path).exists()
    assert manager.get(job.id) is not None


def test_shutdown_waits_for_running_upload(
    manager: UploadManager,
    accounts: dict[str, FakeStorage],
    tmp_path: Path,
) -> None:
    accounts["a"].upload_delay = 0.5
    accounts["b"].upload_delay = 0.5
    job = manager.submit(
        file_name="slow-shutdown.bin",
        temp_path=_spool(tmp_path, b"data"),
        file_size=4,
        content_type="text/plain",
        request_id=None,
        client=None,
    )
    deadline = time.monotonic() + 2
    while manager.get(job.id).state is JobState.QUEUED and time.monotonic() < deadline:  # type: ignore[union-attr]
        time.sleep(0.005)
    assert manager.get(job.id).state is JobState.UPLOADING  # type: ignore[union-attr]

    started = time.monotonic()
    manager.shutdown()
    elapsed = time.monotonic() - started

    finished = manager.get(job.id)
    assert finished is not None
    assert finished.state is JobState.COMPLETE
    assert elapsed >= 0.4


def test_shutdown_waits_for_in_flight_delete(manager: UploadManager) -> None:
    manager.begin_delete("blocked.bin")
    finished = threading.Event()

    def drain() -> None:
        manager.shutdown()
        finished.set()

    thread = threading.Thread(target=drain)
    thread.start()
    deadline = time.monotonic() + 2
    while not manager.closed and time.monotonic() < deadline:
        time.sleep(0.005)

    assert manager.closed
    assert not finished.wait(0.1)

    manager.end_delete("blocked.bin")
    assert finished.wait(2.0)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_delete_rejected_after_shutdown(manager: UploadManager) -> None:
    manager.shutdown()
    with pytest.raises(UploadRejected) as excinfo:
        manager.begin_delete("late.bin")
    assert excinfo.value.code == "service_unavailable"


def test_runtime_releases_lock_after_drain(
    manager: UploadManager,
    accounts: dict[str, FakeStorage],
    pool: AccountPool,
    settings: Settings,
    tmp_path: Path,
) -> None:
    accounts["a"].upload_delay = 0.5
    accounts["b"].upload_delay = 0.5
    runtime = Runtime(settings=settings, pool=pool, manager=manager, audit=AuditLog())

    class RecordingLock:
        released = False
        active_at_release = -1
        queued_at_release = -1

        def release(self) -> None:
            self.released = True
            self.active_at_release = manager.active_count()
            self.queued_at_release = manager.queued_count()

    lock = RecordingLock()
    runtime.lock = lock
    job = manager.submit(
        file_name="runtime-drain.bin",
        temp_path=_spool(tmp_path, b"data"),
        file_size=4,
        content_type="text/plain",
        request_id=None,
        client=None,
    )
    deadline = time.monotonic() + 2
    while manager.get(job.id).state is JobState.QUEUED and time.monotonic() < deadline:  # type: ignore[union-attr]
        time.sleep(0.005)
    assert manager.get(job.id).state is JobState.UPLOADING  # type: ignore[union-attr]

    runtime.shutdown()

    assert lock.released
    assert lock.active_at_release == 0
    assert lock.queued_at_release == 0
    finished = manager.get(job.id)
    assert finished is not None
    assert finished.state is JobState.COMPLETE


def test_dismiss_only_terminal_jobs(
    manager: UploadManager, accounts: dict[str, FakeStorage], tmp_path: Path
) -> None:
    accounts["a"].upload_delay = 0.3
    job = manager.submit(
        file_name="x.bin",
        temp_path=_spool(tmp_path, b"x"),
        file_size=1,
        content_type="text/plain",
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
