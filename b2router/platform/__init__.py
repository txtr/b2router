"""Platform-specific utilities for B2 Router."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from ..exceptions import StateError


def get_cache_dir(app_name: str = "b2router") -> Path:
    """Get platform-appropriate cache directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        # Linux/Unix - use XDG_CACHE_HOME or ~/.cache
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / app_name


def get_state_dir(source_dir: str) -> Path:
    """Get the directory for state files, outside the source tree.

    Uses platform-appropriate cache/config directory to avoid
    the state file being uploaded to B2.
    """
    source_path = Path(source_dir).resolve()
    # Create a unique subdir based on source path hash
    path_hash = hashlib.sha256(str(source_path).encode()).hexdigest()[:12]
    return get_cache_dir() / "state" / path_hash


def get_state_file_path(source_dir: str) -> Path:
    """Get the full path to the state file for a source directory."""
    return get_state_dir(source_dir) / ".b2router_state.json"


def secure_file(path: Path) -> None:
    """Restrict file permissions: owner read/write only."""
    if sys.platform == "win32":
        # Use icacls to set ACL: owner full, remove inheritance, remove other users
        try:
            # Disable inheritance and remove inherited ACEs
            subprocess.run(["icacls", str(path), "/inheritance:r"],
                          check=False, capture_output=True)
            # Grant current user full control
            subprocess.run(["icacls", str(path), "/grant:r",
                           f"{os.getlogin()}:(OI)(CI)F"],
                          check=False, capture_output=True)
        except Exception as exc:
            raise StateError(f"Failed to secure file on Windows: {exc}") from exc
    else:
        try:
            path.chmod(0o600)
        except Exception as exc:
            raise StateError(f"Failed to chmod file: {exc}") from exc


def is_fuse_mount(path: Path) -> bool:
    """Check if a path appears to be a FUSE mount (Linux only)."""
    if sys.platform != "linux":
        return False
    try:
        mounts = Path("/proc/mounts").read_text()
        return "fuse" in mounts.lower() and str(path.resolve()) in mounts
    except Exception:
        return False