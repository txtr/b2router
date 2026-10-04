"""B2 API client wrapper for B2 Router."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent import futures
from typing import Callable, TypeVar

from b2sdk.v2 import B2Api, InMemoryAccountInfo
from b2sdk.v2.exception import B2Error

from .models import Account, Bucket
from .exceptions import AuthenticationError, BucketError
from .retry import retry_with_backoff, run_with_timeout
from .allocation import CAP_SAFETY

T = TypeVar('T')

# Token expiration handling
B2_TOKEN_EXPIRY_SECONDS = 24 * 3600  # 24 hours


def _call_with_token_refresh(account: Account, func: Callable[[B2Api], T]) -> T:
    """Call a function with B2Api, refreshing token on 401 errors."""
    client = get_b2_client(account)
    try:
        return func(client)
    except B2Error as exc:
        # Check for 401 Unauthorized (expired token)
        if "401" in str(exc) or "unauthorized" in str(exc).lower() or "expired" in str(exc).lower():
            logging.warning(f"Token expired for account {account.name}, re-authorizing...")
            # Force re-authorization on next get_b2_client call
            account.client = None
            client = get_b2_client(account)
            return func(client)
        raise


def get_b2_client(account: Account) -> B2Api:
    """Authenticate and return a B2Api client for the given account.

    Uses InMemoryAccountInfo to isolate each account's authorization cache,
    preventing cross-account cache pollution from shared SqliteAccountInfo.
    """
    # Invalidate cached client if realm changed
    if account.client is not None and account._cached_realm != account.realm:
        logging.debug(f"Realm changed ({account._cached_realm} -> {account.realm}), recreating B2Api client for {account.name}")
        account.client = None

    if account.client is None:
        logging.debug(f"Creating NEW B2Api client for account {account.name}")
        account.client = B2Api(InMemoryAccountInfo())
        account._cached_realm = account.realm

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

    def _list_buckets(client: B2Api):
        return client.list_buckets()

    b2_buckets_info = retry_with_backoff(run_with_timeout, lambda: _call_with_token_refresh(account, _list_buckets))
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


def populate_bucket_files_and_usage(bucket: Bucket) -> None:
    """Query B2 for all objects in the bucket and populate details.

    If population fails, the bucket is marked as failed and will be skipped
    during allocation to prevent over-allocation based on stale/zero usage data.
    """
    if bucket.account is None:
        raise BucketError("Bucket must have an associated account")
    bucket.files.clear()
    bucket._file_names.clear()
    bucket._populate_failed = False

    def _list_and_process(client: B2Api) -> int:
        """List and process all objects in the bucket incrementally (streaming).
        Returns total bytes processed.
        """
        total_local = 0
        b2_bucket = client.get_bucket_by_id(bucket.id_)
        # Stream objects instead of loading all into memory
        for fv, _ in b2_bucket.ls(recursive=True):
            total_local += fv.size
            bucket.add_file(fv.file_name, fv.size, fv.content_sha1)
        return total_local

    try:
        # Retry wraps the entire streaming operation with timeout
        total = retry_with_backoff(run_with_timeout, lambda: _call_with_token_refresh(bucket.account, _list_and_process))  # type: ignore[arg-type]
    except (B2Error, ConnectionError, TimeoutError, OSError, IOError) as exc:
        logging.warning(f"Error listing objects in {bucket.name}: {exc}")
        bucket._populate_failed = True
        # Don't set used_bytes - keep previous value or 0 to avoid false empty state
        return
    except Exception as exc:
        # Catch-all for unexpected errors - log and re-raise to surface bugs
        logging.error(f"Unexpected error listing objects in {bucket.name}: {exc}")
        bucket._populate_failed = True
        raise

    if not bucket._populate_failed:
        bucket.used_bytes = total


def _process_account(account: Account) -> None:
    """Process a single account: discover buckets and populate file metadata."""
    discover_and_add_buckets_for_account(account)
    for bucket in account.buckets:
        populate_bucket_files_and_usage(bucket)


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
            errors = []
            for future in futures.as_completed(future_to_account):
                account = future_to_account[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"Account {account.name}: {exc}")
                    logging.error(f"Failed to process account {account.name}: {exc}")
            if errors:
                raise RuntimeError(f"Failed to process {len(errors)} account(s): " + "; ".join(errors))
    else:
        # Sequential with small delay between accounts to avoid rate limiting
        for i, account in enumerate(accounts.values()):
            if i > 0:
                time.sleep(0.5)  # Rate limit mitigation
            _process_account(account)