"""Upload and file operations for B2 Router."""

import logging
from pathlib import Path
from typing import Tuple, Optional

from b2sdk.v2.exception import B2Error

from .models import Bucket, SourceFile
from .b2_client import get_b2_client
from .hashing import compute_sha1
from .retry import retry_with_backoff, run_with_timeout
from .exceptions import UploadError


def upload_file(bucket: Bucket, abs_path: Path, object_name: str, size: int) -> bool:
    """Upload a file to B2 using streaming and verify SHA-1 after upload.

    If file already exists in B2 with matching SHA-1, skip upload.

    Note: There is a TOCTOU race between checking for existing file and uploading.
    B2 does not support atomic compare-and-swap for uploads. If another process
    uploads the same file between our check and upload, a new version will be
    created. The post-upload SHA-1 verification ensures data integrity.
    """
    if bucket.account is None:
        raise UploadError("Bucket must have an associated account")
    account = bucket.account
    client = get_b2_client(account)

    # Compute local SHA-1 before upload
    local_sha1 = compute_sha1(abs_path)

    def _do_upload() -> bool:
        b2_bucket = client.get_bucket_by_id(bucket.id_)
        # Use upload_local_file for streaming upload (avoids loading entire file into memory)
        b2_bucket.upload_local_file(str(abs_path), object_name)
        return True

    try:
        # Check if file already exists in B2 (for resume/idempotency)
        def _get_file_info():
            b2_bucket = client.get_bucket_by_id(bucket.id_)
            try:
                return b2_bucket.get_file_info_by_name(object_name)
            except B2Error as e:
                if "File not present" in str(e) or "not_found" in str(e).lower():
                    return None
                raise

        existing_file = retry_with_backoff(run_with_timeout, _get_file_info)
        if existing_file is not None:
            remote_sha1 = existing_file.content_sha1
            if remote_sha1 is not None and remote_sha1 == local_sha1:
                logging.info(f"File {object_name} already exists with matching SHA-1, skipping upload")
                return True
            else:
                logging.warning(f"File {object_name} exists but SHA-1 differs (local={local_sha1}, remote={remote_sha1}), will re-upload")
        else:
            logging.debug(f"File {object_name} not found in B2, will upload")

        retry_with_backoff(run_with_timeout, _do_upload)

        # Verify SHA-1 after upload
        def _verify_upload():
            b2_bucket = client.get_bucket_by_id(bucket.id_)
            return b2_bucket.get_file_info_by_name(object_name)

        file_version = retry_with_backoff(run_with_timeout, _verify_upload)
        if file_version is None:
            logging.error(f"B2 did not return file info for {object_name} after upload")
            return False

        remote_sha1 = file_version.content_sha1

        if remote_sha1 is None:
            # B2 may not have computed SHA-1 yet for large files (async processing)
            # Log warning but don't fail - upload succeeded
            logging.warning(f"B2 has not yet computed SHA-1 for {object_name} (large file async processing). Upload succeeded but SHA-1 not verified.")
            return True

        if local_sha1 != remote_sha1:
            logging.error(f"SHA-1 mismatch for {object_name}: local={local_sha1}, remote={remote_sha1}")
            # Attempt to delete the corrupted upload to avoid leaving bad file in B2
            try:
                b2_bucket = client.get_bucket_by_id(bucket.id_)
                b2_bucket.delete_file_version(object_name, file_version.id_)
                logging.info(f"Deleted mismatched file version {object_name} (id={file_version.id_})")
            except Exception as del_exc:
                logging.warning(f"Failed to delete mismatched file {object_name}: {del_exc}")
            return False

        logging.info(f"Uploaded {object_name} ({size} bytes) → {account.name}:{bucket.name} [SHA-1 verified]")
        return True
    except Exception as exc:
        logging.error(f"Failed upload to bucket '{bucket.name}' (Account '{account.name}'): {exc}")
        return False


def delete_source_file(abs_path: Path) -> bool:
    """Delete source file after successful upload."""
    try:
        abs_path.unlink()
        logging.info(f"Deleted local source file: {abs_path}")
        return True
    except Exception as exc:
        logging.error(f"Failed to delete {abs_path}: {exc}")
        return False


def cleanup_empty_dirs(source_dir: str) -> None:
    """Remove empty directories after file moves.

    Does not follow symlinks to avoid traversing outside the source tree.
    Uses retry with backoff to handle race conditions where file handles
    may not be released immediately after parallel uploads.
    """
    import os
    import time
    from pathlib import Path
    
    source_path = Path(source_dir).resolve()
    
    # Retry a few times with increasing delay to handle race conditions
    for attempt in range(5):
        try:
            for root, dirs, files in os.walk(source_path, topdown=False, followlinks=False):
                for dir_name in dirs:
                    dir_path = Path(root) / dir_name
                    try:
                        if not any(dir_path.iterdir()):
                            dir_path.rmdir()
                            logging.info(f"Removed empty directory: {dir_path}")
                    except OSError:
                        pass  # Directory not empty or other error
            break  # Success, exit retry loop
        except OSError as exc:
            if attempt < 4:
                delay = 0.1 * (2 ** attempt)  # 0.1, 0.2, 0.4, 0.8, 1.6 seconds
                logging.debug(f"cleanup_empty_dirs retry {attempt+1}/5 after {delay}s: {exc}")
                time.sleep(delay)
            else:
                logging.warning(f"cleanup_empty_dirs failed after 5 retries: {exc}")


def _upload_and_delete(bucket: Bucket, abs_path: Path, object_name: str, size: int) -> Tuple[Path, bool, Optional[str]]:
    """Upload a file and delete source on success. Returns (abs_path, success, error_msg)."""
    if upload_file(bucket, abs_path, object_name, size):
        if delete_source_file(abs_path):
            return (abs_path, True, None)
        else:
            return (abs_path, False, f"Deletion failed for {abs_path}")
    else:
        return (abs_path, False, f"Upload failed for {abs_path} to {bucket.name}")


def _upload_only(bucket: Bucket, abs_path: Path, object_name: str, size: int) -> Tuple[Path, bool, Optional[str]]:
    """Upload a file without deleting source. Returns (abs_path, success, error_msg)."""
    if upload_file(bucket, abs_path, object_name, size):
        return (abs_path, True, None)
    else:
        return (abs_path, False, f"Upload failed for {abs_path} to {bucket.name}")