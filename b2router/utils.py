"""Simplified utilities for b2router_simple."""

import os
from pathlib import Path
from dataclasses import dataclass
from typing import List


@dataclass
class SourceFile:
    path: Path
    size: int
    rel_path: str


def collect_source_files(source_dir: str) -> List[SourceFile]:
    """Collect all files under source_dir with relative paths."""
    root = Path(source_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {source_dir}")

    files = []
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            abs_path = Path(dirpath) / fname
            try:
                size = abs_path.stat().st_size
                rel = abs_path.relative_to(root).as_posix()
                files.append(SourceFile(path=abs_path, size=size, rel_path=rel))
            except OSError:
                continue
    return files


def format_bytes(size: int) -> str:
    """Format bytes as human-readable string."""
    size_float = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_float < 1024:
            return f"{size_float:.1f} {unit}"
        size_float /= 1024
    return f"{size_float:.1f} PB"