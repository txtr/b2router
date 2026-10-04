"""Data models for B2 Router."""

import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


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
    client: 'B2Api' = field(default=None, repr=False)  # type: ignore[name-defined]
    realm: str = "production"  # B2 realm (production or test)
    _cached_realm: str = field(default="production", repr=False, init=False)

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