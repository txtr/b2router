"""Exception hierarchy for B2 Router."""


class B2RouterError(Exception):
    """Base exception for all B2 Router errors."""
    pass


class ConfigError(B2RouterError, ValueError):
    """Configuration-related errors (accounts.yaml, invalid values)."""
    pass


class StateError(B2RouterError):
    """State file errors (corruption, version mismatch, validation failure)."""
    pass


class AllocationError(B2RouterError):
    """File allocation errors (insufficient capacity, no valid buckets)."""
    pass


class UploadError(B2RouterError):
    """Upload-related errors (network, B2 API, verification failure)."""
    pass


class VerificationError(B2RouterError):
    """Verification errors (SHA-1 mismatch, missing files)."""
    pass


class AuthenticationError(B2RouterError):
    """B2 authentication/authorization errors."""
    pass


class BucketError(B2RouterError):
    """Bucket-related errors (not found, populate failed)."""
    pass