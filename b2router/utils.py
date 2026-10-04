"""Utility functions for B2 Router."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SourceFile


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


def collect_source_files(source_dir: str) -> list['SourceFile']:
    """Collect all regular files from source directory, skipping broken symlinks.

    Does not follow symlinks to avoid including files outside the source tree.
    Warns if source appears to be a FUSE mount point (e.g., rclone mount) to prevent loops.
    Handles PermissionError on directories gracefully.
    """
    import logging
    import os
    import stat
    from pathlib import Path

    from .models import SourceFile
    from .platform import is_fuse_mount

    files = []
    source_path = Path(source_dir).resolve()

    # Check if source might be a B2 FUSE mount (rclone, etc.)
    if is_fuse_mount(source_path):
        logging.warning(f"Source directory {source_path} appears to be a FUSE mount. "
                      "Uploading from a B2 mount may cause loops or corruption.")

    def walk_with_permission_handling(top):
        """Walk directory tree with PermissionError handling."""
        dirs = []
        nondirs = []
        try:
            with os.scandir(top) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    else:
                        nondirs.append(entry.name)
        except PermissionError as exc:
            logging.warning(f"Permission denied accessing directory {top}: {exc}")
            return

        yield top, dirs, nondirs
        for dirname in dirs:
            new_path = os.path.join(top, dirname)
            yield from walk_with_permission_handling(new_path)

    for root, _, filenames in walk_with_permission_handling(source_path):
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