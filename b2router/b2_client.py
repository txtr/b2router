"""Simplified B2 client for b2router_simple."""

import logging
from dataclasses import dataclass
from typing import List, Optional, Iterator, Tuple

from b2sdk.v2 import InMemoryAccountInfo, B2Api
from b2sdk.v2.exception import B2Error

logger = logging.getLogger(__name__)


@dataclass
class Bucket:
    account_name: str
    bucket_name: str
    bucket_id: str
    capacity_bytes: int
    used_bytes: int = 0


def authorize_account(account_id: str, master_key: str) -> B2Api:
    """Authorize and return B2Api instance."""
    info = InMemoryAccountInfo()
    api = B2Api(info)
    api.authorize_account("production", account_id, master_key)
    return api


def get_bucket(api: B2Api, capacity_gb: float = 10.0) -> Tuple[str, str, int]:
    """Get the first bucket for the account, return (bucket_id, bucket_name, capacity_bytes)."""
    buckets = list(api.list_buckets())
    if not buckets:
        raise ValueError("No buckets found for this account")
    # Use the first bucket (assuming single bucket per account)
    bucket = buckets[0]
    capacity = int(capacity_gb * 1024 * 1024 * 1024)
    return bucket.id_, bucket.name, capacity


def list_files_in_bucket(api: B2Api, bucket_id: str) -> Iterator[Tuple[str, int]]:
    """Yield (file_name, size) for all files in bucket."""
    bucket = api.get_bucket_by_id(bucket_id)
    for file_version, _ in bucket.ls(latest_only=True):
        yield file_version.file_name, file_version.size


def upload_file(api: B2Api, bucket_id: str, local_path: str, object_name: str) -> bool:
    """Upload a file to B2. Returns True on success."""
    try:
        bucket = api.get_bucket_by_id(bucket_id)
        bucket.upload_local_file(local_path, object_name)
        return True
    except B2Error as e:
        logger.error(f"Upload failed for {object_name}: {e}")
        return False


def delete_file(api: B2Api, bucket_id: str, file_name: str, file_id: str) -> bool:
    """Delete a file version from B2."""
    try:
        bucket = api.get_bucket_by_id(bucket_id)
        bucket.delete_file_version(file_id, file_name)
        return True
    except B2Error as e:
        logger.error(f"Delete failed for {file_name}: {e}")
        return False


def get_file_info(api: B2Api, bucket_id: str, file_name: str) -> Optional[Tuple[str, int]]:
    """Get (file_id, size) for a file if it exists."""
    try:
        bucket = api.get_bucket_by_id(bucket_id)
        file_version = bucket.get_file_info_by_name(file_name)
        return file_version.id_, file_version.size
    except B2Error as e:
        if "File not present" in str(e) or "not_found" in str(e).lower():
            return None
        raise


def build_buckets(accounts) -> List[Bucket]:
    """Authorize all accounts and discover their buckets."""
    buckets = []
    for acc in accounts:
        api = authorize_account(acc.account_id, acc.master_key)
        bucket_id, bucket_name, capacity = get_bucket(api, acc.capacity_gb)
        buckets.append(Bucket(
            account_name=acc.name,
            bucket_name=bucket_name,
            bucket_id=bucket_id,
            capacity_bytes=capacity,
        ))
    return buckets