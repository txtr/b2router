"""Copy command for B2 Router."""

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import Account, SourceFile
from ..state import OperationState
from ..utils import format_bytes, collect_source_files
from ..b2_client import build_account_state, get_b2_client
from ..allocation import allocate_files
from ..upload import _upload_only
from ..execute import execute_operation
from ..commands.verify import run_verify_only, run_check
from ..hashing import compute_sha1
from ..retry import retry_with_backoff, run_with_timeout

if TYPE_CHECKING:
    from ..models import Bucket


def run_copy(args: argparse.Namespace, accounts: dict[str, Account]) -> int:
    """Execute copy command."""
    source = args.source
    dry_run = args.dry_run
    resume = args.resume
    parallel_uploads = min(args.parallel_uploads, 10)

    if not Path(source).is_dir():
        logging.error(f"Source directory not found: {source}")
        return 1

    source_files = collect_source_files(source)
    if not source_files:
        logging.info("No files found in source directory.")
        return 0

    total_size = sum(f.size for f in source_files)
    print(f"\n{'=' * 60}")
    print(f"        B2 ROUTER - COPY OPERATION")
    print(f"{'=' * 60}")
    print(f"📁  Source:      {source}")
    print(f"📄  Files:       {len(source_files)}")
    print(f"📦  Total size:  {format_bytes(total_size)}")
    print(f"⚙️  Mode:        {'Dry-run' if dry_run else 'Execute'}")
    print(f"🔄  Resume:      {'Yes' if resume else 'No'}")
    print(f"🔀  Parallel:    {parallel_uploads}")
    if resume:
        print(f"🔁  Resuming from saved state...")

    state = None
    if resume:
        state = OperationState.load(source)
        if not state:
            logging.error(f"No saved state found to resume. Use without --resume for new copy.")
            return 1
        # Validate config matches
        if not state.validate_config(args.accounts):
            logging.error(f"Config mismatch: state was created with different accounts file. Use without --resume for new copy.")
            return 1
        print(f"📋  State loaded: {state.created_at} (updated {state.updated_at})")
        print("🔍  Querying B2 accounts...")
        build_account_state(accounts, parallel=False)
    else:
        print("🔍  Querying B2 accounts...")
        build_account_state(accounts, parallel=False)

    print("📊  Allocating files...")
    allocation = allocate_files(source_files, accounts)
    if not allocation:
        print("⚠️  No files could be allocated (insufficient capacity).")
        return 0

    # Display allocation plan
    print_allocation_plan(allocation, source_files, accounts)

    # Handle --skip-existing: filter out files already in B2 with matching SHA-1
    if args.skip_existing:
        allocation = filter_existing_files(allocation, accounts, source_files)
        if not allocation:
            print("\n✅  All files already exist in B2 with matching SHA-1!")
            return 0
        # Re-display allocation plan after filtering
        print_allocation_plan(allocation, source_files, accounts, " (after --skip-existing)")

    if dry_run:
        print(f"\n{'=' * 60}")
        print("🔍  DRY RUN COMPLETE - No uploads performed")
        print(f"{'=' * 60}")
        return 0

    # Handle --verify-only and --check
    if args.verify_only:
        return run_verify_only(args, accounts, source_files, source)
    if args.check:
        return run_check(args, accounts, source_files, source)

    # Confirmation prompt (unless --yes provided)
    if not args.yes:
        print(f"\n{'=' * 60}")
        print(f"🚀  STARTING COPYING...")
        print(f"{'=' * 60}\n")
        print(f"This will copy {len(allocation)} files to B2.")
        print("Type 'yes' to confirm, or anything else to cancel:")
        try:
            response = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0
        if response != "yes":
            print("Cancelled.")
            return 0

    try:
        execute_operation(
            args, accounts, source_files, allocation, state, resume, parallel_uploads,
            operation_type="copy",
            upload_func=_upload_only,
            progress_desc="Copying",
            success_msg="files successfully copied",
            post_success=None,  # No cleanup for copy
        )
    except Exception as exc:
        logging.error(f"Copy failed: {exc}")
        if state:
            print(f"\n💾  State saved. Resume with:")
            print(f"    b2router copy --accounts={args.accounts} {source} --resume --yes")
        return 1

    return 0


def print_allocation_plan(
    allocation: dict[Path, tuple['Bucket', str]],
    source_files: list[SourceFile],
    accounts: dict[str, Account],
    suffix: str = ""
) -> None:
    """Print the allocation plan grouped by account."""
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
    # Build abs_path -> rel_path lookup
    rel_path_map = {f.abs_path: f.rel_path for f in source_files}

    print(f"\n{'─' * 60}")
    print(f"📋  ALLOCATION PLAN{suffix}")
    print(f"{'─' * 60}")

    for acc_name, items in account_allocations.items():
        acc_size = sum(item[0].stat().st_size for item in items)
        print(f"\n  📦  Account: {acc_name} ({len(items)} files, {format_bytes(acc_size)})")
        total_allocated += len(items)
        total_allocated_size += acc_size
        for abs_path, bucket, object_name in items[:10]:
            size_str = format_bytes(abs_path.stat().st_size)
            rel_path = rel_path_map.get(abs_path, abs_path.name)
            print(f"      📄  {rel_path} ({size_str}) → {bucket.name}/{object_name}")
        if len(items) > 10:
            remaining_size = sum(item[0].stat().st_size for item in items[10:])
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
    build_account_state(accounts, parallel=False)
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
                skipped_size += abs_path.stat().st_size
                continue
        filtered_allocation[abs_path] = (bucket, object_name)
    allocation = filtered_allocation
    print(f"  ⏭️  Skipped: {skipped_count} files ({format_bytes(skipped_size)})")
    print(f"  📤  Remaining: {len(allocation)} files")
    return allocation