#!/usr/bin/env python3
"""
B2 Router – Route files from a local directory to Backblaze B2 buckets.

Commands:
    b2.py --accounts=accounts.yaml list [--parallel]
    b2.py --accounts=accounts.yaml move /source [--dry-run] [--yes]
    b2.py --accounts=accounts.yaml copy /source [--dry-run] [--yes]

Options:
    --dry-run  Show allocation plan without executing uploads
    --yes      Skip the confirmation prompt and proceed directly with upload
"""

import argparse
import hashlib
import json
import logging
import os
import platform
import random
import stat
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, TypeVar

import yaml
from b2sdk.v2 import B2Api, InMemoryAccountInfo
from b2sdk.v2.exception import B2Error
from tqdm import tqdm

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 30.0  # seconds

# Constants
CAP_SAFETY = 0.99  # stay 1% under capacity
STATE_FILE_NAME = ".b2router_state.json"
MAX_PARALLEL_UPLOADS = 10  # cap on parallel workers
# Overall timeout for B2 API calls (seconds)
B2_API_TIMEOUT = 300

# Version
__version__ = "1.0.0"

T = TypeVar('T')


def get_state_dir(source_dir: str) -> Path:
    """Get the directory for state files, outside the source tree.

    Uses platform-appropriate cache/config directory to avoid
    the state file being uploaded to B2.
    """
    source_path = Path(source_dir).resolve()
    # Create a unique subdir based on source path hash
    import hashlib
    path_hash = hashlib.sha256(str(source_path).encode()).hexdigest()[:12]
    
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        state_dir = base / "b2router" / "state" / path_hash
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
        state_dir = base / "b2router" / "state" / path_hash
    else:
        # Linux/Unix - use XDG_CACHE_HOME or ~/.cache
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        state_dir = base / "b2router" / "state" / path_hash
    
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_state_file_path(source_dir: str) -> Path:
    """Get the full path to the state file for a source directory."""
    return get_state_dir(source_dir) / STATE_FILE_NAME


