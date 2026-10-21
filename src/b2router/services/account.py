"""Backblaze B2 boundary.

This module is the only place that talks to the B2 SDK. Everything above it
sees ``StorageBackend``: typed file versions, stable error codes, and latency
instrumentation. That boundary is what makes the gateway testable without
credentials and keeps SDK quirks (private APIs, timeouts, retries) contained.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import requests
from b2sdk._internal.raw_api import AbstractRawApi
from b2sdk.v2 import (
    AbstractProgressListener,
    B2Api,
    B2HttpApiConfig,
    Bucket,
    InMemoryAccountInfo,
)
from b2sdk.v2 import exception as b2exc
from b2sdk.v2.b2http import B2Http
from b2sdk.v2.raw_api import B2RawHTTPApi

from b2router import metrics
from b2router.logging_setup import log_event


@dataclass(frozen=True)
class AccountCredentials:
    """Immutable credentials for one B2 account."""

    name: str
    application_key_id: str
    application_key: str
    connect_timeout: float = 30.0
    read_timeout: float = 120.0


@dataclass(frozen=True)
class FileVersion:
    """A file version as reported by Backblaze B2."""

    file_name: str
    file_id: str
    size: int
    content_type: str
    upload_timestamp: int
    action: str


class StorageError(Exception):
    """A stable, sanitised storage failure.

    ``code`` is one of the ``b2_*`` problem codes; the original SDK message is
    never surfaced to clients because it can contain account identifiers.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class DownloadHandle(Protocol):
    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class StorageBackend(Protocol):
    name: str

    def list_files(self) -> list[FileVersion]: ...

    def upload_local_file(
        self,
        local_path: str,
        file_name: str,
        content_type: str,
        progress_listener: AbstractProgressListener | None = None,
    ) -> FileVersion: ...

    def delete_version(self, file_id: str, file_name: str) -> None: ...

    def get_file_info(self, file_name: str) -> FileVersion: ...

    def download(
        self, file_id: str, file_name: str, range_: tuple[int, int] | None = None
    ) -> DownloadHandle: ...


# ---------------------------------------------------------------------------
# SDK plumbing
# ---------------------------------------------------------------------------
class _CustomB2Http(B2Http):  # type: ignore[misc]
    """B2Http with explicit connect/read timeouts."""

    def __init__(
        self, api_config: Any, connect_timeout: float = 30.0, read_timeout: float = 120.0
    ) -> None:
        self._custom_connect_timeout = connect_timeout
        self._custom_read_timeout = read_timeout
        super().__init__(api_config)

    @property
    def CONNECTION_TIMEOUT(self) -> float:  # noqa: N802 - SDK attribute name
        return self._custom_connect_timeout

    @property
    def TIMEOUT_FOR_UPLOAD(self) -> float:  # noqa: N802 - SDK attribute name
        return self._custom_read_timeout


def _session_factory(connect_timeout: float, read_timeout: float) -> Callable[[], requests.Session]:
    def factory() -> requests.Session:
        session = requests.Session()
        session.timeout = (connect_timeout, read_timeout)  # type: ignore[attr-defined]
        return session

    return factory


def _raw_api_factory(
    api_config: B2HttpApiConfig, connect_timeout: float, read_timeout: float
) -> Callable[[B2Http], AbstractRawApi]:
    def factory(_b2_http: B2Http) -> AbstractRawApi:
        return B2RawHTTPApi(
            _CustomB2Http(api_config, connect_timeout=connect_timeout, read_timeout=read_timeout)
        )

    return factory


class _ResponseHandle:
    """Thin wrapper around ``DownloadedFile`` so clients never see b2sdk types."""

    def __init__(self, downloaded: Any) -> None:
        self._downloaded = downloaded
        self._closed = False

    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        response = self._downloaded.response
        try:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):  # pragma: no cover - best effort cleanup
            self._downloaded.response.close()


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------
_RETRYABLE = (
    b2exc.B2RequestTimeout,
    b2exc.RequestTimeout,
    b2exc.B2ConnectionError,
    b2exc.ConnectionReset,
    b2exc.BrokenPipe,
    b2exc.TooManyRequests,
    b2exc.ServiceError,
    b2exc.TransientErrorMixin,
)


def _translate(exc: BaseException) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if isinstance(exc, (b2exc.FileNotPresent, b2exc.FileAlreadyHidden)):
        return StorageError("b2_not_found", "The file no longer exists in B2.")
    if isinstance(exc, (b2exc.InvalidRange, b2exc.UnsatisfiableRange)):
        return StorageError("range_not_satisfiable", "The requested byte range is not available.")
    if isinstance(exc, (b2exc.TooManyRequests, b2exc.CapExceeded, b2exc.TransactionCapExceeded)):
        return StorageError("b2_rate_limited", "B2 rate limited the request.", retryable=True)
    if isinstance(
        exc,
        (
            b2exc.Unauthorized,
            b2exc.InvalidAuthToken,
            b2exc.AccessDenied,
            b2exc.NotAllowedByAppKeyError,
        ),
    ):
        return StorageError("b2_unauthorized", "B2 rejected the account credentials.")
    if isinstance(exc, (b2exc.B2RequestTimeout, b2exc.RequestTimeout)):
        return StorageError("b2_timeout", "B2 did not answer in time.", retryable=True)
    if isinstance(exc, _RETRYABLE):
        return StorageError("b2_unavailable", "B2 is temporarily unavailable.", retryable=True)
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return StorageError("b2_timeout", "B2 did not answer in time.", retryable=True)
    if isinstance(exc, requests.RequestException):
        return StorageError("b2_unavailable", "Could not reach B2.", retryable=True)
    return StorageError("b2_error", "B2 rejected the operation.")


