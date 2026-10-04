"""Move command for B2 Router."""

import argparse
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import Account, SourceFile
from ..state import OperationState
from ..utils import format_bytes, collect_source_files
from ..b2_client import build_account_state
from ..allocation import allocate_files
from ..upload import _upload_and_delete, cleanup_empty_dirs
from ..execute import execute_operation
from ..commands.verify import run_verify_only, run_check
from ..commands.common import print_allocation_plan, filter_existing_files
from ..exceptions import VerificationError

if TYPE_CHECKING:
    from ..models import Bucket


def run_move(args: argparse.Namespace, accounts: dict[str, Account]) -> int:
    """Execute move command."""
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
    print(f"        B2 ROUTER - MOVE OPERATION")
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
            logging.error(f"No saved state found to resume. Use without --resume for new move.")
            return 1
        # Validate config matches
        if not state.validate_config(args.accounts):
            logging.error(f"Config mismatch: state was created with different accounts file. Use without --resume for new move.")
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
        try:
            return run_verify_only(args, accounts, source_files, source)
        except VerificationError as exc:
            logging.error(str(exc))
            return 1
    if args.check:
        try:
            return run_check(args, accounts, source_files, source)
        except VerificationError as exc:
            logging.error(str(exc))
            return 1

    # Confirmation prompt (unless --yes provided)
    if not args.yes:
        print(f"This will move {len(allocation)} files to B2.")
        print("Type 'yes' to confirm, or anything else to cancel:")
        try:
            response = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0
        if response != "yes":
            print("Cancelled.")
            return 0

    print(f"\n{'=' * 60}")
    print(f"🚀  STARTING UPLOADING...")
    print(f"{'=' * 60}\n")

    try:
        execute_operation(
            args, accounts, source_files, allocation, state, resume, parallel_uploads,
            operation_type="move",
            upload_func=_upload_and_delete,
            progress_desc="Uploading",
            success_msg="files successfully moved",
            post_success=cleanup_empty_dirs,
        )
    except Exception as exc:
        logging.error(f"Move failed: {exc}")
        if state:
            print(f"\n💾  State saved. Resume with:")
            print(f"    b2router move --accounts={args.accounts} {source} --resume --yes")
        return 1

    return 0