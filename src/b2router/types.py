"""Semantic type aliases for domain identifiers.

These are erased at runtime but provide static checking against
accidental misuse (e.g., passing a JobId where a FileId is expected).
"""

from __future__ import annotations

from typing import NewType

FileId = NewType("FileId", str)
JobId = NewType("JobId", str)
AccountName = NewType("AccountName", str)
FileName = NewType("FileName", str)
RequestId = NewType("RequestId", str)
ReservationId = NewType("ReservationId", str)
