"""B2 Router - Route files from local directory to Backblaze B2 buckets."""

__version__ = "1.0.0"

from .models import (
    FileMetadata,
    Bucket,
    Account,
    SourceFile,
    AllocationEntry,
)
from .state import OperationState
from .exceptions import (
    B2RouterError,
    ConfigError,
    StateError,
    AllocationError,
    UploadError,
    VerificationError,
)

__all__ = [
    "__version__",
    "FileMetadata",
    "Bucket",
    "Account",
    "SourceFile",
    "AllocationEntry",
    "OperationState",
    "B2RouterError",
    "ConfigError",
    "StateError",
    "AllocationError",
    "UploadError",
    "VerificationError",
]