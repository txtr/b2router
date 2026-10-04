#!/usr/bin/env python3
"""B2 Router – Route files from a local directory to Backblaze B2 buckets.

This is a backwards-compatible entry point that delegates to the b2router package.

Commands:
    b2.py --accounts=accounts.yaml list [--parallel]
    b2.py --accounts=accounts.yaml move /source [--dry-run] [--yes]
    b2.py --accounts=accounts.yaml copy /source [--dry-run] [--yes]

Options:
    --dry-run  Show allocation plan without executing uploads
    --yes      Skip the confirmation prompt and proceed directly with upload
"""

# Re-export for backwards compatibility with tests and existing code
from b2router.config import load_config
from b2router.allocation import allocate_files, get_unique_object_name
from b2router.upload import delete_source_file, cleanup_empty_dirs
from b2router.utils import collect_source_files, format_bytes
from b2router.b2_client import discover_and_add_buckets_for_account, populate_bucket_files_and_usage
from b2router.models import FileMetadata, Bucket, Account, SourceFile, AllocationEntry
from b2router.state import OperationState
from b2router.hashing import compute_sha1
from b2router.allocation import CAP_SAFETY

from b2router.cli import main

__all__ = [
    "load_config",
    "allocate_files",
    "get_unique_object_name",
    "delete_source_file",
    "cleanup_empty_dirs",
    "collect_source_files",
    "format_bytes",
    "discover_and_add_buckets_for_account",
    "populate_bucket_files_and_usage",
    "FileMetadata",
    "Bucket",
    "Account",
    "SourceFile",
    "AllocationEntry",
    "OperationState",
    "compute_sha1",
    "CAP_SAFETY",
    "main",
]

if __name__ == "__main__":
    main()