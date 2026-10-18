from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from b2sdk.v2 import InMemoryAccountInfo, B2Api, Bucket, B2HttpApiConfig


def _create_session_factory(connect_timeout: float, read_timeout: float) -> Callable[[], requests.Session]:
    """Create a session factory that produces sessions with custom timeouts."""
    def factory() -> requests.Session:
        session = requests.Session()
        session.timeout = (connect_timeout, read_timeout)
        return session
    return factory


@dataclass
class Account:
    """Encapsulates B2 API client and bucket reference."""

    application_key_id: str
    application_key: str
    name: str = field(default="", init=True, repr=True)  # Account name from pool
    api: B2Api = field(init=False, repr=False)
    bucket: Bucket | None = field(default=None, init=False, repr=False)
    bucket_name: str = field(default="", init=False, repr=False)
    _total_size: int = field(default=0, init=False, repr=False)
    _file_count: int = field(default=0, init=False, repr=False)

    # Configurable timeouts (seconds)
    connect_timeout: float = field(default=30.0, init=True, repr=False)
    read_timeout: float = field(default=120.0, init=True, repr=False)

    def __post_init__(self) -> None:
        # Create API config with custom session factory for timeouts
        api_config = B2HttpApiConfig(
            http_session_factory=_create_session_factory(self.connect_timeout, self.read_timeout),
        )
        self.api = B2Api(InMemoryAccountInfo(), api_config=api_config)
        self.api.authorize_account("production", self.application_key_id, self.application_key)
        self._fetch_bucket()

    def _fetch_bucket(self) -> None:
        """Fetch the single bucket for this account."""
        buckets = self.api.list_buckets()
        assert len(buckets) == 1, f"Expected exactly 1 bucket, found {len(buckets)}"
        self.bucket = buckets[0]
        self.bucket_name = buckets[0].name

    def list_files(self) -> list[dict[str, Any]]:
        """List all files in the bucket and update cached totals."""
        assert self.bucket is not None, "Bucket not initialized"
        files = []
        total_size = 0
        for file_version, _ in self.bucket.ls(latest_only=True):
            size = file_version.size
            total_size += size
            files.append(
                {
                    "file_name": file_version.file_name,
                    "file_id": file_version.id_,
                    "size": size,
                    "content_type": file_version.content_type,
                    "upload_timestamp": file_version.upload_timestamp,
                    "action": file_version.action,
                }
            )
        self._total_size = total_size
        self._file_count = len(files)
        return files

    @property
    def total_size(self) -> int:
        """Total size of all files in this account's bucket (cached after list_files)."""
        return self._total_size

    @property
    def file_count(self) -> int:
        """Number of files in this account's bucket (cached after list_files)."""
        return self._file_count