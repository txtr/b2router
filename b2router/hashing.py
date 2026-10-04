"""SHA-1 computation with caching for B2 Router."""

import hashlib
import logging
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=10000)
def _compute_sha1_cached(path: str, mtime: float, size: int) -> str:
    """Compute SHA-1 hash of a file (cached by path, mtime, size)."""
    sha1 = hashlib.sha1()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha1.update(chunk)
    return sha1.hexdigest()


def compute_sha1(file_path: Path) -> str:
    """Compute SHA-1 hash of a file with caching."""
    try:
        stat_result = file_path.stat()
        cache_key = (str(file_path), stat_result.st_mtime, stat_result.st_size)
    except OSError:
        # If we can't stat, don't cache
        logging.info(f"Computing SHA-1 (no cache): {file_path}")
        sha1 = hashlib.sha1()
        with file_path.open('rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha1.update(chunk)
        return sha1.hexdigest()

    # Log for files >= 1MB (likely to take noticeable time)
    size_mb = stat_result.st_size / (1024 * 1024)
    if size_mb >= 1:
        logging.info(f"Computing SHA-1: {file_path.name} ({size_mb:.1f} MB)")

    return _compute_sha1_cached(*cache_key)