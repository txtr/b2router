"""Verification commands for B2 Router (--verify-only, --check)."""

import logging
import sys
from typing import TYPE_CHECKING

from ..state import OperationState
from ..b2_client import build_account_state, get_b2_client
from ..hashing import compute_sha1
from ..retry import retry_with_backoff, run_with_timeout

if TYPE_CHECKING:
    from ..models import Account, SourceFile, Bucket


def verify_files(
    source_files: list['SourceFile'],
    accounts: dict[str, 'Account'],
    source_dir: str,
    mode: str = "verify-only",  # "verify-only" or "check"
) -> tuple[int, int, int]:
    """Verify SHA-1 of files in B2 match local.

    Returns: (verified, mismatches, missing)
    """
    build_account_state(accounts, parallel=False)

    # Load state if available to get exact object names used
    state = OperationState.load(source_dir)
    state_allocations = {}
    if state:
        for entry in state.allocations:
            state_allocations[entry.abs_path] = entry.object_name

    mismatches = 0
    missing = 0
    verified = 0

    for src_file in source_files:
        # Use object_name from state if available, otherwise fall back to rel_path
        object_name = state_allocations.get(str(src_file.abs_path), src_file.rel_path)
        found = False
        for account in accounts.values():
            for bucket in account.buckets:
                if bucket._populate_failed:
                    continue
                if bucket.has_file(object_name):
                    # File exists in B2, verify SHA-1
                    client = get_b2_client(account)
                    def _get_file_info():
                        b2_bucket = client.get_bucket_by_id(bucket.id_)
                        return b2_bucket.get_file_info_by_name(object_name)
                    file_version = retry_with_backoff(run_with_timeout, _get_file_info)
                    if file_version and file_version.content_sha1:
                        try:
                            local_sha1 = compute_sha1(src_file.abs_path)
                        except OSError as exc:
                            logging.error(f"✗ {object_name} LOCAL FILE MISSING: {exc}")
                            mismatches += 1
                        else:
                            if mode == "verify-only":
                                if file_version.content_sha1 == local_sha1:
                                    logging.info(f"✓ {object_name} SHA-1 matches")
                                    verified += 1
                                else:
                                    logging.error(f"✗ {object_name} SHA-1 MISMATCH: local={local_sha1}, remote={file_version.content_sha1}")
                                    mismatches += 1
                            else:  # check mode
                                if file_version.content_sha1 != local_sha1:
                                    logging.error(f"✗ {object_name} SHA-1 MISMATCH")
                                    mismatches += 1
                    else:
                        logging.warning(f"? {object_name} SHA-1 not available from B2")
                    found = True
                    break
            if found:
                break
        if not found:
            if mode == "verify-only":
                logging.warning(f"! {object_name} not found in B2")
            else:
                logging.error(f"✗ {object_name} MISSING from B2")
                missing += 1

    return verified, mismatches, missing


def run_verify_only(args, accounts: dict[str, 'Account'], source_files: list['SourceFile'], source_dir: str) -> int:
    """Run --verify-only mode."""
    print(f"\n{'=' * 60}")
    print("🔍  VERIFY ONLY MODE - Checking SHA-1 of files in B2")
    print(f"{'=' * 60}\n")

    verified, mismatches, missing = verify_files(source_files, accounts, source_dir, "verify-only")

    print(f"\n{'=' * 60}")
    print(f"Verified: {verified}, Mismatches: {mismatches}, Not found: {len(source_files) - verified - mismatches}")
    if mismatches > 0:
        sys.exit(1)
    return 0


def run_check(args, accounts: dict[str, 'Account'], source_files: list['SourceFile'], source_dir: str) -> int:
    """Run --check mode."""
    print(f"\n{'=' * 60}")
    print("🔍  CHECK MODE - Verifying files in B2 match local")
    print(f"{'=' * 60}\n")

    verified, mismatches, missing = verify_files(source_files, accounts, source_dir, "check")

    print(f"\n{'=' * 60}")
    print(f"OK: {len(source_files) - mismatches - missing}, Mismatches: {mismatches}, Missing: {missing}")
    if mismatches > 0 or missing > 0:
        sys.exit(1)
    return 0