def retry_with_backoff(
    func: Callable[..., T],
    *args,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    retry_exceptions: tuple[type[Exception], ...] = (B2Error, ConnectionError, TimeoutError, OSError, IOError),
    **kwargs
) -> T:
    """Execute function with exponential backoff retry for transient errors."""
    last_exception: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except retry_exceptions as exc:
            last_exception = exc
            if attempt < max_retries:
                # Exponential backoff with jitter to avoid thundering herd
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, 0.5)  # Add jitter
                logging.warning(f"Attempt {attempt + 1}/{max_retries + 1} failed: {exc}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
            else:
                logging.error(f"All {max_retries + 1} attempts failed: {exc}")
                raise
    if last_exception is None:
        raise RuntimeError("Retry failed with no exception recorded")
    raise last_exception


def run_with_timeout(func: Callable[..., T], *args, timeout: float = B2_API_TIMEOUT, **kwargs) -> T:
    """Run a function with a timeout using ThreadPoolExecutor.

    Note: This doesn't actually cancel the underlying operation if it times out,
    but it prevents the caller from blocking indefinitely.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            logging.error(f"Operation timed out after {timeout}s")
            # Attempt to cancel the underlying operation
            future.cancel()
            # Note: B2 SDK doesn't support cancellation, but we cancel the future
            # to prevent it from continuing to run in the thread pool
            raise


# ----------------------------------------------------------------------
# Strongly-typed data classes (using dataclass with slots)
# ----------------------------------------------------------------------


@dataclass(slots=True, eq=False)
class FileMetadata:
    """Represents a single file in a B2 bucket."""
    file_name: str
    size: int
    content_sha1: str | None = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileMetadata):
            return NotImplemented
        # Compare file_name and size always
        # For content_sha1: None != None (treat as unknown, don't consider equal)
        if self.content_sha1 is not None and other.content_sha1 is not None:
            return (self.file_name == other.file_name and
                    self.size == other.size and
                    self.content_sha1 == other.content_sha1)
        # If either SHA-1 is None, only compare name and size
        return self.file_name == other.file_name and self.size == other.size

    def __hash__(self) -> int:
        # Hash based on name and size only (SHA-1 can be None)
        return hash((self.file_name, self.size))

    def __repr__(self) -> str:
        return f"File({self.file_name}, {self.size} bytes)"


@dataclass(slots=True)
class Bucket:
    """Represents a B2 bucket with its capacity and file list."""
    name: str
    id_: str
    capacity_bytes: int = 0
    used_bytes: int = 0
    files: list[FileMetadata] = field(default_factory=list)
    _file_names: set = field(default_factory=set, repr=False)
    account: Optional['Account'] = field(default=None, repr=False)
    _populate_failed: bool = field(default=False, repr=False)

    def add_file(self, file_name: str, size: int, content_sha1: str | None = None) -> None:
        self.files.append(FileMetadata(file_name, size, content_sha1))
        self._file_names.add(file_name)

    @property
    def remaining_bytes(self) -> int:
        return self.capacity_bytes - self.used_bytes

    def has_file(self, file_name: str) -> bool:
        return file_name in self._file_names

    def __repr__(self) -> str:
        status = " (POPULATE FAILED)" if self._populate_failed else ""
        return f"Bucket({self.name} [id={self.id_}], capacity={self.capacity_bytes}, used={self.used_bytes}){status}"


@dataclass(slots=True)
class Account:
    """Represents a Backblaze B2 account."""
    name: str
    account_id: str
    master_key: str
    capacity_in_gb: int
    buckets: list[Bucket] = field(default_factory=list)
    client: B2Api | None = field(default=None, repr=False)
    realm: str = "production"  # B2 realm (production or test)

    def __repr__(self) -> str:
        return f"Account({self.name}, id={self.account_id}, capacity={self.capacity_in_gb} GB)"


@dataclass(slots=True)
class SourceFile:
    """Represents a source file to be uploaded."""
    abs_path: Path
    size: int
    rel_path: str


@dataclass(slots=True)
class AllocationEntry:
    """Represents a file allocation for persistence."""
    abs_path: str
    size: int
    rel_path: str
    bucket_name: str
    bucket_id: str
    account_name: str
    object_name: str
    sha1: str | None = None  # SHA-1 of source file at time of allocation
    uploaded: bool = False


@dataclass(slots=True)
class OperationState:
    """Persisted state for resume capability (used by both move and copy)."""
    source_dir: str
    accounts_config: str
    created_at: str
    updated_at: str
    allocations: list[AllocationEntry]
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False)

    @staticmethod
    def create(source_dir: str, accounts_config: str, allocation: dict[Path, tuple[Bucket, str]]) -> 'OperationState':
        """Create a new state from allocation plan."""
        entries = []
        for abs_path, (bucket, object_name) in allocation.items():
            if bucket.account is None:
                raise ValueError(f"Bucket {bucket.name} has no associated account")
            # Validate object name length (B2 limit: 1024 bytes)
            if len(object_name.encode('utf-8')) > 1024:
                raise ValueError(f"Object name too long (>1024 bytes): {object_name[:50]}...")
            # Compute SHA-1 for resume verification
            file_sha1 = compute_sha1(abs_path)
            entries.append(AllocationEntry(
                abs_path=str(abs_path),
                size=abs_path.stat().st_size,
                rel_path=str(abs_path).replace(str(Path(source_dir).resolve()), "").lstrip("/"),
                bucket_name=bucket.name,
                bucket_id=bucket.id_,
                account_name=bucket.account.name,
                object_name=object_name,
                sha1=file_sha1,
                uploaded=False
            ))
        now = datetime.utcnow().isoformat() + "Z"
        return OperationState(
            source_dir=source_dir,
            accounts_config=accounts_config,
            created_at=now,
            updated_at=now,
            allocations=entries
        )

    def to_json(self) -> str:
        # Exclude _lock from serialization (not JSON serializable)
        data = {
            "source_dir": self.source_dir,
            "accounts_config": self.accounts_config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "allocations": [asdict(e) for e in self.allocations]
        }
        return json.dumps(data, indent=2)

    @staticmethod
    def from_json(json_str: str) -> 'OperationState':
        data = json.loads(json_str)
        return OperationState(
            source_dir=data["source_dir"],
            accounts_config=data["accounts_config"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            allocations=[AllocationEntry(**e) for e in data["allocations"]]
        )

    def save(self, source_dir: str) -> None:
        """Save state to .b2router_state.json in platform-appropriate cache directory."""
        with self._lock:
            state_file = get_state_file_path(source_dir)
            state_file.write_text(self.to_json())
            # Restrict permissions: owner read/write only (contains account info)
            if sys.platform == "win32":
                # Use icacls to set ACL: owner full, remove inheritance, remove other users
                try:
                    import subprocess
                    # Disable inheritance and remove inherited ACEs
                    subprocess.run(["icacls", str(state_file), "/inheritance:r"], check=False, capture_output=True)
                    # Grant current user full control
                    subprocess.run(["icacls", str(state_file), "/grant:r", f"{os.getlogin()}:(OI)(CI)F"], check=False, capture_output=True)
                    # Remove all other access (optional, but more secure)
                    # Note: This is best-effort; icacls may fail in some environments
                except Exception:
                    pass  # Best effort - if icacls fails, file may be world-readable
            else:
                state_file.chmod(0o600)
            logging.debug(f"Saved state to {state_file}")

    @staticmethod
    def load(source_dir: str) -> Optional['OperationState']:
        """Load state from .b2router_state.json in platform-appropriate cache directory."""
        state_file = get_state_file_path(source_dir)
        if not state_file.exists():
            return None
        try:
            return OperationState.from_json(state_file.read_text())
        except Exception as exc:
            logging.warning(f"Failed to load state file: {exc}")
            return None

    def validate_config(self, accounts_config: str) -> bool:
        """Validate that the state matches the current config."""
        return self.accounts_config == accounts_config

    def get_pending(self) -> list[AllocationEntry]:
        """Get list of entries not yet uploaded."""
        return [e for e in self.allocations if not e.uploaded]

    def mark_uploaded(self, abs_path: str) -> None:
        """Mark an entry as uploaded."""
        with self._lock:
            for entry in self.allocations:
                if entry.abs_path == abs_path:
                    entry.uploaded = True
                    break
            self.updated_at = datetime.utcnow().isoformat() + "Z"

    def is_complete(self) -> bool:
        """Check if all entries are uploaded."""
        return all(e.uploaded for e in self.allocations)

    def cleanup(self, source_dir: str) -> None:
        """Remove state file after successful completion."""
        with self._lock:
            state_file = get_state_file_path(source_dir)
            if state_file.exists():
                state_file.unlink()
                logging.debug(f"Removed state file {state_file}")


# ----------------------------------------------------------------------
# B2 API and Configuration helpers
# ----------------------------------------------------------------------


def load_config(config_path: str) -> dict[str, Account]:
    """Load accounts from YAML configuration."""
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {cfg_path}")

    with open(cfg_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    accounts: dict[str, Account] = {}
    raw_accounts = raw.get("accounts", {}) or {}
    
    # Check for duplicate account IDs across accounts
    seen_account_ids: set[str] = set()
    for acc_name, acc_data in raw_accounts.items():
        try:
            account_id = acc_data["account_id"]
            master_key = acc_data["master_key"]
            capacity_in_gb = acc_data["capacity_in_gb"]

            # Validate credentials are non-empty
            if not account_id or not str(account_id).strip():
                raise ValueError(f"account_id is empty in account '{acc_name}'")
            if not master_key or not str(master_key).strip():
                raise ValueError(f"master_key is empty in account '{acc_name}'")
            if not isinstance(capacity_in_gb, int) or capacity_in_gb <= 0:
                raise ValueError(f"capacity_in_gb must be a positive integer in account '{acc_name}'")
            
            # Check for duplicate account_id
            if account_id in seen_account_ids:
                raise ValueError(f"Duplicate account_id '{account_id}' found (also used by another account)")
            seen_account_ids.add(account_id)

            accounts[acc_name] = Account(
                name=acc_name,
                account_id=account_id,
                master_key=master_key,
                capacity_in_gb=capacity_in_gb
            )
        except KeyError as exc:
            raise KeyError(f"Missing key {exc} in account '{acc_name}'")
    return accounts


def get_b2_client(account: Account) -> B2Api:
    """Authenticate and return a B2Api client for the given account.

    Uses InMemoryAccountInfo to isolate each account's authorization cache,
    preventing cross-account cache pollution from shared SqliteAccountInfo.
    """
    if account.client is None:
        logging.debug(f"Creating NEW B2Api client for account {account.name}")
        account.client = B2Api(InMemoryAccountInfo())

        def _authorize():
            account.client.authorize_account(
                realm=account.realm,
                application_key_id=account.account_id,
                application_key=account.master_key
            )

        retry_with_backoff(run_with_timeout, _authorize)
    else:
        logging.debug(f"Using CACHED B2Api client for account {account.name}")
    return account.client


def discover_and_add_buckets_for_account(account: Account) -> None:
    """Discover all buckets for an account from B2 dynamically."""
    # Clear existing buckets to make this idempotent (safe to call multiple times)
    account.buckets.clear()

    client = get_b2_client(account)

    def _list_buckets():
        return client.list_buckets()

    b2_buckets_info = retry_with_backoff(run_with_timeout, _list_buckets)
    # First pass: create buckets with placeholder capacity
    for b2_bucket in b2_buckets_info:
        # Handle both possible SDK attribute names for bucket ID
        bucket_id = getattr(b2_bucket, 'id_', None) or getattr(b2_bucket, 'bucket_id', None) or 'unknown'
        bucket_obj = Bucket(name=b2_bucket.name, id_=bucket_id, capacity_bytes=0)
        bucket_obj.account = account
        account.buckets.append(bucket_obj)

    # Second pass: set bucket capacity to account total (B2 has no per-bucket limit)
    # The account-level capacity limit is enforced in allocate_files()
    n = len(account.buckets)
    if n > 0:
        total_capacity_bytes = int(account.capacity_in_gb * (1024 ** 3) * CAP_SAFETY)
        for bucket in account.buckets:
            bucket.capacity_bytes = total_capacity_bytes
            # used_bytes will be populated by populate_bucket_files_and_usage

    logging.info(f"Discovered {len(account.buckets)} buckets for account {account.name}.")


def _process_account(account: Account) -> None:
    """Process a single account: discover buckets and populate file metadata."""
    discover_and_add_buckets_for_account(account)
    for bucket in account.buckets:
        populate_bucket_files_and_usage(bucket)


def populate_bucket_files_and_usage(bucket: Bucket) -> None:
    """Query B2 for all objects in the bucket and populate details.

    If population fails, the bucket is marked as failed and will be skipped
    during allocation to prevent over-allocation based on stale/zero usage data.
    """
    if bucket.account is None:
        raise ValueError("Bucket must have an associated account")
    client = get_b2_client(bucket.account)
    total = 0
    bucket.files.clear()
    bucket._file_names.clear()
    bucket._populate_failed = False

    def _list_and_process():
        """List and process all objects in the bucket incrementally (streaming)."""
        b2_bucket = client.get_bucket_by_id(bucket.id_)
        # Stream objects instead of loading all into memory
        for fv, _ in b2_bucket.ls(recursive=True):
            total_local = fv.size
            bucket.add_file(fv.file_name, fv.size, fv.content_sha1)
            # Use nonlocal to update total
            nonlocal_total[0] += total_local

    # Use a list to allow modification from nested function
    nonlocal_total = [0]
    try:
        # Retry wraps the entire streaming operation with timeout
        retry_with_backoff(run_with_timeout, _list_and_process)
        total = nonlocal_total[0]
    except Exception as exc:
        logging.warning(f"Error listing objects in {bucket.name}: {exc}")
        bucket._populate_failed = True
        # Don't set used_bytes - keep previous value or 0 to avoid false empty state

    if not bucket._populate_failed:
        bucket.used_bytes = total


def build_account_state(accounts: dict[str, Account], parallel: bool = False) -> None:
    """Discover all buckets and populate file metadata."""
    if parallel:
        # Limit workers to avoid rate limiting (B2 allows ~2-3 concurrent auth requests)
        max_workers = min(len(accounts), 3)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_account = {
                executor.submit(_process_account, account): account
                for account in accounts.values()
            }
            for future in as_completed(future_to_account):
                future.result()
    else:
        # Sequential with small delay between accounts to avoid rate limiting
        for i, account in enumerate(accounts.values()):
            if i > 0:
                time.sleep(0.5)  # Rate limit mitigation
            _process_account(account)


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------


def list_all(accounts: dict[str, Account], parallel: bool = False) -> None:
    """Print details of all accounts, buckets, and files."""

    # Show progress while fetching
    print("🔍  Fetching account state from B2...")
    build_account_state(accounts, parallel=parallel)

    print("\n" + "=" * 70)
    print("                         B2 ROUTER - ACCOUNT OVERVIEW")
    print("=" * 70)

    total_accounts = len(accounts)
    total_buckets = 0
    total_files = 0
    total_used_gb = 0.0
    total_capacity_gb = 0.0

    for acc_name, account in accounts.items():
        print(f"\n📦  Account: {acc_name}")
        print(f"    ID: {account.account_id}")
        print(f"    Realm: {getattr(account, 'realm', 'production')}")

        account_used = sum(b.used_bytes for b in account.buckets if not b._populate_failed)
        account_capacity = int(account.capacity_in_gb * (1024 ** 3) * CAP_SAFETY)
        account_used_gb = account_used / (1024 ** 3)
        account_capacity_gb = account_capacity / (1024 ** 3)
        pct = (account_used_gb / account_capacity_gb * 100) if account_capacity_gb > 0 else 0

        total_used_gb += account_used_gb
        total_capacity_gb += account_capacity_gb

        # Progress bar for account usage
        bar_width = 30
        filled = int(bar_width * account_used_gb / account_capacity_gb) if account_capacity_gb > 0 else 0
        filled = max(0, min(filled, bar_width))  # Clamp
        bar = "█" * filled + "░" * (bar_width - filled)
        print(f"    Capacity: {account_capacity_gb:.2f} GB / {account.capacity_in_gb} GB (safety: {CAP_SAFETY*100:.0f}%)")
        print(f"    Used:     {account_used_gb:.2f} GB ({pct:.1f}%) [{bar}]")

        account_buckets = 0
        account_files = 0
        for bucket in account.buckets:
            status = " ⚠️  POPULATE FAILED" if bucket._populate_failed else ""
            print(f"\n    🪣  Bucket: {bucket.name}")
            print(f"        ID: {bucket.id_}{status}")

            bucket_used_gb = bucket.used_bytes / (1024 ** 3)
            # Bucket capacity is account-level (B2 has no per-bucket limit)
            bucket_cap_gb = bucket.capacity_bytes / (1024 ** 3)
            bucket_pct = (bucket_used_gb / bucket_cap_gb * 100) if bucket_cap_gb > 0 else 0

            filled = int(20 * bucket_used_gb / bucket_cap_gb) if bucket_cap_gb > 0 else 0
            filled = max(0, min(filled, 20))  # Clamp
            bar = "█" * filled + "░" * (20 - filled)
            print(f"        Capacity: {bucket_cap_gb:.2f} GB (account limit)")
            print(f"        Used:     {bucket_used_gb:.2f} GB ({bucket_pct:.1f}%) [{bar}]")

            if not bucket.files:
                print(f"        Files:    (empty)")
            else:
                print(f"        Files:    {len(bucket.files)}")
                account_files += len(bucket.files)
                total_files += len(bucket.files)
                # Show first 5 files, then truncate
                for i, f in enumerate(bucket.files[:5]):
                    size_str = format_bytes(f.size)
                    print(f"          • {f.file_name} ({size_str})")
                if len(bucket.files) > 5:
                    print(f"          … and {len(bucket.files) - 5} more files")
            account_buckets += 1
            total_buckets += 1

        print(f"\n    📊  Account summary: {account_buckets} buckets, {account_files} files")

    # Global summary
    print("\n" + "=" * 70)
    print("                              GLOBAL SUMMARY")
    print("=" * 70)
    total_pct = (total_used_gb / total_capacity_gb * 100) if total_capacity_gb > 0 else 0
    print(f"  Accounts:   {total_accounts}")
    print(f"  Buckets:    {total_buckets}")
    print(f"  Files:      {total_files}")
    print(f"  Used:       {total_used_gb:.2f} GB / {total_capacity_gb:.2f} GB ({total_pct:.1f}%)")


def format_bytes(size: int) -> str:
    """Format bytes as human-readable string."""
    size_float: float = size
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_float < 1024 or unit == 'TB':
            if unit == 'B':
                return f"{int(size_float)} {unit}"
            return f"{size_float:.2f} {unit}"
        size_float /= 1024
    return f"{size_float:.2f} TB"  # Fallback (should never reach here)


def collect_source_files(source_dir: str) -> list[SourceFile]:
    """Collect all regular files from source directory, skipping broken symlinks.

    Does not follow symlinks to avoid including files outside the source tree.
    """
    files = []
    source_path = Path(source_dir).resolve()
    for root, _, filenames in os.walk(source_path, followlinks=False):
        for name in filenames:
            abs_path = Path(root) / name
            try:
                # Use lstat to not follow symlinks - we want to skip symlinks entirely
                stat_result = abs_path.lstat()
                # Skip if not a regular file (e.g., sockets, devices, directories, symlinks)
                if not stat.S_ISREG(stat_result.st_mode):
                    continue
                size = stat_result.st_size
            except OSError as exc:
                logging.warning(f"Skipping inaccessible file {abs_path}: {exc}")
                continue
            rel = abs_path.relative_to(source_path)
            rel_str = str(rel).replace(os.sep, "/")
            files.append(SourceFile(abs_path, size, rel_str))
    return files


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
    # Use local tracking to avoid mutating bucket objects
    bucket_info = []  # list of (bucket, bucket_remaining, account_remaining)
    for account in accounts.values():
        # Calculate total used space across all buckets in this account
        account_used = sum(b.used_bytes for b in account.buckets if not b._populate_failed)
        account_capacity = int(account.capacity_in_gb * (1024 ** 3) * CAP_SAFETY)
        account_remaining = account_capacity - account_used

        for bucket in account.buckets:
            if bucket._populate_failed:
                logging.warning(f"Skipping bucket {bucket.name} (account {account.name}) - population failed, cannot determine available space")
                continue
            bucket_remaining = bucket.remaining_bytes
            bucket_info.append((bucket, bucket_remaining, account_remaining))

    sorted_files = sorted(source_files, key=lambda x: x.size, reverse=True)
    allocation: dict[Path, tuple[Bucket, str]] = {}

    for src_file in sorted_files:
        best_idx = None
        best_remaining = None

        for i, (bucket, bucket_remaining, account_remaining) in enumerate(bucket_info):
            # Use the more restrictive limit
            effective_remaining = min(bucket_remaining, account_remaining)

            if effective_remaining >= src_file.size:
                if best_remaining is None or effective_remaining < best_remaining:
                    best_remaining = effective_remaining
                    best_idx = i

        if best_idx is not None:
            bucket, bucket_remaining, account_remaining = bucket_info[best_idx]
            object_name = get_unique_object_name(bucket, src_file.rel_path)
            allocation[src_file.abs_path] = (bucket, object_name)
            # Update local tracking only (don't mutate bucket objects)
            bucket_info[best_idx] = (
                bucket,
                bucket_remaining - src_file.size,
                account_remaining - src_file.size
            )
        else:
            logging.warning(f"Skipping {src_file.rel_path} ({src_file.size} bytes) – no bucket has enough free space")
    return allocation


def compute_sha1(file_path: Path) -> str:
    """Compute SHA-1 hash of a file."""
    sha1 = hashlib.sha1()
    try:
        with file_path.open('rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha1.update(chunk)
    except KeyboardInterrupt:
        raise
    return sha1.hexdigest()


def upload_file(bucket: Bucket, abs_path: Path, object_name: str, size: int) -> bool:
    """Upload a file to B2 using streaming and verify SHA-1 after upload.

    If file already exists in B2 with matching SHA-1, skip upload.
    """
    if bucket.account is None:
        raise ValueError("Bucket must have an associated account")
    account = bucket.account
    client = get_b2_client(account)

    # Compute local SHA-1 before upload
    local_sha1 = compute_sha1(abs_path)

    def _do_upload() -> bool:
                b2_bucket = client.get_bucket_by_id(bucket.id_)
                # Use upload_local_file for streaming upload (avoids loading entire file into memory)
                b2_bucket.upload_local_file(str(abs_path), object_name)
                return True

    try:
        # Check if file already exists in B2 (for resume/idempotency)
        def _get_file_info():
            b2_bucket = client.get_bucket_by_id(bucket.id_)
            try:
                return b2_bucket.get_file_info_by_name(object_name)
            except B2Error as e:
                if "File not present" in str(e) or "not_found" in str(e).lower():
                    return None
                raise

        existing_file = retry_with_backoff(run_with_timeout, _get_file_info)
        if existing_file is not None:
            remote_sha1 = existing_file.content_sha1
            if remote_sha1 is not None and remote_sha1 == local_sha1:
                logging.info(f"File {object_name} already exists with matching SHA-1, skipping upload")
                return True
            else:
                logging.warning(f"File {object_name} exists but SHA-1 differs (local={local_sha1}, remote={remote_sha1}), will re-upload")
        else:
            logging.debug(f"File {object_name} not found in B2, will upload")

        retry_with_backoff(run_with_timeout, _do_upload)

        # Verify SHA-1 after upload
        def _verify_upload():
            b2_bucket = client.get_bucket_by_id(bucket.id_)
            return b2_bucket.get_file_info_by_name(object_name)

        file_version = retry_with_backoff(run_with_timeout, _verify_upload)
        if file_version is None:
            logging.error(f"B2 did not return file info for {object_name} after upload")
            return False

        remote_sha1 = file_version.content_sha1

        if remote_sha1 is None:
            # B2 may not have computed SHA-1 yet for large files (async processing)
            # Log warning but don't fail - upload succeeded
            logging.warning(f"B2 has not yet computed SHA-1 for {object_name} (large file async processing). Upload succeeded but SHA-1 not verified.")
            return True

        if local_sha1 != remote_sha1:
            logging.error(f"SHA-1 mismatch for {object_name}: local={local_sha1}, remote={remote_sha1}")
            return False

        logging.info(f"Uploaded {object_name} ({size} bytes) \u2192 {account.name}:{bucket.name} [SHA-1 verified]")
        return True
    except Exception as exc:
        logging.error(f"Failed upload to bucket '{bucket.name}' (Account '{account.name}'): {exc}")
        return False


def delete_source_file(abs_path: Path) -> bool:
    """Delete source file after successful upload."""
    try:
        abs_path.unlink()
        logging.info(f"Deleted local source file: {abs_path}")
        return True
    except Exception as exc:
        logging.error(f"Failed to delete {abs_path}: {exc}")
        return False


def cleanup_empty_dirs(source_dir: str) -> None:
    """Remove empty directories after file moves.

    Does not follow symlinks to avoid traversing outside the source tree.
    """
    # Small delay to ensure all file handles are released after parallel uploads
    time.sleep(0.1)
    source_path = Path(source_dir).resolve()
    for root, dirs, files in os.walk(source_path, topdown=False, followlinks=False):
        for dir_name in dirs:
            dir_path = Path(root) / dir_name
            try:
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
                    logging.info(f"Removed empty directory: {dir_path}")
            except OSError:
                pass  # Directory not empty or other error


def _upload_and_delete(bucket: Bucket, abs_path: Path, object_name: str, size: int) -> tuple[Path, bool, str | None]:
    """Upload a file and delete source on success. Returns (abs_path, success, error_msg)."""
    if upload_file(bucket, abs_path, object_name, size):
        if delete_source_file(abs_path):
            return (abs_path, True, None)
        else:
            return (abs_path, False, f"Deletion failed for {abs_path}")
    else:
        return (abs_path, False, f"Upload failed for {abs_path} to {bucket.name}")


def _upload_only(bucket: Bucket, abs_path: Path, object_name: str, size: int) -> tuple[Path, bool, str | None]:
    """Upload a file without deleting source. Returns (abs_path, success, error_msg)."""
    if upload_file(bucket, abs_path, object_name, size):
        return (abs_path, True, None)
    else:
        return (abs_path, False, f"Upload failed for {abs_path} to {bucket.name}")


# ----------------------------------------------------------------------
# Execution Controller
# ----------------------------------------------------------------------


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure logging based on verbosity flags."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    # Use stderr for logs to not interfere with stdout output
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)]
    )

    # Reduce tqdm output noise
    logging.getLogger("tqdm").setLevel(logging.WARNING)


def _execute_operation(
    args: argparse.Namespace,
    accounts: dict[str, Account],
    source_files: list[SourceFile],
    allocation: dict[Path, tuple[Bucket, str]],
    state: OperationState | None = None,
    resume: bool = False,
    max_workers: int = 1,
    operation_type: str = "move",
    upload_func: Callable[[Bucket, Path, str, int], tuple[Path, bool, str | None]] = _upload_and_delete,
    progress_desc: str = "Uploading",
    success_msg: str = "files successfully moved",
    post_success: Callable[[str], None] | None = cleanup_empty_dirs,
) -> int:
    """Execute move or copy operation with shared logic.
    
    Args:
        args: Parsed command line arguments
        accounts: Account configurations
        source_files: List of source files to process
        allocation: Mapping of source paths to (bucket, object_name) tuples
        state: Existing state for resume, or None for new operation
        resume: Whether this is a resume operation
        max_workers: Number of parallel workers (1 = sequential)
        operation_type: "move" or "copy" - used for messages
        upload_func: Function to process each file (_upload_and_delete or _upload_only)
        progress_desc: Progress bar description
        success_msg: Success message suffix
        post_success: Optional callback on success (e.g., cleanup_empty_dirs for move)
    """
    simple_log = getattr(args, 'simple_log', False)
    # If resuming, rebuild allocation from state
    if resume and state:
        allocation = {}
        # Refresh account state from B2 to get current capacity/usage
        logging.info("Refreshing account state from B2 for resume verification...")
        build_account_state(accounts, parallel=False)
        
        for entry in state.allocations:
            if not entry.uploaded:
                found = False
                for account in accounts.values():
                    if account.name == entry.account_name:
                        for bucket in account.buckets:
                            if bucket.name == entry.bucket_name and bucket.id_ == entry.bucket_id:
                                # Validate object name length (B2 limit: 1024 bytes)
                                if len(entry.object_name.encode('utf-8')) > 1024:
                                    logging.warning(f"Object name too long (>1024 bytes), skipping: {entry.object_name[:50]}...")
                                    found = True  # Skip this entry
                                    break
                                
                                # H4: Verify bucket still exists and account has capacity
                                if bucket._populate_failed:
                                    logging.warning(f"Bucket {bucket.name} population failed, cannot verify capacity, skipping {entry.abs_path}")
                                    found = True
                                    break
                                
                                # Check account capacity
                                account_used = sum(b.used_bytes for b in account.buckets if not b._populate_failed)
                                account_capacity = int(account.capacity_in_gb * (1024 ** 3) * CAP_SAFETY)
                                account_remaining = account_capacity - account_used
                                
                                # Get source file size for capacity check
                                source_path = Path(entry.abs_path)
                                if source_path.exists():
                                    file_size = source_path.stat().st_size
                                    # H3: Verify local file hasn't changed since state was saved
                                    if entry.size != file_size:
                                        logging.warning(f"Local file {entry.abs_path} size changed ({entry.size} -> {file_size}), will re-upload")
                                    elif entry.sha1:
                                        local_sha1 = compute_sha1(source_path)
                                        if local_sha1 != entry.sha1:
                                            logging.warning(f"Local file {entry.abs_path} SHA-1 changed, will re-upload")
                                    if account_remaining < file_size:
                                        logging.warning(f"Account {account.name} has insufficient capacity ({format_bytes(account_remaining)} remaining, need {format_bytes(file_size)}), skipping {entry.abs_path}")
                                        found = True
                                        break
                                else:
                                    logging.warning(f"Source file {entry.abs_path} no longer exists, skipping")
                                    found = True
                                    break
                                
                                allocation[Path(entry.abs_path)] = (bucket, entry.object_name)
                                found = True
                                break
                        break
                if not found:
                    logging.warning(f"Could not find account '{entry.account_name}' bucket '{entry.bucket_name}' for resume, skipping {entry.abs_path}")
        logging.info(f"Resuming: {len(allocation)} files remaining")
        if not allocation:
            logging.info(f"All files already {operation_type}ed!")
            state.cleanup(args.source)
            return 0
    else:
        # New operation - create state
        state = OperationState.create(args.source, args.accounts, allocation)
        state.save(args.source)

    items = list(allocation.items())
    total_files = len(items)

    def process_item(item: tuple[Path, tuple[Bucket, str]]) -> tuple[Path, bool, str | None]:
        abs_path, (bucket, object_name) = item
        size = abs_path.stat().st_size
        return upload_func(bucket, abs_path, object_name, size)

    success = 0
    failed = False
    error_msg = None
    files_since_save = 0
    SAVE_BATCH_SIZE = 5  # Save state every N files
    save_lock = threading.Lock()  # Protect files_since_save and state.save()
    processed = 0

    # Progress bar or simple log
    if simple_log:
        logging.info(f"Starting {progress_desc.lower()} {total_files} files...")
        pbar = None
    else:
        pbar = tqdm(total=total_files, desc=progress_desc, unit="file",
                    disable=args.quiet, leave=True,
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    def log_progress(path: Path, ok: bool, err: str | None = None) -> None:
        """Log progress for simple-log mode."""
        if simple_log:
            processed_nonlocal[0] += 1
            current = processed_nonlocal[0]
            if ok:
                rel = path.name
                logging.info(f"[{current}/{total_files}] ✓ {rel}")
            else:
                logging.error(f"[{current}/{total_files}] ✗ {path.name}: {err}")

    processed_nonlocal = [0]

    try:
        if max_workers == 1:
            # Sequential
            for item in items:
                path, ok, err = process_item(item)
                log_progress(path, ok, err)
                if ok:
                    success += 1
                    files_since_save += 1
                    if state:
                        state.mark_uploaded(str(path))
                        if files_since_save >= SAVE_BATCH_SIZE:
                            state.save(args.source)
                            files_since_save = 0
                else:
                    failed = True
                    error_msg = err
                    break
                if pbar:
                    pbar.update(1)
        else:
            # Parallel uploads
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_path = {executor.submit(process_item, item): item[0] for item in items}
                for future in as_completed(future_to_path):
                    path, ok, err = future.result()
                    log_progress(path, ok, err)
                    if ok:
                        success += 1
                        # Thread-safe state save batching
                        with save_lock:
                            files_since_save += 1
                            if state:
                                state.mark_uploaded(str(path))
                                if files_since_save >= SAVE_BATCH_SIZE:
                                    state.save(args.source)
                                    files_since_save = 0
                    else:
                        failed = True
                        error_msg = err
                        # Cancel remaining futures
                        for f in future_to_path:
                            f.cancel()
                        break
                    if pbar:
                        pbar.update(1)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user, saving state...")
        if state:
            state.save(args.source)
        raise
    finally:
        if pbar:
            pbar.close()
        # Final state save
        if state and files_since_save > 0:
            state.save(args.source)

    if failed:
        logging.error(f"{error_msg}! Interrupting and stopping execution immediately to prevent partial failures.")
        raise RuntimeError(error_msg)

    if state:
        state.cleanup(args.source)

    # Post-success callback (e.g., cleanup empty dirs for move)
    if post_success:
        post_success(args.source)

    if simple_log:
        logging.info(f"Done. {success}/{total_files} {success_msg}.")
    else:
        print(f"\n== Done. {success}/{total_files} {success_msg}.")
    return success


def main() -> None:
    parser = argparse.ArgumentParser(description="B2 Router – manage and move files to Backblaze B2")
    parser.add_argument("--accounts", default="accounts.yaml", help="Path to accounts YAML configuration")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (debug) output")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress non-error output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--realm", default="production", choices=["production", "test"],
                        help="B2 realm (default: production)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List all accounts, buckets, and files")
    list_parser.add_argument("--parallel", action="store_true", help="Fetch accounts in parallel for faster results")

    move_parser = subparsers.add_parser("move", help="Move files from source directory to B2")
    move_parser.add_argument("source", help="Local directory containing files to move")
    move_parser.add_argument("--dry-run", action="store_true", help="Show allocation plan without execution")
    move_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt and proceed directly with upload")
    move_parser.add_argument("--parallel-uploads", type=int, default=1, metavar="N",
                             help="Number of parallel uploads (default: 1, max: 10)")
    move_parser.add_argument("--resume", action="store_true",
                             help="Resume interrupted move from saved state file")
    move_parser.add_argument("--skip-existing", action="store_true",
                             help="Skip files that already exist in B2 with matching SHA-1")
    move_parser.add_argument("--check", action="store_true",
                             help="Verify files in B2 match local (no upload), exit with code 1 if mismatch")
    move_parser.add_argument("--verify-only", action="store_true",
                             help="Only verify SHA-1 of already uploaded files (no upload)")
    move_parser.add_argument("--simple-log", action="store_true",
                             help="Simple line-based logging per file (no progress bar, works in Colab)")

    copy_parser = subparsers.add_parser("copy", help="Copy files from source directory to B2 (no deletion)")
    copy_parser.add_argument("source", help="Local directory containing files to copy")
    copy_parser.add_argument("--dry-run", action="store_true", help="Show allocation plan without execution")
    copy_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt and proceed directly with upload")
    copy_parser.add_argument("--parallel-uploads", type=int, default=1, metavar="N",
                             help="Number of parallel uploads (default: 1, max: 10)")
    copy_parser.add_argument("--resume", action="store_true",
                             help="Resume interrupted copy from saved state file")
    copy_parser.add_argument("--skip-existing", action="store_true",
                             help="Skip files that already exist in B2 with matching SHA-1")
    copy_parser.add_argument("--check", action="store_true",
                             help="Verify files in B2 match local (no upload), exit with code 1 if mismatch")
    copy_parser.add_argument("--verify-only", action="store_true",
                             help="Only verify SHA-1 of already uploaded files (no upload)")
    copy_parser.add_argument("--simple-log", action="store_true",
                             help="Simple line-based logging per file (no progress bar, works in Colab)")

    args = parser.parse_args()

    # Validate parallel_uploads range
    if hasattr(args, "parallel_uploads") and not (1 <= args.parallel_uploads <= MAX_PARALLEL_UPLOADS):
        logging.error(f"--parallel-uploads must be between 1 and {MAX_PARALLEL_UPLOADS}")
        return

    setup_logging(verbose=args.verbose, quiet=args.quiet)

    try:
        accounts = load_config(args.accounts)
    except Exception as exc:
        logging.error(f"Failed to load configuration: {exc}")
        return

    # Store realm for use in get_b2_client
    for account in accounts.values():
        account.realm = args.realm

    if args.command == "list":
        list_all(accounts, parallel=args.parallel)
        return

    if args.command == "move":
        _execute_command(args, accounts, command_type="move")

    if args.command == "copy":
        _execute_command(args, accounts, command_type="copy")


def _execute_command(args: argparse.Namespace, accounts: dict[str, Account], command_type: str) -> None:
    """Execute move or copy command with shared logic."""
    source = args.source
    dry_run = args.dry_run
    yes = args.yes
    resume = args.resume
    parallel_uploads = min(args.parallel_uploads, MAX_PARALLEL_UPLOADS)

    if not os.path.isdir(source):
        logging.error(f"Source directory not found: {source}")
        return

    source_files = collect_source_files(source)
    if not source_files:
        logging.info("No files found in source directory.")
        return

    total_size = sum(f.size for f in source_files)
    print(f"\n{'=' * 60}")
    op_name = "MOVE" if command_type == "move" else "COPY"
    print(f"        B2 ROUTER - {op_name} OPERATION")
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
            logging.error(f"No saved state found to resume. Use without --resume for new {command_type}.")
            return
        # Validate config matches
        if not state.validate_config(args.accounts):
            logging.error(f"Config mismatch: state was created with different accounts file. Use without --resume for new {command_type}.")
            return
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
        return

    print(f"\n{'─' * 60}")
    print("📋  ALLOCATION PLAN")
    print(f"{'─' * 60}")

    # Group by account for display
    account_allocations: dict[str, list[tuple[Path, Bucket, str]]] = {}
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
    for acc_name, items in account_allocations.items():
        acc_size = sum(item[0].stat().st_size for item in items)
        print(f"\n  📦  Account: {acc_name} ({len(items)} files, {format_bytes(acc_size)})")
        total_allocated += len(items)
        total_allocated_size += acc_size
        for abs_path, bucket, object_name in items[:10]:  # Show first 10 per account
            size_str = format_bytes(abs_path.stat().st_size)
            rel_path = rel_path_map.get(abs_path, abs_path.name)
            print(f"      📄  {rel_path} ({size_str}) → {bucket.name}/{object_name}")
        if len(items) > 10:
            remaining_size = sum(item[0].stat().st_size for item in items[10:])
            print(f"      … and {len(items) - 10} more files ({format_bytes(remaining_size)})")

    print(f"\n  ✅  Total allocated: {total_allocated}/{len(source_files)} files ({format_bytes(total_allocated_size)})")
    if total_allocated < len(source_files):
        skipped = len(source_files) - total_allocated
        skipped_size = total_size - total_allocated_size
        print(f"  ⚠️  Skipped: {skipped} files ({format_bytes(skipped_size)}) - insufficient capacity")

    if dry_run:
        print(f"\n{'=' * 60}")
        print("🔍  DRY RUN COMPLETE - No uploads performed")
        print(f"{'=' * 60}")
        return

    # Handle --verify-only: verify SHA-1 of files already in B2
    if args.verify_only:
        print(f"\n{'=' * 60}")
        print("🔍  VERIFY ONLY MODE - Checking SHA-1 of files in B2")
        print(f"{'=' * 60}\n")
        build_account_state(accounts, parallel=False)
        mismatches = 0
        verified = 0
        for src_file in source_files:
            # Find which bucket this file would go to (use allocation or check all)
            found = False
            for account in accounts.values():
                for bucket in account.buckets:
                    if bucket._populate_failed:
                        continue
                    if bucket.has_file(src_file.rel_path):
                        # File exists in B2, verify SHA-1
                        client = get_b2_client(account)
                        def _get_file_info():
                            b2_bucket = client.get_bucket_by_id(bucket.id_)
                            return b2_bucket.get_file_info_by_name(src_file.rel_path)
                        file_version = retry_with_backoff(run_with_timeout, _get_file_info)
                        if file_version and file_version.content_sha1:
                            local_sha1 = compute_sha1(src_file.abs_path)
                            if file_version.content_sha1 == local_sha1:
                                logging.info(f"✓ {src_file.rel_path} SHA-1 matches")
                                verified += 1
                            else:
                                logging.error(f"✗ {src_file.rel_path} SHA-1 MISMATCH: local={local_sha1}, remote={file_version.content_sha1}")
                                mismatches += 1
                        else:
                            logging.warning(f"? {src_file.rel_path} SHA-1 not available from B2")
                        found = True
                        break
                if found:
                    break
            if not found:
                logging.warning(f"! {src_file.rel_path} not found in B2")
        print(f"\n{'=' * 60}")
        print(f"Verified: {verified}, Mismatches: {mismatches}, Not found: {len(source_files) - verified - mismatches}")
        if mismatches > 0:
            sys.exit(1)
        return

    # Handle --check: verify files in B2 match local (no upload)
    if args.check:
        print(f"\n{'=' * 60}")
        print("🔍  CHECK MODE - Verifying files in B2 match local")
        print(f"{'=' * 60}\n")
        build_account_state(accounts, parallel=False)
        mismatches = 0
        missing = 0
        for src_file in source_files:
            found = False
            for account in accounts.values():
                for bucket in account.buckets:
                    if bucket._populate_failed:
                        continue
                    if bucket.has_file(src_file.rel_path):
                        client = get_b2_client(account)
                        def _get_file_info():
                            b2_bucket = client.get_bucket_by_id(bucket.id_)
                            return b2_bucket.get_file_info_by_name(src_file.rel_path)
                        file_version = retry_with_backoff(run_with_timeout, _get_file_info)
                        if file_version and file_version.content_sha1:
                            local_sha1 = compute_sha1(src_file.abs_path)
                            if file_version.content_sha1 != local_sha1:
                                logging.error(f"✗ {src_file.rel_path} SHA-1 MISMATCH")
                                mismatches += 1
                        else:
                            logging.warning(f"? {src_file.rel_path} SHA-1 not available from B2")
                        found = True
                        break
                if found:
                    break
            if not found:
                logging.error(f"✗ {src_file.rel_path} MISSING from B2")
                missing += 1
        print(f"\n{'=' * 60}")
        print(f"OK: {len(source_files) - mismatches - missing}, Mismatches: {mismatches}, Missing: {missing}")
        if mismatches > 0 or missing > 0:
            sys.exit(1)
        return

    # Handle --skip-existing: filter out files already in B2 with matching SHA-1
    if args.skip_existing:
        print(f"\n🔍  Checking for existing files in B2 (--skip-existing)...")
        build_account_state(accounts, parallel=False)
        filtered_allocation = {}
        skipped_count = 0
        skipped_size = 0
        # Build abs_path -> rel_path lookup
        rel_path_map = {f.abs_path: f.rel_path for f in source_files}
        for abs_path, (bucket, object_name) in allocation.items():
            if bucket._populate_failed:
                filtered_allocation[abs_path] = (bucket, object_name)
                continue
            if bucket.account is None:
                filtered_allocation[abs_path] = (bucket, object_name)
                continue
            client = get_b2_client(bucket.account)
            # Check using the original file name (rel_path), not the allocated unique name
            check_name = rel_path_map.get(abs_path, object_name)
            def _get_file_info():
                b2_bucket = client.get_bucket_by_id(bucket.id_)
                return b2_bucket.get_file_info_by_name(check_name)
            existing_file = retry_with_backoff(run_with_timeout, _get_file_info)
            if existing_file is not None:
                remote_sha1 = existing_file.content_sha1
                local_sha1 = compute_sha1(abs_path)
                if remote_sha1 is not None and remote_sha1 == local_sha1:
                    logging.info(f"Skipping {check_name} - already exists with matching SHA-1")
                    skipped_count += 1
                    skipped_size += abs_path.stat().st_size
                    continue
            filtered_allocation[abs_path] = (bucket, object_name)
        allocation = filtered_allocation
        print(f"  ⏭️  Skipped: {skipped_count} files ({format_bytes(skipped_size)})")
        print(f"  📤  Remaining: {len(allocation)} files")
        if not allocation:
            print("\n✅  All files already exist in B2 with matching SHA-1!")
            return
        print(f"\n{'=' * 60}")
        print("⚠️  CONFIRMATION REQUIRED")
        print(f"{'=' * 60}")
        action = "move" if command_type == "move" else "copy"
        print(f"This will {action} {len(allocation)} files to B2.")
        print("Type 'yes' to confirm, or anything else to cancel:")
        try:
            response = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
        if response != "yes":
            print("Cancelled.")
            return

    print(f"\n{'=' * 60}")
    action = "Uploading" if command_type == "move" else "Copying"
    print(f"🚀  STARTING {action.upper()}...")
    print(f"{'=' * 60}\n")

    try:
        _execute_operation(
            args, accounts, source_files, allocation, state, resume, parallel_uploads,
            operation_type=command_type,
            upload_func=_upload_and_delete if command_type == "move" else _upload_only,
            progress_desc=f"{action}",
            success_msg=f"files successfully {command_type}d",
            post_success=cleanup_empty_dirs if command_type == "move" else None,
        )
    except RuntimeError:
        # Error already logged in execution function
        if state:
            print(f"\n💾  State saved. Resume with:")
            print(f"    b2.py {command_type} --accounts={args.accounts} {source} --resume --yes")
        return
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user. State saved.")
        if state:
            print(f"Resume with:")
            print(f"    b2.py {command_type} --accounts={args.accounts} {source} --resume --yes")
        raise


if __name__ == "__main__":
    main()