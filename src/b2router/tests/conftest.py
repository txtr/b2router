"""Shared fixtures: fake B2 accounts and an app wired without credentials."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from b2router.services.account import FileVersion, StorageError
from b2router.services.audit import AuditLog
from b2router.services.pool import AccountPool
from b2router.services.uploads import UploadManager
from b2router.settings import Settings
from b2router.web import create_app


class FakeDownload:
    def __init__(self, data: bytes, fail_after: int | None = None) -> None:
        self.data = data
        self.fail_after = fail_after
        self.closed = False

    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        if self.fail_after is not None:
            if self.fail_after:
                yield self.data[: self.fail_after]
            raise StorageError("b2_unavailable", "Simulated mid-stream failure.", retryable=True)
        for offset in range(0, len(self.data), chunk_size):
            yield self.data[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeStorage:
    name: str
    fail_upload: bool = False
    ambiguous_upload: bool = False
    omit_upload_marker: bool = False
    fail_delete: bool = False
    delete_failures_remaining: int = 0
    upload_delay: float = 0.0
    stream_failure: bool = False
    files: dict[str, FileVersion] = field(default_factory=dict)
    all_versions: list[FileVersion] = field(default_factory=list)
    data: dict[str, bytes] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)
    _counter: int = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"{self.name}-{self._counter}"

    def list_files(self) -> list[FileVersion]:
        return list(self.files.values())

    def list_versions(self) -> list[FileVersion]:
        return list(self.all_versions)

    def upload_local_file(
        self,
        local_path: str,
        file_name: str,
        content_type: str,
        progress_listener: object | None = None,
        file_info: dict[str, str] | None = None,
    ) -> FileVersion:
        if self.upload_delay:
            time.sleep(self.upload_delay)
        if self.fail_upload:
            raise StorageError("b2_error", "Simulated upload failure.")
        data = Path(local_path).read_bytes()
        file_id = self._new_id()
        version = FileVersion(
            file_name=file_name,
            file_id=file_id,
            size=len(data),
            content_type=content_type,
            upload_timestamp=int(time.time() * 1000),
            action="upload",
            file_info={} if self.omit_upload_marker else dict(file_info or {}),
        )
        self.files[file_name] = version
        self.all_versions.append(version)
        self.data[file_id] = data
        self.uploads.append(file_id)
        if progress_listener is not None:
            progress_listener.set_total_bytes(len(data))  # type: ignore[attr-defined]
            progress_listener.bytes_completed(len(data))  # type: ignore[attr-defined]
            progress_listener.close()  # type: ignore[attr-defined]
        if self.ambiguous_upload:
            raise StorageError("b2_timeout", "Timed out after storing.", retryable=True)
        return version

    def delete_version(self, file_id: str, file_name: str) -> None:
        if self.delete_failures_remaining > 0:
            self.delete_failures_remaining -= 1
            raise StorageError("b2_unavailable", "Simulated delete failure.", retryable=True)
        if self.fail_delete:
            raise StorageError("b2_error", "Simulated delete failure.")
        if file_id not in self.data:
            raise StorageError("b2_not_found", "Missing in fake.")
        del self.data[file_id]
        self.deleted.append(file_id)
        self.all_versions = [
            version for version in self.all_versions if version.file_id != file_id
        ]
        if file_name in self.files and self.files[file_name].file_id == file_id:
            remaining = [
                version for version in self.all_versions if version.file_name == file_name
            ]
            if remaining:
                # B2 reveals the newest surviving version after deleting the
                # latest one, so the fake must not drop the name entirely.
                self.files[file_name] = remaining[-1]
            else:
                del self.files[file_name]

    def get_file_info(self, file_name: str) -> FileVersion:
        if file_name not in self.files:
            raise StorageError("b2_not_found", "Missing in fake.")
        return self.files[file_name]

    def download(
        self, file_id: str, file_name: str, range_: tuple[int, int] | None = None
    ) -> FakeDownload:
        data = self.data[file_id]
        if range_ is not None:
            start, end = range_
            data = data[start : end + 1]
        return FakeDownload(data, fail_after=1 if self.stream_failure else None)

    def seed(self, file_name: str, data: bytes, content_type: str = "text/plain") -> FileVersion:
        file_id = self._new_id()
        version = FileVersion(
            file_name=file_name,
            file_id=file_id,
            size=len(data),
            content_type=content_type,
            upload_timestamp=int(time.time() * 1000),
            action="upload",
        )
        self.files[file_name] = version
        self.all_versions.append(version)
        self.data[file_id] = data
        return version


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        temp_dir=str(tmp_path / "tmp"),
        lock_file=str(tmp_path / "lock"),
        acquire_lock=False,
        upload_workers=2,
        queue_max=10,
        job_ttl_seconds=600,
        job_history_max=100,
        mutation_rate_per_minute=10_000,
        log_format="json",
        log_level="ERROR",
    )


@pytest.fixture
def accounts() -> dict[str, FakeStorage]:
    return {"a": FakeStorage("a"), "b": FakeStorage("b")}


@pytest.fixture
def pool(accounts: dict[str, FakeStorage]) -> AccountPool:
    pool = AccountPool(max_files_per_account=10)
    pool.add_account(accounts["a"], capacity_bytes=1024 * 1024)
    pool.add_account(accounts["b"], capacity_bytes=1024 * 1024)
    return pool


@pytest.fixture
def manager(pool: AccountPool, settings: Settings) -> Iterator[UploadManager]:
    audit = AuditLog(maxlen=1000)
    instance = UploadManager(pool, audit, settings)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def app(settings: Settings, pool: AccountPool, manager: UploadManager):
    audit = manager._audit  # the same log the manager writes to
    application = create_app(
        settings,
        pool=pool,
        manager=manager,
        audit=audit,
        configure_logs=False,
    )
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def wait_for_job(client, job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.get_json()
        if payload["state"] in {"complete", "partial", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")
