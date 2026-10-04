"""Retry utilities with exponential backoff for B2 Router."""

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from .exceptions import B2RouterError

T = TypeVar('T')

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
RETRY_MAX_DELAY = 30.0  # seconds

# Overall timeout for B2 API calls (seconds)
B2_API_TIMEOUT = 300


def retry_with_backoff(
    func: Callable[..., T],
    *args,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    retry_exceptions: tuple[type[Exception], ...] = (ConnectionError, TimeoutError, OSError, IOError),
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
        raise B2RouterError("Retry failed with no exception recorded")
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