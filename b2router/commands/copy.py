"""Copy command for b2router - sequential upload, no deletion."""

import logging
from pathlib import Path
from ..b2_client import build_buckets, upload_file, get_file_info
from ..b2_client import B2Error
from ..allocation import allocate_files, print_allocation_plan
from ..utils import collect_source_files, format_bytes

# Google Drive shortcut extensions that aren't real files
GDOC_EXTENSIONS = {
    '.gdoc', '.gsheet', '.gslides', '.gdraw', '.gform', '.gscript', '.gmap', '.gsite',
    '.gvid', '.gscript', '.gfolder', '.glink', '.gscript', '.gtable', '.gscript'
}

logger = logging.getLogger(__name__)


def is_gdoc_file(path: Path) -> bool:
    """Check if file is a Google Drive shortcut (not a real file)."""
    return path.suffix.lower() in GDOC_EXTENSIONS


def run_copy(accounts, source_dir: str) -> int:
    """Copy files from source_dir to B2 buckets."""
    # Collect source files
    source_files = collect_source_files(source_dir)
    if not source_files:
        print("No files found in source directory")
        return 0

    total_size = sum(f.size for f in source_files)
    print(f"Found {len(source_files)} files ({format_bytes(total_size)})")

    # Build buckets and get current usage
    print("Authorizing accounts and discovering buckets...")
    buckets = build_buckets(accounts)
    for bucket in buckets:
        api = __import__('b2sdk.v2', fromlist=['B2Api']).B2Api(
            __import__('b2sdk.v2', fromlist=['InMemoryAccountInfo']).InMemoryAccountInfo()
        )
        acc = next(a for a in accounts if a.name == bucket.account_name)
        api.authorize_account("production", acc.account_id, acc.master_key)

        used = sum(s for _, s in list_files_in_bucket(api, bucket.bucket_id))
        bucket.used_bytes = used
        print(f"  {bucket.account_name}: {bucket.bucket_name} (used: {format_bytes(used)})")

    # Allocate
    print("Computing allocation plan...")
    allocations = allocate_files(source_files, buckets)
    if not allocations:
        print("No files could be allocated (all too large)")
        return 0

    print_allocation_plan(allocations, source_files, buckets)

    # Execute uploads sequentially
    print("\nStarting upload...")
    success = 0
    failed = 0
    skipped = 0

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

        print(f"\n--- Processing bucket: {bucket.bucket_name} ({bucket.account_name}) ---")

        bucket_transaction_cap = False  # Track if bucket hit transaction cap

        for i, entry in enumerate(entries, 1):
            # Skip remaining files if bucket hit transaction cap
            if bucket_transaction_cap:
                print(f"[{i}/{len(entries)}] ⊘ {entry.object_name} (skipped: bucket transaction cap exceeded)")
                skipped += 1
                continue

            # Skip Google Drive shortcut files
            if is_gdoc_file(entry.source_file.path):
                print(f"[{i}/{len(entries)}] ⊘ {entry.object_name} (skipped: Google Drive shortcut)")
                skipped += 1
                continue

            try:
                existing = get_file_info(api, bucket.bucket_id, entry.object_name)
            except B2Error as e:
                error_msg = str(e)
                if "transaction cap exceeded" in error_msg.lower():
                    print(f"[{i}/{len(entries)}] ⊘ {entry.object_name} (skipped: bucket transaction cap exceeded)")
                    bucket_transaction_cap = True
                    skipped += 1
                    continue
                raise

            if existing:
                print(f"[{i}/{len(entries)}] ⊘ {entry.object_name} (already exists in B2)")
                success += 1
                continue

            print(f"[{i}/{len(entries)}] ↑ Uploading: {entry.object_name} ({format_bytes(entry.source_file.size)})")
            print(f"    Source: {entry.source_file.path}")
            print(f"    Destination: {bucket.bucket_name}/{entry.object_name}")

            try:
                upload_success, error_msg = upload_file(api, bucket.bucket_id, str(entry.source_file.path), entry.object_name)
            except OSError as e:
                if e.errno == 95:  # Operation not supported (Google Drive shortcut)
                    print(f"    ⊘ Skipped: Google Drive shortcut (not a real file)")
                    skipped += 1
                    continue
                print(f"    ✗ Upload error: {e}")
                failed += 1
                continue
            except B2Error as e:
                error_msg = str(e)
                if "transaction cap exceeded" in error_msg.lower():
                    print(f"    ⊘ Skipped: Bucket transaction cap exceeded")
                    bucket_transaction_cap = True
                    skipped += 1
                    continue
                print(f"    ✗ Upload error: {error_msg}")
                failed += 1
                continue

            if upload_success:
                print(f"    ✓ Upload successful")
                success += 1
            else:
                # Check for storage cap exceeded
                if "storage cap exceeded" in error_msg.lower() or "cap exceeded" in error_msg.lower():
                    print(f"    ⊘ Skipped: Bucket storage cap exceeded")
                    skipped += 1
                else:
                    print(f"    ✗ FAILED: {error_msg}")
                    failed += 1

    print(f"\n{'=' * 60}")
    print(f"Done. {success} succeeded, {failed} failed, {skipped} skipped")
    print(f"{'=' * 60}")
    return 0 if failed == 0 else 1


def list_files_in_bucket(api, bucket_id: str):
    """Import from b2_client to avoid circular import."""
    from ..b2_client import list_files_in_bucket as _list
    return _list(api, bucket_id)