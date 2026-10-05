"""Copy command for b2router_simple - sequential upload, no deletion."""

import logging
from ..b2_client import build_buckets, upload_file, get_file_info
from ..allocation import allocate_files, print_allocation_plan
from ..utils import collect_source_files, format_bytes


def run_copy(accounts, source_dir: str) -> int:
    """Copy files from source_dir to B2 buckets."""
    # Collect source files
    source_files = collect_source_files(source_dir)
    if not source_files:
        print("No files found in source directory")
        return 0

    print(f"Found {len(source_files)} files ({format_bytes(sum(f.size for f in source_files))})")

    # Build buckets and get current usage
    buckets = build_buckets(accounts)
    for bucket in buckets:
        api = __import__('b2sdk.v2', fromlist=['B2Api']).B2Api(
            __import__('b2sdk.v2', fromlist=['InMemoryAccountInfo']).InMemoryAccountInfo()
        )
        acc = next(a for a in accounts if a.name == bucket.account_name)
        api.authorize_account("production", acc.account_id, acc.master_key)

        # Get current usage
        used = sum(s for _, s in list_files_in_bucket(api, bucket.bucket_id))
        bucket.used_bytes = used

    # Allocate
    allocations = allocate_files(source_files, buckets)
    if not allocations:
        print("No files could be allocated (all too large)")
        return 0

    print_allocation_plan(allocations, source_files, buckets)

    # Execute uploads sequentially
    print("\nStarting upload...")
    success = 0
    failed = 0

    # Group allocations by bucket to reuse API connections
    by_bucket: dict[str, list] = {}
    for a in allocations:
        by_bucket.setdefault(a.bucket.bucket_id, []).append(a)

    for bucket_id, entries in by_bucket.items():
        bucket = entries[0].bucket
        acc = next(a for a in accounts if a.name == bucket.account_name)
        api = __import__('b2sdk.v2', fromlist=['B2Api']).B2Api(
            __import__('b2sdk.v2', fromlist=['InMemoryAccountInfo']).InMemoryAccountInfo()
        )
        api.authorize_account("production", acc.account_id, acc.master_key)

        for i, entry in enumerate(entries, 1):
            # Check if file already exists in B2
            existing = get_file_info(api, bucket.bucket_id, entry.object_name)
            if existing:
                print(f"[{i}/{len(entries)}] ⊘ {entry.object_name} (already exists)")
                success += 1
                continue

            print(f"[{i}/{len(entries)}] ↑ {entry.object_name} ({format_bytes(entry.source_file.size)})")
            if upload_file(api, bucket.bucket_id, str(entry.source_file.path), entry.object_name):
                print(f"  ✓")
                success += 1
            else:
                print(f"  ✗ FAILED")
                failed += 1

    print(f"\n{'=' * 60}")
    print(f"Done. {success} succeeded, {failed} failed")
    print(f"{'=' * 60}")
    return 0 if failed == 0 else 1


def list_files_in_bucket(api, bucket_id: str):
    """Import from b2_client to avoid circular import."""
    from ..b2_client import list_files_in_bucket as _list
    return _list(api, bucket_id)