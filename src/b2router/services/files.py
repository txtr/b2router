"""Shared file mutations used by the JSON API and the server-rendered UI.

Keeping delete in one place means the per-name claim, B2 call, cache commit,
audit entry, and metrics are identical no matter which surface invoked them.
"""

from __future__ import annotations

import time

from b2router import metrics
from b2router.errors import Problem
from b2router.logging_setup import log_event, request_id_var
from b2router.runtime import Runtime
from b2router.services.account import StorageError
from b2router.services.pool import FileEntry
from b2router.services.uploads import UploadRejected


def delete_file(
    rt: Runtime,
    file_name: str,
    *,
    expected_file_id: str | None = None,
    request_id: str | None = None,
    client: str | None = None,
) -> FileEntry:
    """Delete a file synchronously while holding the per-name claim.

    Raises ``Problem`` for every expected failure, so both HTTP surfaces can
    translate it consistently.
    """
    try:
        rt.manager.begin_delete(file_name)
    except UploadRejected as exc:
        raise Problem(exc.code, exc.message, extensions=exc.details) from exc

    started = time.monotonic()
    try:
        entry = rt.pool.get_file(file_name)
        if entry is None:
            raise Problem("file_not_found", f"File {file_name!r} is not in the pool.", status=404)

        if (
            expected_file_id is not None
            and expected_file_id != "*"
            and expected_file_id != entry.file_id
        ):
            raise Problem(
                "precondition_failed",
                "The file changed since it was read; refresh and retry.",
                status=412,
                extensions={
                    "expected_file_id": expected_file_id,
                    "actual_file_id": entry.file_id,
                },
            )

        account = rt.pool.get(entry.accounts[0])
        if account is None:
            raise Problem(
                "account_not_found",
                "The owning account is no longer in the pool.",
                status=404,
            )

        rid = request_id or request_id_var.get()
        try:
            account.delete_version(entry.file_id, entry.file_name)
        except StorageError as exc:
            if exc.code != "b2_not_found":
                metrics.DELETES.labels(result="failed").inc()
                rt.audit.append(
                    "delete",
                    result="failed",
                    file_name=file_name,
                    account_name=entry.accounts[0],
                    file_size=entry.size,
                    error_code=exc.code,
                    request_id=rid,
                    client=client,
                    duration_ms=(time.monotonic() - started) * 1000,
                    details={"file_id": entry.file_id},
                )
                status = {
                    "b2_unauthorized": 502,
                    "b2_rate_limited": 503,
                    "b2_unavailable": 503,
                    "b2_timeout": 504,
                }.get(exc.code, 502)
                raise Problem(exc.code, exc.message, status=status) from exc

        deleted = rt.pool.commit_delete(file_name, expected_file_id=entry.file_id)
        metrics.DELETES.labels(result="success").inc()
        metrics.refresh_pool(rt.pool)
        rt.audit.append(
            "delete",
            result="success",
            file_name=file_name,
            account_name=deleted.accounts[0],
            file_size=deleted.size,
            request_id=rid,
            client=client,
            duration_ms=(time.monotonic() - started) * 1000,
            details={"file_id": deleted.file_id},
        )
        log_event(
            "file.deleted",
            file_name=file_name,
            account=deleted.accounts[0],
            size=deleted.size,
        )
        return deleted
    finally:
        rt.manager.end_delete(file_name)
