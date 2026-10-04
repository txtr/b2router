"""Execution controller for B2 Router operations."""

import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import Callable, Optional, Tuple, TYPE_CHECKING

from tqdm import tqdm

from .models import SourceFile, AllocationEntry
from .state import OperationState
from .exceptions import B2RouterError

if TYPE_CHECKING:
    from .models import Account, Bucket

# Type aliases
UploadFunc = Callable[['Bucket', Path, str, int], Tuple[Path, bool, Optional[str]]]


class ExecutionError(B2RouterError):
    """Error during operation execution."""
    pass


def execute_operation(
    args: argparse.Namespace,
    accounts: dict[str, 'Account'],
    source_files: list[SourceFile],
    allocation: dict[Path, tuple['Bucket', str]],
    state: Optional[OperationState] = None,
    resume: bool = False,
    max_workers: int = 1,
    operation_type: str = "move",
    upload_func: Optional[UploadFunc] = None,
    progress_desc: str = "Uploading",
    success_msg: str = "files successfully moved",
    post_success: Optional[Callable[[str], None]] = None,
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
    from .b2_client import build_account_state
    from .hashing import compute_sha1

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

                                # Verify bucket still exists and account has capacity
                                if bucket._populate_failed:
                                    logging.warning(f"Bucket {bucket.name} population failed, cannot verify capacity, skipping {entry.abs_path}")
                                    found = True
                                    break

                                # Check account capacity
                                account_used = sum(b.used_bytes for b in account.buckets if not b._populate_failed)
                                account_capacity = int(account.capacity_in_gb * (1024 ** 3) * 0.99)
                                account_remaining = account_capacity - account_used

                                # Get source file size for capacity check
                                source_path = Path(entry.abs_path)
                                if source_path.exists():
                                    file_size = source_path.stat().st_size
                                    # Verify local file hasn't changed since state was saved
                                    if entry.size != file_size:
                                        logging.warning(f"Local file {entry.abs_path} size changed ({entry.size} -> {file_size}), will re-upload")
                                    elif entry.sha1:
                                        local_sha1 = compute_sha1(source_path)
                                        if local_sha1 != entry.sha1:
                                            logging.warning(f"Local file {entry.abs_path} SHA-1 changed, will re-upload")
                                    else:
                                        # State lacks SHA-1 (old format), compute and verify
                                        local_sha1 = compute_sha1(source_path)
                                        logging.info(f"Computed SHA-1 for {entry.abs_path} (state missing hash): {local_sha1}")
                                    if account_remaining < file_size:
                                        logging.warning(f"Account {account.name} has insufficient capacity, skipping {entry.abs_path}")
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

    if upload_func is None:
        raise ValueError("upload_func is required")

    def process_item(item: tuple[Path, tuple['Bucket', str]]) -> Tuple[Path, bool, Optional[str]]:
        abs_path, (bucket, object_name) = item
        size = abs_path.stat().st_size
        return upload_func(bucket, abs_path, object_name, size)

    success = 0
    failed = False
    error_msg = None
    files_since_save = 0
    SAVE_BATCH_SIZE = 5  # Save state every N files
    save_lock = threading.Lock()  # Protect files_since_save and state.save()

    # Progress bar or simple log
    if simple_log:
        logging.info(f"Starting {progress_desc.lower()} {total_files} files...")
        pbar = None
    else:
        pbar = tqdm(total=total_files, desc=progress_desc, unit="file",
                    disable=args.quiet, leave=True,
                    bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    processed_nonlocal = [0]

    def log_progress(path: Path, ok: bool, err: Optional[str] = None) -> None:
        """Log progress for simple-log mode."""
        if simple_log:
            processed_nonlocal[0] += 1
            current = processed_nonlocal[0]
            # Use relative path for clarity (from source_dir)
            try:
                rel: str = str(path.relative_to(args.source))
            except ValueError:
                rel = path.name
            if ok:
                logging.info(f"[{current}/{total_files}] ✓ {rel}")
            else:
                logging.error(f"[{current}/{total_files}] ✗ {rel}: {err}")

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
            # Parallel uploads - submit in batches to avoid unbounded queue
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                batch_size = max_workers * 4  # Keep queue bounded
                pending = list(items)
                future_to_path = {}

                def submit_batch(batch):
                    for item in batch:
                        future_to_path[executor.submit(process_item, item)] = item[0]

                # Submit initial batch
                submit_batch(pending[:batch_size])
                pending = pending[batch_size:]

                while future_to_path:
                    # Wait for at least one to complete
                    done, _ = wait(future_to_path.keys(), return_when=FIRST_COMPLETED)
                    for future in done:
                        path = future_to_path.pop(future)
                        path_res, ok, err = future.result()
                        log_progress(path_res, ok, err)
                        if ok:
                            success += 1
                            # Thread-safe state save batching
                            with save_lock:
                                files_since_save += 1
                                if state:
                                    state.mark_uploaded(str(path_res))
                                    if files_since_save >= SAVE_BATCH_SIZE:
                                        state.save(args.source)
                                        files_since_save = 0
                        else:
                            failed = True
                            error_msg = err
                            # Cancel remaining futures
                            for f in future_to_path:
                                f.cancel()
                            future_to_path.clear()
                            break
                        if pbar:
                            pbar.update(1)

                    # Submit more work to keep workers busy
                    if not failed and pending:
                        next_batch = pending[:batch_size]
                        pending = pending[batch_size:]
                        submit_batch(next_batch)
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
        raise ExecutionError(error_msg)

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