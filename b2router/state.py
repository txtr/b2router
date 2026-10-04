"""State persistence for B2 Router operations."""

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import AllocationEntry, Bucket
from .platform import get_state_file_path, secure_file
from .exceptions import StateError
from .hashing import compute_sha1


class OperationState:
    """Persisted state for resume capability (used by both move and copy)."""

    def __init__(
        self,
        source_dir: str,
        accounts_config: str,
        created_at: str,
        updated_at: str,
        allocations: list[AllocationEntry],
        _checksum: str = "",
    ):
        self.source_dir = source_dir
        self.accounts_config = accounts_config
        self.created_at = created_at
        self.updated_at = updated_at
        self.allocations = allocations
        self._checksum = _checksum
        self._config_checksum_cached: str = ""
        self._lock = threading.Lock()

    @staticmethod
    def _compute_checksum(allocations: list[AllocationEntry]) -> str:
        """Compute SHA-256 checksum of allocations data for integrity verification."""
        import hashlib
        data = json.dumps([asdict(e) for e in allocations], sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()

    @classmethod
    def create(cls, source_dir: str, accounts_config: str,
               allocation: dict[Path, tuple[Bucket, str]]) -> 'OperationState':
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
            # Use Path.relative_to for correct relative path computation
            try:
                rel_path = str(abs_path.relative_to(Path(source_dir).resolve())).replace(os.sep, "/")
            except ValueError:
                rel_path = abs_path.name
            entries.append(AllocationEntry(
                abs_path=str(abs_path),
                size=abs_path.stat().st_size,
                rel_path=rel_path,
                bucket_name=bucket.name,
                bucket_id=bucket.id_,
                account_name=bucket.account.name,
                object_name=object_name,
                sha1=file_sha1,
                uploaded=False
            ))
        now = datetime.utcnow().isoformat() + "Z"
        return cls(
            source_dir=source_dir,
            accounts_config=accounts_config,
            created_at=now,
            updated_at=now,
            allocations=entries,
            _checksum=cls._compute_checksum(entries)
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        data = {
            "source_dir": self.source_dir,
            "accounts_config": self.accounts_config,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "allocations": [asdict(e) for e in self.allocations],
            "_checksum": self._checksum
        }
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> 'OperationState':
        """Deserialize from JSON string with integrity verification."""
        data = json.loads(json_str)
        # Verify integrity checksum
        allocations_data = data.get("allocations", [])
        expected_checksum = data.get("_checksum", "")
        if expected_checksum:
            actual_checksum = cls._compute_checksum([AllocationEntry(**e) for e in allocations_data])
            if actual_checksum != expected_checksum:
                raise StateError("State file integrity check failed: checksum mismatch")
        return cls(
            source_dir=data["source_dir"],
            accounts_config=data["accounts_config"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            allocations=[AllocationEntry(**e) for e in allocations_data],
            _checksum=expected_checksum
        )

    def save(self, source_dir: str) -> None:
        """Save state to platform-appropriate cache directory with atomic write."""
        with self._lock:
            state_file = get_state_file_path(source_dir)
            # Update checksum before saving
            self._checksum = self._compute_checksum(self.allocations)
            # Atomic write: write to temp file then rename
            temp_file = state_file.with_suffix(".tmp")
            state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file.write_text(self.to_json())
            temp_file.replace(state_file)
            secure_file(state_file)

    @classmethod
    def load(cls, source_dir: str) -> Optional['OperationState']:
        """Load state from platform-appropriate cache directory."""
        state_file = get_state_file_path(source_dir)
        if not state_file.exists():
            return None
        try:
            return cls.from_json(state_file.read_text())
        except StateError:
            # Integrity check failed - don't resume with corrupted state
            raise
        except Exception as exc:
            raise StateError(f"Failed to load state file: {exc}") from exc

    @staticmethod
    def _config_checksum(config_path: str) -> str:
        """Compute SHA-256 checksum of config file content."""
        import hashlib
        try:
            return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        except Exception:
            return ""

    def validate_config(self, accounts_config: str) -> bool:
        """Validate that the state matches the current config (by content hash)."""
        if not self._config_checksum_cached:
            self._config_checksum_cached = self._config_checksum(self.accounts_config)
        current_checksum = self._config_checksum(accounts_config)
        return self._config_checksum_cached == current_checksum

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