"""File allocation algorithm for B2 Router."""

import logging
from pathlib import Path
from typing import Dict, Tuple, List

from .models import SourceFile, Account, Bucket


CAP_SAFETY = 0.99  # stay 1% under capacity


def get_unique_object_name(bucket: Bucket, base_name: str) -> str:
    """Generate a unique object name by appending 'Copy of (N)' prefix if needed."""
    if not bucket.has_file(base_name):
        # Validate length even for original name
        if len(base_name.encode('utf-8')) > 1024:
            raise ValueError(f"Object name too long (>1024 bytes): {base_name[:50]}...")
        return base_name
    counter = 1
    max_attempts = 10000  # Prevent infinite loop
    while counter <= max_attempts:
        candidate = f"Copy of ({counter}) {base_name}"
        # Validate length after adding prefix
        if len(candidate.encode('utf-8')) > 1024:
            counter += 1
            continue
        if not bucket.has_file(candidate):
            return candidate
        counter += 1
    raise RuntimeError(f"Could not generate unique name for {base_name} after {max_attempts} attempts")


def allocate_files(source_files: list[SourceFile], accounts: dict[str, Account]) -> dict[Path, tuple[Bucket, str]]:
    """Allocate files to buckets using greedy best-fit algorithm with account-level capacity.

    Skips buckets where population failed to avoid over-allocation.
    Respects account total capacity limit across all buckets.
    """
    # Build list of valid buckets with their accounts
    # Track account_remaining per account (shared across buckets)
    account_remaining_map: dict[str, int] = {}  # account_name -> remaining bytes
    bucket_info: list[tuple[Bucket, int, str]] = []  # list of (bucket, bucket_remaining, account_name)
    for account in accounts.values():
        # Calculate total used space across all buckets in this account
        account_used = sum(b.used_bytes for b in account.buckets if not b._populate_failed)
        account_capacity = int(account.capacity_in_gb * (1024 ** 3) * CAP_SAFETY)
        account_remaining_map[account.name] = account_capacity - account_used

        for bucket in account.buckets:
            if bucket._populate_failed:
                logging.warning(f"Skipping bucket {bucket.name} (account {account.name}) - population failed, cannot determine available space")
                continue
            bucket_remaining = bucket.remaining_bytes
            bucket_info.append((bucket, bucket_remaining, account.name))

    sorted_files = sorted(source_files, key=lambda x: x.size, reverse=True)
    allocation: dict[Path, tuple[Bucket, str]] = {}

    for src_file in sorted_files:
        best_idx = None
        best_remaining = None

        for i, (bucket, bucket_remaining, account_name) in enumerate(bucket_info):
            # Use the more restrictive limit (shared account_remaining)
            account_remaining = account_remaining_map[account_name]
            effective_remaining = min(bucket_remaining, account_remaining)

            if effective_remaining >= src_file.size:
                if best_remaining is None or effective_remaining < best_remaining:
                    best_remaining = effective_remaining
                    best_idx = i

        if best_idx is not None:
            bucket, bucket_remaining, account_name = bucket_info[best_idx]
            object_name = get_unique_object_name(bucket, src_file.rel_path)
            allocation[src_file.abs_path] = (bucket, object_name)
            # Update local tracking (both bucket and account level)
            bucket_info[best_idx] = (
                bucket,
                bucket_remaining - src_file.size,
                account_name
            )
            account_remaining_map[account_name] -= src_file.size
        else:
            logging.warning(f"Skipping {src_file.rel_path} ({src_file.size} bytes) – no bucket has enough free space")
    return allocation