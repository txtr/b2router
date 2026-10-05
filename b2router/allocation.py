"""Allocation algorithm - efficient bin packing for minimal free space.

Uses a greedy best-fit decreasing algorithm:
1. Sort files by size descending
2. For each file, place in the bucket with least remaining space that fits
3. If no bucket fits, skip (file too large)
"""

from dataclasses import dataclass
from typing import List, Dict, Tuple
from pathlib import Path

from .b2_client import Bucket
from .utils import SourceFile


@dataclass
class AllocationEntry:
    source_file: SourceFile
    bucket: Bucket
    object_name: str


def allocate_files(
    source_files: List[SourceFile],
    buckets: List[Bucket]
) -> List[AllocationEntry]:
    """Allocate files to buckets using best-fit decreasing.

    Args:
        source_files: Files to allocate
        buckets: Available buckets with capacity and current usage

    Returns:
        List of AllocationEntry mapping each file to a bucket
    """
    # Sort files by size descending (largest first for better packing)
    sorted_files = sorted(source_files, key=lambda f: f.size, reverse=True)

    # Track remaining capacity per bucket (keyed by bucket_id)
    bucket_remaining = {b.bucket_id: b.capacity_bytes - b.used_bytes for b in buckets}
    bucket_map = {b.bucket_id: b for b in buckets}

    allocations = []

    for file in sorted_files:
        # Find bucket with least remaining space that can fit this file
        best_bucket_id = None
        best_remaining = float('inf')

        for bucket_id, remaining in bucket_remaining.items():
            if remaining >= file.size and remaining < best_remaining:
                best_bucket_id = bucket_id
                best_remaining = remaining

        if best_bucket_id is None:
            # File too large for any bucket - skip with warning
            import logging
            logging.warning(f"File {file.rel_path} ({file.size} bytes) too large for any bucket, skipping")
            continue

        # Allocate to best bucket
        best_bucket = bucket_map[best_bucket_id]
        object_name = file.rel_path
        allocations.append(AllocationEntry(
            source_file=file,
            bucket=best_bucket,
            object_name=object_name,
        ))
        bucket_remaining[best_bucket_id] -= file.size

    return allocations


def print_allocation_plan(
    allocations: List[AllocationEntry],
    source_files: List[SourceFile],
    buckets: List[Bucket]
) -> None:
    """Print human-readable allocation plan grouped by account."""
    from .utils import format_bytes

    total_allocated = sum(a.source_file.size for a in allocations)
    total_files = len(allocations)

    print(f"\n{'=' * 60}")
    print("ALLOCATION PLAN")
    print(f"{'=' * 60}")
    print(f"\nTotal files: {total_files} ({format_bytes(total_allocated)})")

    # Group by account
    by_account: Dict[str, List[AllocationEntry]] = {}
    for a in allocations:
        by_account.setdefault(a.bucket.account_name, []).append(a)

    for acc_name in sorted(by_account.keys()):
        entries = by_account[acc_name]
        acc_size = sum(e.source_file.size for e in entries)
        bucket = entries[0].bucket
        remaining = bucket.capacity_bytes - bucket.used_bytes - acc_size

        print(f"\n  Account: {acc_name} ({len(entries)} files, {format_bytes(acc_size)})")
        print(f"    Bucket: {bucket.bucket_name} | Remaining after: {format_bytes(remaining)}")

        for e in entries:
            print(f"    {e.source_file.rel_path} ({format_bytes(e.source_file.size)})")

    # Show skipped files
    allocated_paths = {a.source_file.rel_path for a in allocations}
    skipped = [f for f in source_files if f.rel_path not in allocated_paths]
    if skipped:
        print(f"\n  Skipped (too large): {len(skipped)} files")
        for f in skipped:
            print(f"    {f.rel_path} ({format_bytes(f.size)})")