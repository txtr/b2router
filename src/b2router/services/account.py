from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from b2sdk.v2 import InMemoryAccountInfo, B2Api, Bucket
from b2sdk.v2.exception import FileNotPresent

from b2router.core.config import settings
from b2router.core.logging import logger


@dataclass
class Account:
    """Encapsulates B2 API client and caches account state."""

    application_key_id: str
    application_key: str
    api: B2Api = field(init=False, repr=False)
    cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.api = B2Api(InMemoryAccountInfo())
        self.api.authorize_account("production", self.application_key_id, self.application_key)
        self._enrich_cache()

    def _enrich_cache(self) -> None:
        """Fetch buckets, assert single bucket, and cache bucket + file metadata."""
        buckets = self.api.list_buckets()
        if len(buckets) != 1:
            raise ValueError(f"Expected exactly 1 bucket, found {len(buckets)}")
        bucket = buckets[0]
        self.cache["bucket"] = {
            "id": bucket.id_,
            "name": bucket.name,
            "type": bucket.type_,
        }
        files = []
        for file_version, _ in bucket.ls(latest_only=True):
            files.append(
                {
                    "file_name": file_version.file_name,
                    "file_id": file_version.id_,
                    "size": file_version.size,
                    "content_type": file_version.content_type,
                    "upload_timestamp": file_version.upload_timestamp,
                    "action": file_version.action,
                }
            )
        self.cache["files"] = files
        self.cache["file_count"] = len(files)
        self.cache["total_size"] = sum(f["size"] for f in files)
        logger.info("account_cache_enriched", bucket=bucket.name, file_count=len(files))

    def get_bucket(self) -> Bucket:
        """Return the cached bucket, fetching if needed."""
        if "bucket" not in self.cache:
            self._enrich_cache()
        bucket_info = self.cache["bucket"]
        return self.api.get_bucket_by_id(bucket_info["id"])

    def refresh_cache(self) -> None:
        """Re-fetch bucket and file list."""
        self.cache.clear()
        self._enrich_cache()

    def get_file_info(self, file_name: str) -> dict | None:
        """Get cached file info by name."""
        for f in self.cache.get("files", []):
            if f["file_name"] == file_name:
                return f
        return None

    def __getitem__(self, key: str) -> Any:
        return self.cache[key]

    def __contains__(self, key: str) -> bool:
        return key in self.cache

    def get(self, key: str, default: Any = None) -> Any:
        return self.cache.get(key, default)


def create_account() -> Account:
    """Factory to create Account from settings."""
    return Account(
        application_key_id=settings.b2_application_key_id,
        application_key=settings.b2_application_key,
    )