"""Common utilities for move and copy commands."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import Account, SourceFile
from ..utils import format_bytes
from ..b2_client import get_b2_client
from ..hashing import compute_sha1
from ..retry import retry_with_backoff, run_with_timeout

if TYPE_CHECKING:
    from ..models import Bucket


def print_allocation_plan(
    allocation: dict[Path, tuple['Bucket', str]],
    source_files: list[SourceFile],
    accounts: dict[str, Account],
    suffix: str = ""
) -> None:
    """Print the allocation plan grouped by account."""
    # Build abs_path -> (rel_path, size) lookup
    file_info_map = {f.abs_path: (f.rel_path, f.size) for f in source_files}

    # Group by account for display
    account_allocations: dict[str, list[tuple[Path, 'Bucket', str]]] = {}
    for abs_path, (bucket, object_name) in allocation.items():
        if bucket.account is None:
            raise ValueError(f"Bucket {bucket.name} has no associated account")
        acc_name = bucket.account.name
        if acc_name not in account_allocations:
            account_allocations[acc_name] = []
        account_allocations[acc_name].append((abs_path, bucket, object_name))

    total_allocated = 0
    total_allocated_size = 0

    print(f"\n{'─' * 60}")
    print(f"📋  ALLOCATION PLAN{suffix}")
    print(f"{'─' * 60}")

    for acc_name, items in account_allocations.items():
        acc_size = sum(file_info_map[item[0]][1] for item in items)
        print(f"\n  📦  Account: {acc_name} ({len(items)} files, {format_bytes(acc_size)})")
        total_allocated += len(items)
        total_allocated_size += acc_size
        for abs_path, bucket, object_name in items[:10]:
            rel_path, size = file_info_map[abs_path]
            size_str = format_bytes(size)
            print(f"      📄  {rel_path} ({size_str}) → {bucket.name}/{object_name}")
        if len(items) > 10:
            remaining_size = sum(file_info_map[item[0]][1] for item in items[10:])
            print(f"      … and {len(items) - 10} more files ({format_bytes(remaining_size)})")

    print(f"\n  ✅  Total allocated: {total_allocated}/{len(source_files)} files ({format_bytes(total_allocated_size)})")
    if total_allocated < len(source_files):
        skipped = len(source_files) - total_allocated
        skipped_size = sum(f.size for f in source_files) - total_allocated_size
        print(f"  ⚠️  Skipped: {skipped} files ({format_bytes(skipped_size)}) - insufficient capacity")


def filter_existing_files(
    allocation: dict[Path, tuple['Bucket', str]],
    accounts: dict[str, Account],
    source_files: list[SourceFile],
) -> dict[Path, tuple['Bucket', str]]:
    """Filter out files that already exist in B2 with matching SHA-1."""
    print(f"\n🔍  Checking for existing files in B2 (--skip-existing)...")
    # Note: build_account_state was already called, reuse existing bucket data
    filtered_allocation = {}
    skipped_count = 0
    skipped_size = 0
    for abs_path, (bucket, object_name) in allocation.items():
        if bucket._populate_failed:
            filtered_allocation[abs_path] = (bucket, object_name)
            continue
        if bucket.account is None:
            filtered_allocation[abs_path] = (bucket, object_name)
            continue
        client = get_b2_client(bucket.account)
        # Check using the object_name that will be used for upload (not rel_path)
        def _get_file_info():
            b2_bucket = client.get_bucket_by_id(bucket.id_)
            return b2_bucket.get_file_info_by_name(object_name)
        existing_file = None
        try:
            existing_file = retry_with_backoff(run_with_timeout, _get_file_info)
        except Exception:
            pass
        if existing_file is not None:
            remote_sha1 = existing_file.content_sha1
            local_sha1 = compute_sha1(abs_path)
            if remote_sha1 is not None and remote_sha1 == local_sha1:
                logging.info(f"Skipping {object_name} - already exists with matching SHA-1")
                skipped_count += 1
                # Use size from source_files lookup
                size = next((f.size for f in source_files if f.abs_path == abs_path), abs_path.stat().st_size)
                skipped_size += size
                continue
        filtered_allocation[abs_path] = (bucket, object_name)
    allocation = filtered_allocation
    print(f"  ⏭️  Skipped: {skipped_count} files ({format_bytes(skipped_size)})")
    print(f"  📤  Remaining: {len(allocation)} files")
    return allocation