# ---------------------------------------------------------------------------
# Account client
# ---------------------------------------------------------------------------
class AccountClient:
    """Effectful B2 client with lazy authorization and bounded retries."""

    def __init__(self, credentials: AccountCredentials) -> None:
        self.credentials = credentials
        self.name = credentials.name
        self._api: B2Api | None = None
        self._bucket: Bucket | None = None
        self._bucket_name = ""
        self._lock = threading.Lock()

    # -- setup ---------------------------------------------------------
    def _create_api(self) -> B2Api:
        config = B2HttpApiConfig(
            http_session_factory=_session_factory(
                self.credentials.connect_timeout, self.credentials.read_timeout
            )
        )
        config.raw_api_class = _raw_api_factory(
            config, self.credentials.connect_timeout, self.credentials.read_timeout
        )
        return B2Api(InMemoryAccountInfo(), api_config=config)

    def authorize(self) -> None:
        with self._lock:
            if self._api is not None and self._bucket is not None:
                return
            try:
                api = self._create_api()
                api.authorize_account(
                    "production",
                    self.credentials.application_key_id,
                    self.credentials.application_key,
                )
                buckets = api.list_buckets()
                if len(buckets) != 1:
                    raise StorageError(
                        "b2_error",
                        f"Account {self.name!r} must expose exactly one bucket "
                        f"(found {len(buckets)}).",
                    )
                self._api = api
                self._bucket = buckets[0]
                self._bucket_name = buckets[0].name
            except BaseException as exc:
                raise _translate(exc) from exc

    @property
    def bucket(self) -> Bucket:
        if self._bucket is None:
            self.authorize()
        assert self._bucket is not None
        return self._bucket

    @property
    def bucket_name(self) -> str:
        if not self._bucket_name:
            self.authorize()
        return self._bucket_name

    # -- operations ----------------------------------------------------
    def _observe(
        self, operation: str, started: float, outcome: str, error_code: str | None = None
    ) -> None:
        duration = time.monotonic() - started
        metrics.record_b2(operation, duration)
        if error_code:
            metrics.record_b2_error(operation, error_code)
        log_event(
            "b2.request",
            operation=operation,
            account=self.name,
            duration_ms=round(duration * 1000, 2),
            outcome=outcome,
            error_code=error_code,
        )

    def list_files(self) -> list[FileVersion]:
        started = time.monotonic()
        try:
            files = [self._to_version(version) for version, _ in self.bucket.ls(latest_only=True)]
            self._observe("list_files", started, "ok")
            return files
        except BaseException as exc:
            error = _translate(exc)
            self._observe("list_files", started, "error", error.code)
            raise error from exc

    def list_versions(self) -> list[FileVersion]:
        """Every stored upload version, including superseded ones.

        Used by ``b2router-reconcile`` to find orphaned versions that a
        latest-only cache build cannot see.
        """
        started = time.monotonic()
        try:
            versions = [
                self._to_version(version)
                for version, _ in self.bucket.ls(latest_only=False)
                if version.action == "upload"
            ]
            self._observe("list_versions", started, "ok")
            return versions
        except BaseException as exc:
            error = _translate(exc)
            self._observe("list_versions", started, "error", error.code)
            raise error from exc

    @staticmethod
    def _to_version(version: Any) -> FileVersion:
        return FileVersion(
            file_name=version.file_name,
            file_id=version.id_,
            size=version.size,
            content_type=version.content_type,
            upload_timestamp=version.upload_timestamp,
            action=version.action,
        )

    def get_file_info(self, file_name: str) -> FileVersion:
        started = time.monotonic()
        try:
            info = self.bucket.get_file_info_by_name(file_name)
            self._observe("get_file_info", started, "ok")
            return FileVersion(
                file_name=info.file_name,
                file_id=info.id_,
                size=info.size,
                content_type=info.content_type,
                upload_timestamp=info.upload_timestamp,
                action=info.action or "upload",
            )
        except BaseException as exc:
            error = _translate(exc)
            self._observe("get_file_info", started, "error", error.code)
            raise error from exc

    def upload_local_file(
        self,
        local_path: str,
        file_name: str,
        content_type: str,
        progress_listener: AbstractProgressListener | None = None,
    ) -> FileVersion:
        started = time.monotonic()
        try:
            version = self.bucket.upload_local_file(
                local_path,
                file_name,
                content_type=content_type,
                progress_listener=progress_listener,
            )
            self._observe("upload", started, "ok")
            return FileVersion(
                file_name=version.file_name,
                file_id=version.id_,
                size=version.size,
                content_type=version.content_type,
                upload_timestamp=version.upload_timestamp,
                action=version.action or "upload",
            )
        except BaseException as exc:
            error = _translate(exc)
            self._observe("upload", started, "error", error.code)
            raise error from exc

    def delete_version(self, file_id: str, file_name: str) -> None:
        started = time.monotonic()
        try:
            self.bucket.delete_file_version(file_id, file_name)
            self._observe("delete", started, "ok")
        except BaseException as exc:
            error = _translate(exc)
            self._observe("delete", started, "error", error.code)
            raise error from exc

    def download(
        self, file_id: str, file_name: str, range_: tuple[int, int] | None = None
    ) -> DownloadHandle:
        started = time.monotonic()
        try:
            downloaded = self.bucket.download_file_by_id(file_id, range_=range_)
            self._observe("download", started, "ok")
            return _ResponseHandle(downloaded)
        except BaseException as exc:
            error = _translate(exc)
            self._observe("download", started, "error", error.code)
            raise error from exc
