"""Job-based upserts.

An upsert is asynchronous: the HTTP request spools bytes to a private temp file
and returns 202 with a job. A small worker pool performs routing, reservation,
the B2 transfer, and, when the name already exists, deletion of the old version
in a different account. All state transitions are visible through
``UploadJob.to_dict`` and every mutation is audited.

The worker pool is deliberately bounded: B2 transaction caps and the in-memory
accounting both prefer predictable throughput over unbounded parallelism.
"""

from __future__ import annotations

import builtins
import contextlib
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from b2router import metrics
from b2router.logging_setup import client_label_var, job_id_var, log_event, request_id_var
from b2router.services.account import FileVersion, StorageBackend, StorageError
from b2router.services.audit import AuditLog
from b2router.services.pool import (
    AccountPool,
    FileEntry,
    PoolError,
    Reservation,
)
from b2router.settings import Settings
from b2router.types import AccountName, FileName, JobId, RequestId


class JobState(StrEnum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    REPLACING = "replacing"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset(
    {JobState.COMPLETE, JobState.PARTIAL, JobState.FAILED, JobState.CANCELLED}
)
ACTIVE_STATES = frozenset({JobState.UPLOADING, JobState.REPLACING})
JOB_MARKER_KEY = "b2router_job_id"


class UploadRejected(Exception):
    """A submission was refused before a job was created."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class UploadJob:
    id: JobId
    file_name: FileName
    file_size: int
    content_type: str
    temp_path: str
    request_id: RequestId | None
    client: str | None
    operation: Literal["upsert"] = "upsert"
    account_name: AccountName | None = None
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    bytes_sent: int = 0
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    replaced: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    seq: int = 0
    seq_source: Callable[[], int] | None = field(default=None, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "operation": self.operation,
                "file_name": self.file_name,
                "file_size": self.file_size,
                "content_type": self.content_type,
                "account_name": self.account_name,
                "state": self.state.value,
                "progress": round(self.progress, 4),
                "bytes_sent": self.bytes_sent,
                "error": self.error,
                "result": self.result,
                "replaced": self.replaced,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "request_id": self.request_id,
                "client": self.client,
                "seq": self.seq,
            }

    # -- transitions ---------------------------------------------------
    def _touch(self) -> None:
        if self.seq_source is not None:
            self.seq = self.seq_source()

    def begin(self) -> bool:
        with self._lock:
            if self.state != JobState.QUEUED:
                return False
            self.state = JobState.UPLOADING
            self.started_at = _now()
            self._touch()
            return True

    def set_state(self, state: JobState) -> None:
        with self._lock:
            self.state = state
            self._touch()

    def update_progress(self, bytes_sent: int, total: int | None = None) -> None:
        with self._lock:
            # ``file_size`` is the immutable spooled size captured at submit.
            # The SDK's reported total must not overwrite the value used for
            # capacity accounting and post-upload verification.
            del total
            self.bytes_sent = bytes_sent
            self.progress = min(bytes_sent / (self.file_size or 1), 1.0)
            # Every observable byte count invalidates list/detail ETags, not
            # only the transitions between integer percentages.
            self._touch()

    def complete(self, result: dict[str, Any]) -> None:
        with self._lock:
            self.state = JobState.COMPLETE
            self.progress = 1.0
            self.bytes_sent = self.file_size
            self.result = result
            self.finished_at = _now()
            self._touch()

    def partial(
        self,
        result: dict[str, Any],
        error: dict[str, Any],
        replaced: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self.state = JobState.PARTIAL
            self.progress = 1.0
            self.result = result
            self.error = error
            self.replaced = replaced
            self.finished_at = _now()
            self._touch()

    def partial_unresolved(
        self, error: dict[str, Any], replaced: dict[str, Any] | None = None
    ) -> None:
        """Mark an operation whose remote outcome could not be confirmed."""
        with self._lock:
            self.state = JobState.PARTIAL
            self.error = error
            self.replaced = replaced
            self.finished_at = _now()
            self._touch()

    def fail(self, code: str, message: str, **details: Any) -> None:
        with self._lock:
            self.state = JobState.FAILED
            self.error = {
                "code": code,
                "message": message,
                **({"details": details} if details else {}),
            }
            self.finished_at = _now()
            self._touch()

    def try_cancel(self) -> bool:
        """Atomically cancel a queued job; running jobs cannot be cancelled."""
        with self._lock:
            if self.state != JobState.QUEUED:
                return False
            self.state = JobState.CANCELLED
            self.finished_at = _now()
            self._touch()
            return True


class _ProgressListener:
    """Progress listener implementing the ProgressListener protocol."""

    def __init__(self, job: UploadJob) -> None:
        self._job = job
        self._closed = False

    def set_total_bytes(self, total_byte_count: int) -> None:
        self._job.update_progress(0, total_byte_count)

    def bytes_completed(self, byte_count: int) -> None:
        self._job.update_progress(byte_count)

    def close(self) -> None:
        if not self._closed:
            self._closed = True

    def change_description(self, new_description: str) -> bool:
        return True


class UploadManager:
    def __init__(
        self,
        pool: AccountPool,
        audit: AuditLog,
        settings: Settings,
        *,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._pool = pool
        self._audit = audit
        self._settings = settings
        self._jobs: OrderedDict[str, UploadJob] = OrderedDict()
        self._active_names: dict[str, str] = {}
        self._deleting: set[str] = set()
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._delete_done = threading.Condition(self._lock)
        self._seq_lock = threading.Lock()
        self._last_seq = 0
        self._closed = False
        self._executor = executor or ThreadPoolExecutor(
            max_workers=settings.upload_workers,
            thread_name_prefix="b2-upload",
        )
        self._owns_executor = executor is None

    @property
    def pool(self) -> AccountPool:
        return self._pool

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def _blocking_name_locked(self, file_name: str) -> str | None:
        """Return the live job holding ``file_name``, pruning finished leftovers.

        A job is marked terminal before the worker's cleanup runs. Treating that
        short window as "busy" made a poll-then-delete sequence flaky, because
        the cache is already committed once the job reports complete.
        """
        existing = self._active_names.get(file_name)
        if existing is None:
            return None
        job = self._jobs.get(existing)
        if job is None or job.state in TERMINAL_STATES:
            self._active_names.pop(file_name, None)
            return None
        return existing

    # -- submission ----------------------------------------------------
    def submit(
        self,
        *,
        file_name: str,
        temp_path: str,
        file_size: int,
        content_type: str,
        request_id: str | None,
        client: str | None,
    ) -> UploadJob:
        with self._lock:
            if self._closed:
                raise UploadRejected("service_unavailable", "The server is shutting down.")
            self._prune()
            existing = self._blocking_name_locked(file_name)
            if existing is not None:
                raise UploadRejected(
                    "job_active",
                    f"An operation for {file_name!r} is already in progress.",
                    file_name=file_name,
                    job_id=existing,
                )
            if file_name in self._deleting:
                raise UploadRejected(
                    "job_active",
                    f"A delete for {file_name!r} is in progress.",
                    file_name=file_name,
                )
            queued = sum(1 for job in self._jobs.values() if job.state == JobState.QUEUED)
            if queued >= self._settings.queue_max:
                raise UploadRejected(
                    "queue_full",
                    "The upload queue is full; retry once active uploads finish.",
                    queue_max=self._settings.queue_max,
                )
            job = UploadJob(
                id=JobId(f"job_{uuid.uuid4().hex}"),
                file_name=FileName(file_name),
                file_size=file_size,
                content_type=content_type,
                temp_path=temp_path,
                request_id=RequestId(request_id) if request_id else None,
                client=client,
                seq=self._next_seq(),
                seq_source=self._next_seq,
            )
            self._jobs[job.id] = job
            self._active_names[file_name] = job.id
            try:
                self._futures[job.id] = self._executor.submit(self._run, job)
            except Exception as exc:
                self._jobs.pop(job.id, None)
                self._active_names.pop(file_name, None)
                self._cleanup_temp(job)
                raise UploadRejected(
                    "service_unavailable",
                    "The upload worker pool is unavailable.",
                ) from exc
        metrics.refresh_jobs(self)
        log_event(
            "upload.queued",
            job_id=job.id,
            file_name=file_name,
            size=file_size,
            operation="upsert",
        )
        return job

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._last_seq += 1
            return self._last_seq

    # -- worker --------------------------------------------------------
    def _run(self, job: UploadJob) -> None:
        job_id_var.set(job.id)
        request_id_var.set(job.request_id)
        client_label_var.set(job.client)
        started = time.monotonic()
        reservation: Reservation | None = None
        client: StorageBackend | None = None
        old_entry: FileEntry | None = None
        remote_version: FileVersion | None = None
        committed = False
        if not job.begin():
            self._cleanup_temp(job)
            self._release_name(job)
            return
        metrics.refresh_jobs(self)
        old_entry = self._pool.get_file(job.file_name)
        max_rate_limit_retries = len(self._pool._states)  # Try each account at most once
        try:
            for attempt in range(max_rate_limit_retries):
                try:
                    if reservation is not None:
                        self._pool.release(reservation.id)
                        reservation = None
                    decision, reservation = self._pool.reserve(job.file_name, job.file_size)
                    job.account_name = decision.account_name
                    client = self._pool.require(decision.account_name)
                    listener = _ProgressListener(job)
                    remote_version = client.upload_local_file(
                        job.temp_path,
                        job.file_name,
                        job.content_type,
                        listener,
                        file_info={JOB_MARKER_KEY: job.id},
                    )
                    if remote_version.size != job.file_size:
                        committed = self._partial_size_mismatch(
                            job,
                            old_entry,
                            remote_version,
                            client,
                            started,
                        )
                    else:
                        committed = self._complete_upsert(
                            job, reservation, old_entry, remote_version, client, started
                        )
                    break  # Success - exit retry loop
                except StorageError as exc:
                    if exc.code == "b2_rate_limited" and attempt < max_rate_limit_retries - 1:
                        # Rate limited on this account - mark it and retry on another
                        if reservation is not None:
                            self._pool.mark_rate_limited(reservation.account_name)
                            log_event(
                                "upload.rate_limited_retry",
                                job_id=job.id,
                                file_name=job.file_name,
                                account=reservation.account_name,
                                attempt=attempt + 1,
                            )
                        continue  # Loop will retry with a new reservation
                    # Not rate limited, or out of retries - handle as normal error
                    if (
                        not committed
                        and exc.retryable
                        and client is not None
                        and reservation is not None
                    ):
                        committed = self._recover_after_transport_error(
                            job, reservation, old_entry, client, started
                        )
                    if not committed and remote_version is not None:
                        committed = self._partial_commit_failure(
                            job,
                            old_entry,
                            remote_version,
                            exc,
                            started,
                        )
                    if not committed:
                        self._fail(
                            job,
                            exc.code,
                            str(exc),
                            started,
                            **getattr(exc, "details", {}),
                        )
                    break
                except PoolError as exc:
                    # Pool errors (no space, etc.) are not retryable on another account
                    if not committed and remote_version is not None:
                        committed = self._partial_commit_failure(
                            job,
                            old_entry,
                            remote_version,
                            exc,
                            started,
                        )
                    if not committed:
                        self._fail(
                            job,
                            exc.code,
                            str(exc),
                            started,
                            **exc.details,
                        )
                    break
                except BaseException as exc:  # noqa: BLE001 - worker must never die silently
                    log_event(
                        "upload.crashed",
                        level=40,
                        job_id=job.id,
                        file_name=job.file_name,
                        error=type(exc).__name__,
                    )
                    if not committed:
                        if remote_version is not None:
                            committed = self._partial_commit_failure(
                                job,
                                old_entry,
                                remote_version,
                                exc,
                                started,
                            )
                        else:
                            self._fail(
                                job, "upload_failed", "The upload failed unexpectedly.", started
                            )
                    break
        finally:
            if reservation is not None:
                self._pool.release(reservation.id)
            self._cleanup_temp(job)
            self._release_name(job)
            metrics.refresh_jobs(self)
            job_id_var.set(None)
            request_id_var.set(None)
            client_label_var.set(None)

    def _complete_upsert(
        self,
        job: UploadJob,
        reservation: Reservation,
        old_entry: FileEntry | None,
        version: FileVersion,
        client: StorageBackend,
        started: float,
    ) -> bool:
        if old_entry is not None:
            job.set_state(JobState.REPLACING)
            old_deleted = self._delete_old(old_entry)
            old_exists: bool | None = None
            if not old_deleted:
                # A failed delete can be ambiguous. If B2 says the old version
                # is gone, keep the new one; if it is still there, roll the new
                # one back so the name does not remain duplicated.
                old_exists = self._old_version_exists(old_entry)
                if old_exists is False:
                    old_deleted = True
                elif old_exists and self._rollback_new_version(job, client, version):
                    self._fail(
                        job,
                        "old_version_retained",
                        (
                            "The replacement was rolled back because the previous version "
                            "could not be deleted; the original file is unchanged."
                        ),
                        started,
                    )
                    return True
            try:
                entry = self._pool.commit_replace(
                    reservation, old_entry, version, old_deleted=old_deleted
                )
            except (PoolError, StorageError) as exc:
                return self._partial_commit_failure(job, old_entry, version, exc, started)
            if old_deleted:
                job.replaced = old_entry.to_dict()
                job.complete(entry.to_dict())
            else:
                job.partial(
                    entry.to_dict(),
                    {
                        "code": "old_version_retained",
                        "message": (
                            "The new version was stored, but the previous version could not "
                            "be deleted and rolling back the new version also failed. Run "
                            "b2router-reconcile --apply with the gateway stopped before "
                            "restarting."
                        ),
                        "details": {
                            **old_entry.to_dict(),
                            "rollback_failed": True,
                            "old_version_exists": old_exists,
                        },
                    },
                    old_entry.to_dict(),
                )
        else:
            try:
                entry = self._pool.commit_upload(reservation, version)
            except (PoolError, StorageError) as exc:
                return self._partial_commit_failure(job, old_entry, version, exc, started)
            job.complete(entry.to_dict())
        self._record_success(job, started)
        return True

    def _partial_size_mismatch(
        self,
        job: UploadJob,
        old_entry: FileEntry | None,
        version: FileVersion,
        client: StorageBackend,
        started: float,
    ) -> bool:
        cleanup_error: StorageError | None = None
        try:
            client.delete_version(version.file_id, version.file_name)
        except StorageError as exc:
            cleanup_error = exc
            if exc.code == "b2_not_found":
                cleanup_error = None
        if cleanup_error is None:
            self._fail(
                job,
                "upload_failed",
                "B2 stored a different number of bytes than were uploaded.",
                started,
                expected=job.file_size,
                actual=version.size,
            )
            return True

        job.partial(
            self._version_dict(version),
            {
                "code": "upload_cleanup_failed",
                "message": (
                    "B2 stored a different number of bytes and the invalid version could not "
                    "be removed. Run b2router-reconcile --apply with the gateway stopped before "
                    "retrying."
                ),
                "details": {
                    "expected": job.file_size,
                    "actual": version.size,
                    "cleanup_error": cleanup_error.code,
                    "file_id": version.file_id,
                    "account": job.account_name,
                },
            },
            old_entry.to_dict() if old_entry is not None else None,
        )
        self._record_success(job, started)
        return True

    def _partial_commit_failure(
        self,
        job: UploadJob,
        old_entry: FileEntry | None,
        version: FileVersion,
        exc: BaseException,
        started: float,
    ) -> bool:
        code = getattr(exc, "code", "commit_failed")
        details = getattr(exc, "details", {})
        if not isinstance(details, dict):
            details = {}
        job.partial(
            self._version_dict(version),
            {
                "code": code,
                "message": (
                    "The upload reached B2, but the local cache commit failed. The cache may "
                    "be stale; run b2router-reconcile --apply with the gateway stopped before "
                    "retrying or restarting."
                ),
                "details": {
                    **details,
                    "job_id": job.id,
                    "file_id": version.file_id,
                    "account": job.account_name,
                    "previous_file_id": old_entry.file_id if old_entry is not None else None,
                },
            },
            old_entry.to_dict() if old_entry is not None else None,
        )
        self._record_success(job, started)
        return True

    @staticmethod
    def _version_dict(version: FileVersion) -> dict[str, Any]:
        return {
            "file_name": version.file_name,
            "file_id": version.file_id,
            "size": version.size,
            "content_type": version.content_type,
            "upload_timestamp": version.upload_timestamp,
            "action": version.action,
        }

    def _recover_after_transport_error(
        self,
        job: UploadJob,
        reservation: Reservation,
        old_entry: FileEntry | None,
        client: StorageBackend,
        started: float,
    ) -> bool:
        """Resolve an ambiguous transport error using the upload's fileInfo marker.

        A timeout can happen after B2 accepted the bytes. Timestamps and sizes
        are not unique enough to prove ownership, so the upload carries an opaque
        job id in B2 ``fileInfo`` and recovery only commits an exact marker match.
        If B2 cannot confirm the outcome after bounded retries, the job becomes
        ``partial``/unknown instead of guessing.
        """
        latest = None
        last_error: StorageError | None = None
        for attempt in range(3):
            try:
                latest = client.get_file_info(job.file_name)
                break
            except StorageError as exc:
                last_error = exc
                if not exc.retryable:
                    break
                if attempt < 2:
                    time.sleep(0.2 * (2**attempt))

        if latest is None:
            if last_error is not None and last_error.code == "b2_not_found":
                # No version is visible under this name. Retrying the upload is
                # safe and more useful than leaving a permanent unknown job.
                return False
            self._partial_unknown(
                job,
                old_entry,
                started,
                "B2 could not be queried after the upload failed.",
            )
            return True

        marker = latest.file_info.get(JOB_MARKER_KEY)
        if marker != job.id or latest.size != job.file_size:
            self._partial_unknown(
                job,
                old_entry,
                started,
                "B2 did not return a matching job marker for the upload.",
            )
            return True

        cached = self._pool.get_file(job.file_name)
        try:
            if cached is not None and cached.file_id == latest.file_id:
                # The commit already happened; only the job transition was lost.
                if job.state not in TERMINAL_STATES:
                    if old_entry is not None:
                        job.replaced = old_entry.to_dict()
                    job.complete(cached.to_dict())
                    self._record_success(job, started)
                return True

            self._complete_upsert(job, reservation, old_entry, latest, client, started)
            if job.state is not JobState.FAILED:
                log_event(
                    "upload.recovered",
                    job_id=job.id,
                    file_name=job.file_name,
                    account=job.account_name,
                    size=latest.size,
                )
            return True
        except (PoolError, StorageError):
            return False

    def _partial_unknown(
        self,
        job: UploadJob,
        old_entry: FileEntry | None,
        started: float,
        message: str,
    ) -> None:
        job.partial_unresolved(
            {
                "code": "upload_outcome_unknown",
                "message": (
                    f"{message} The upload may or may not have landed. Run "
                    "b2router-reconcile --apply with the gateway stopped before retrying."
                ),
                "details": {"job_id": job.id},
            },
            old_entry.to_dict() if old_entry is not None else None,
        )
        self._record_success(job, started)

    def _append_audit(self, job: UploadJob, **fields: Any) -> None:
        try:
            self._audit.append("upsert", **fields)
        except Exception:  # pragma: no cover - audit must never break a transfer
            log_event("upload.audit_failed", level=40, job_id=job.id)

    def _record_success(self, job: UploadJob, started: float) -> None:
        duration = (time.monotonic() - started) * 1000
        result = "partial" if job.state == JobState.PARTIAL else "success"
        metrics.UPLOADS.labels(result=result).inc()
        if job.state == JobState.COMPLETE:
            metrics.UPLOAD_BYTES.inc(job.file_size)
        self._append_audit(
            job,
            result=result,
            file_name=job.file_name,
            account_name=job.account_name,
            file_size=job.file_size,
            error_code=job.error["code"] if job.error else None,
            request_id=job.request_id,
            client=job.client,
            duration_ms=duration,
            details={"job_id": job.id, "replaced": job.replaced is not None},
        )
        log_event(
            "upload.finished",
            level=logging.WARNING if job.state == JobState.PARTIAL else logging.INFO,
            job_id=job.id,
            file_name=job.file_name,
            account=job.account_name,
            state=job.state.value,
            size=job.file_size,
            duration_ms=round(duration, 2),
        )

    def _delete_old(self, entry: FileEntry) -> bool:
        account_name = entry.account_name
        client = self._pool.get(account_name)
        if client is None:
            return False
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                client.delete_version(entry.file_id, entry.file_name)
                return True
            except StorageError as exc:
                last_error = exc
                if exc.code == "b2_not_found":
                    return True
                if not exc.retryable:
                    break
                time.sleep(0.5 * (2**attempt))
        log_event(
            "upload.old_version_delete_failed",
            level=40,
            file_name=entry.file_name,
            account=account_name,
            file_id=entry.file_id,
            error=str(last_error),
        )
        return False

    def _old_version_exists(self, entry: FileEntry) -> bool | None:
        """Return whether ``entry`` is still stored, or ``None`` if unknown.

        A delete timeout can mean B2 removed the version but the response was
        lost. Before rolling the new version back, make sure the old one is
        actually still there; otherwise the rollback would delete the only
        surviving copy.
        """
        account_name = entry.account_name
        client = self._pool.get(account_name)
        if client is None:
            return None
        try:
            versions = client.list_versions()
        except StorageError as exc:
            log_event(
                "upload.old_version_state_unknown",
                level=40,
                file_name=entry.file_name,
                account=account_name,
                file_id=entry.file_id,
                error=str(exc),
            )
            return None
        return any(version.file_id == entry.file_id for version in versions)

    def _rollback_new_version(
        self, job: UploadJob, client: StorageBackend, version: FileVersion
    ) -> bool:
        """Delete a newly stored version after its predecessor could not be removed.

        Upserts upload first so a crash cannot lose the previous copy. Once the
        old-version delete fails, that new copy is the duplicate we must remove
        before leaving the job failed. This is best-effort: if B2 refuses the
        rollback too, the job becomes ``partial`` and startup/reconcile is
        required.
        """
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                client.delete_version(version.file_id, version.file_name)
                log_event(
                    "upload.replacement_rolled_back",
                    job_id=job.id,
                    file_name=job.file_name,
                    account=job.account_name,
                    file_id=version.file_id,
                )
                return True
            except StorageError as exc:
                last_error = exc
                if exc.code == "b2_not_found":
                    return True
                if not exc.retryable:
                    break
                time.sleep(0.5 * (2**attempt))
        log_event(
            "upload.replacement_rollback_failed",
            level=40,
            job_id=job.id,
            file_name=job.file_name,
            account=job.account_name,
            file_id=version.file_id,
            error=str(last_error),
        )
        return False

    def _fail(
        self,
        job: UploadJob,
        code: str,
        message: str,
        started: float,
        **details: Any,
    ) -> None:
        job.fail(code, message, **details)
        metrics.UPLOADS.labels(result="failed").inc()
        duration = (time.monotonic() - started) * 1000
        self._append_audit(
            job,
            result="failed",
            file_name=job.file_name,
            account_name=job.account_name,
            file_size=job.file_size,
            error_code=code,
            request_id=job.request_id,
            client=job.client,
            duration_ms=duration,
            details={"job_id": job.id, **details},
        )
        log_event(
            "upload.failed",
            level=30,
            job_id=job.id,
            file_name=job.file_name,
            error_code=code,
            duration_ms=round(duration, 2),
        )

    def _cleanup_temp(self, job: UploadJob) -> None:
        try:
            os.unlink(job.temp_path)
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - best effort
            log_event("upload.temp_cleanup_failed", level=30, path=job.temp_path)

    def _release_name(self, job: UploadJob) -> None:
        with self._lock:
            if self._active_names.get(job.file_name) == job.id:
                del self._active_names[job.file_name]
            self._futures.pop(job.id, None)

    # -- queries -------------------------------------------------------
    def get(self, job_id: str) -> UploadJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_for_name(self, file_name: str) -> str | None:
        with self._lock:
            return self._blocking_name_locked(file_name)

    def begin_delete(self, file_name: str) -> None:
        """Reserve a name for a synchronous delete.

        This closes the race between a delete and an upsert submission: both
        operations must claim the same per-name slot before touching B2.
        """
        with self._lock:
            if self._closed:
                raise UploadRejected("service_unavailable", "The server is shutting down.")
            existing = self._blocking_name_locked(file_name)
            if existing is not None:
                raise UploadRejected(
                    "job_active",
                    f"An operation for {file_name!r} is already in progress.",
                    file_name=file_name,
                    job_id=existing,
                )
            if file_name in self._deleting:
                raise UploadRejected(
                    "job_active",
                    f"A delete for {file_name!r} is already in progress.",
                    file_name=file_name,
                )
            self._deleting.add(file_name)

    def end_delete(self, file_name: str) -> None:
        with self._delete_done:
            self._deleting.discard(file_name)
            self._delete_done.notify_all()

    def list(
        self,
        *,
        states: set[JobState] | None = None,
        file_name: str | None = None,
        updated_since: int = 0,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = False,
    ) -> tuple[builtins.list[UploadJob], int]:
        with self._lock:
            self._prune()
            jobs = [
                job
                for job in self._jobs.values()
                if (states is None or job.state in states)
                and (file_name is None or job.file_name == file_name)
                and job.seq > updated_since
            ]
        total = len(jobs)
        jobs.sort(key=lambda job: job.seq, reverse=newest_first)
        return jobs[offset : offset + limit], total

    @property
    def revision(self) -> int:
        with self._seq_lock:
            return self._last_seq

    def jobs_snapshot(
        self,
        *,
        states: set[JobState] | None = None,
        file_name: str | None = None,
        updated_since: int = 0,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = False,
    ) -> tuple[builtins.list[UploadJob], int, int]:
        """Return jobs, total, and revision from a single consistent read."""
        with self._lock:
            self._prune()
            jobs: list[UploadJob] = [
                job
                for job in self._jobs.values()
                if (states is None or job.state in states)
                and (file_name is None or job.file_name == file_name)
                and job.seq > updated_since
            ]
            total = len(jobs)
            rev = self._last_seq
        jobs.sort(key=lambda job: job.seq, reverse=newest_first)
        return jobs[offset : offset + limit], total, rev

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.state == JobState.QUEUED)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.state in ACTIVE_STATES)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    # -- lifecycle -----------------------------------------------------
    def cancel(self, job_id: str) -> UploadJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UploadRejected("job_not_found", f"Upload job {job_id!r} was not found.")
            if not job.try_cancel():
                if job.state in ACTIVE_STATES:
                    message = "The job is already uploading and cannot be cancelled."
                else:
                    message = "The job has already finished; dismiss it instead."
                raise UploadRejected("job_active", message, job_id=job_id)
            future = self._futures.get(job_id)
            if future is not None and future.cancel():
                self._futures.pop(job_id, None)
            self._active_names.pop(job.file_name, None)
        self._cleanup_temp(job)
        self._append_audit(
            job,
            result="cancelled",
            file_name=job.file_name,
            file_size=job.file_size,
            request_id=job.request_id,
            client=job.client,
            details={"job_id": job.id},
        )
        metrics.refresh_jobs(self)
        return job

    def dismiss(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise UploadRejected("job_not_found", f"Upload job {job_id!r} was not found.")
            if job.state not in TERMINAL_STATES:
                raise UploadRejected(
                    "job_active",
                    "Only finished jobs can be dismissed.",
                    job_id=job_id,
                )
            del self._jobs[job_id]
            # Dismissal changes the list representation even though no job
            # remains to carry the new sequence number.
            self._next_seq()

    def shutdown(self) -> None:
        """Stop accepting mutations and wait for accepted work to finish.

        The process-level writer lock must not be released until this method
        returns. Running uploads may still be touching B2, and synchronous
        deletes are not part of the executor, so both must be drained separately.
        """
        with self._lock:
            self._closed = True
            queued = [job for job in self._jobs.values() if job.state == JobState.QUEUED]
            for job in queued:
                if job.try_cancel():
                    self._cleanup_temp(job)
                    future = self._futures.get(job.id)
                    if future is not None and future.cancel():
                        self._futures.pop(job.id, None)
            futures = [future for future in self._futures.values() if not future.cancelled()]

        # Do not hold _lock while waiting: workers and delete handlers need it
        # to finish their cleanup. New submissions and deletes are rejected by
        # the _closed check above.
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        else:
            wait(futures)

        with self._delete_done:
            self._delete_done.wait_for(lambda: not self._deleting)
            self._active_names.clear()
            self._futures.clear()

    def _prune(self) -> None:
        """Drop old terminal jobs; never removes queued/running work."""
        now = time.time()
        for job_id in list(self._jobs):
            job = self._jobs[job_id]
            if job.state not in TERMINAL_STATES:
                continue
            too_many = len(self._jobs) > self._settings.job_history_max
            age = now - _parse_iso(job.finished_at or job.created_at)
            if too_many or age > self._settings.job_ttl_seconds:
                del self._jobs[job_id]
                self._next_seq()


def _parse_iso(value: str | None) -> float:
    if not value:
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:  # pragma: no cover - defensive
        return time.time()


def cleanup_temp_dir(temp_dir: str) -> int:
    """Remove spool files left behind by an unclean shutdown.

    Called only after the single-writer lock is held, so every matching file is
    guaranteed to be stale.
    """
    removed = 0
    try:
        entries = os.listdir(temp_dir)
    except FileNotFoundError:
        os.makedirs(temp_dir, exist_ok=True)
        return 0
    for name in entries:
        if not name.startswith("b2router-up-"):
            continue
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(temp_dir, name))
            removed += 1
    return removed
