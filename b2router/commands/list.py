"""List command for b2router_simple."""

from ..b2_client import build_buckets, list_files_in_bucket
from ..utils import format_bytes


def run_list(accounts) -> int:
    """List all files across all buckets."""
    buckets = build_buckets(accounts)

    total_files = 0
    total_size = 0

    for bucket in buckets:
        api = __import__('b2sdk.v2', fromlist=['B2Api']).B2Api(
            __import__('b2sdk.v2', fromlist=['InMemoryAccountInfo']).InMemoryAccountInfo()
        )
        # Re-authorize for this bucket's account
        acc = next(a for a in accounts if a.name == bucket.account_name)
        api.authorize_account("production", acc.account_id, acc.master_key)

        print(f"\nAccount: {bucket.account_name} ({bucket.bucket_name})")
        print(f"  Capacity: {format_bytes(bucket.capacity_bytes)}")

        files = list(list_files_in_bucket(api, bucket.bucket_id))
        if not files:
            print("  (empty)")
        else:
            for name, size in files:
                print(f"  {name} ({format_bytes(size)})")

        bucket_used = sum(s for _, s in files)
        bucket.used_bytes = bucket_used
        total_files += len(files)
        total_size += bucket_used

    print(f"\n{'=' * 60}")
    print(f"Total: {total_files} files, {format_bytes(total_size)}")
    print(f"{'=' * 60}")
    return 0