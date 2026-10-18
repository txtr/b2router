from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from b2router.services.account import Account


@dataclass
class FileCacheEntry:
    """Cache entry for a file across multiple accounts (built at startup, never modified)."""

    file_name: str
    file_id: str
    size: int
    content_type: str
    upload_timestamp: int
    action: str
    accounts: list[str]  # Account names that have this file

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "file_id": self.file_id,
            "size": self.size,
            "content_type": self.content_type,
            "upload_timestamp": self.upload_timestamp,
            "action": self.action,
            "accounts": self.accounts.copy(),
            "account_count": len(self.accounts),
        }


@dataclass
class AccountState:
    """Runtime state for an account (capacity tracking, file count)."""

    name: str
    account: Account
    capacity_bytes: int  # Total capacity from config
    used_bytes: int = 0
    file_count: int = 0
    max_files: int = 1700  # Max files per bucket for this account

    @property
    def remaining_bytes(self) -> int:
        return self.capacity_bytes - self.used_bytes

    @property
    def remaining_files(self) -> int:
        return self.max_files - self.file_count

    @property
    def utilization(self) -> float:
        """Capacity utilization ratio (0.0 to 1.0)."""
        if self.capacity_bytes == 0:
            return 0.0
        return self.used_bytes / self.capacity_bytes

    def can_fit(self, file_size: int) -> bool:
        """Check if file fits in both file count and capacity."""
        return self.remaining_files > 0 and self.remaining_bytes >= file_size

    def add_file(self, file_size: int) -> None:
        """Update usage after adding a file."""
        self.used_bytes += file_size
        self.file_count += 1


class AccountPool:
    """Pool managing multiple B2 Account instances with centralized file cache (built once at startup)."""

    def __init__(
        self,
        connect_timeout: float = 30.0,
        read_timeout: float = 120.0,
    ) -> None:
        self._accounts: dict[str, Account] = {}
        self._states: dict[str, AccountState] = {}  # name -> AccountState
        self._file_cache: dict[str, FileCacheEntry] = {}  # file_name -> FileCacheEntry
        self._default_max_files: int = 1700
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    def set_max_files_per_account(self, max_files: int) -> None:
        """Set the default max files per account for all accounts."""
        assert max_files > 0, "max_files must be positive"
        self._default_max_files = max_files
        # Update existing accounts
        for state in self._states.values():
            state.max_files = max_files

    def configure_timeouts(self, connect_timeout: float, read_timeout: float) -> None:
        """Set network timeouts used when creating new Account instances.

        Mutates the shared singleton in place so that modules which imported the
        singleton (e.g. web.py) observe accounts added afterwards.
        """
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

    def add(self, name: str, application_key_id: str, application_key: str, capacity_mb: int = 0) -> Account:
        """Create and add an Account to the pool. Enriches file cache once at startup."""
        assert name not in self._accounts, f"Account '{name}' already exists"
        account = Account(
            application_key_id=application_key_id,
            application_key=application_key,
            name=name,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
        )
        self._accounts[name] = account
        capacity_bytes = capacity_mb * 1024 * 1024
        self._states[name] = AccountState(
            name=name,
            account=account,
            capacity_bytes=capacity_bytes,
            max_files=self._default_max_files,
        )
        self._enrich_file_cache(name, account)
        return account

    def _enrich_file_cache(self, account_name: str, account: Account) -> None:
        """Fetch files from account and build centralized file cache (once at startup)."""
        files = account.list_files()
        state = self._states[account_name]
        for f in files:
            file_name = f["file_name"]
            if file_name in self._file_cache:
                existing = self._file_cache[file_name]
                assert False, f"Duplicate filename '{file_name}' found in account '{account_name}' (already in account '{existing.accounts[0]}')"
            else:
                self._file_cache[file_name] = FileCacheEntry(
                    file_name=f["file_name"],
                    file_id=f["file_id"],
                    size=f["size"],
                    content_type=f["content_type"],
                    upload_timestamp=f["upload_timestamp"],
                    action=f["action"],
                    accounts=[account_name],
                )
            # Update state with actual usage
            state.used_bytes += f["size"]
            state.file_count += 1

    def get(self, name: str) -> Account | None:
        """Get account by name."""
        return self._accounts.get(name)

    def require(self, name: str) -> Account:
        """Get account by name, panic if not found."""
        account = self._accounts.get(name)
        assert account is not None, f"Account '{name}' not found"
        return account

    def remove(self, name: str) -> None:
        """Remove account from pool (not supported after cache built)."""
        assert False, "Cannot remove individual account after cache built."

    def __getitem__(self, name: str) -> Account:
        return self.require(name)

    def __contains__(self, name: str) -> bool:
        return name in self._accounts

    def __len__(self) -> int:
        return len(self._accounts)

    def __iter__(self):
        return iter(self._accounts.values())

    def names(self) -> list[str]:
        return list(self._accounts.keys())

    # --- File Cache Methods (read-only after startup) ---

    def get_file(self, file_name: str) -> FileCacheEntry | None:
        """Get file cache entry by name."""
        return self._file_cache.get(file_name)

    def get_file_accounts(self, file_name: str) -> list[str]:
        """Get list of account names that have this file."""
        entry = self._file_cache.get(file_name)
        return entry.accounts.copy() if entry else []

    def list_files(self) -> list[dict[str, Any]]:
        """List all unique files across all accounts with their metadata."""
        return [entry.to_dict() for entry in self._file_cache.values()]

    def list_files_for_account(self, account_name: str) -> list[dict[str, Any]]:
        """List files that belong to a specific account."""
        return [
            entry.to_dict()
            for entry in self._file_cache.values()
            if account_name in entry.accounts
        ]

    def file_cache_stats(self) -> dict[str, Any]:
        """Get file cache statistics."""
        total_files = len(self._file_cache)
        total_size = sum(e.size for e in self._file_cache.values())
        total_account_refs = sum(len(e.accounts) for e in self._file_cache.values())
        all_accounts = set()
        for e in self._file_cache.values():
            all_accounts.update(e.accounts)
        return {
            "unique_files": total_files,
            "total_size_bytes": total_size,
            "total_account_references": total_account_refs,
            "accounts_with_files": len(all_accounts),
        }

    # --- Upload Routing (Best-Fit Packing) ---

    def select_best_account(self, file_size: int) -> Account | None:
        """Select best-fit account for a file (maximizes packing efficiency).

        Returns account with least remaining capacity after adding the file
        (i.e., most full account that can still fit the file).
        Returns None if no account can fit.
        """
        best_account: Account | None = None
        best_remaining: int | None = None

        for name, state in self._states.items():
            if not state.can_fit(file_size):
                continue
            remaining_after = state.remaining_bytes - file_size
            if best_remaining is None or remaining_after < best_remaining:
                best_remaining = remaining_after
                best_account = state.account

        return best_account

    def upload_file(
        self,
        file_name: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> Account:
        """Upload file to best-fit account. Fails if file already exists."""
        # Fail if file already exists in cache (uploaded at startup)
        if file_name in self._file_cache:
            raise AssertionError(f"File '{file_name}' already exists in the pool")

        file_size = len(content)
        account = self.select_best_account(file_size)
        assert account is not None, f"No account can fit file of size {file_size} bytes"

        # Upload to B2
        file_version = account.bucket.upload_bytes(content, file_name, content_type=content_type)

        # Update state
        state = self._states[account.name]
        state.add_file(file_size)

        # Update file cache
        self._file_cache[file_name] = FileCacheEntry(
            file_name=file_version.file_name,
            file_id=file_version.id_,
            size=file_version.size,
            content_type=file_version.content_type,
            upload_timestamp=file_version.upload_timestamp,
            action=file_version.action,
            accounts=[account.name],
        )

        return account

    def get_account_state(self, name: str) -> AccountState | None:
        """Get runtime state for an account."""
        return self._states.get(name)

    def get_all_states(self) -> dict[str, AccountState]:
        """Get all account states."""
        return dict(self._states)

    def get_default(self) -> Account | None:
        """Get the first account (default), or None if empty."""
        return next(iter(self._accounts.values()), None)

    def stats(self) -> dict[str, Any]:
        """Get pool statistics."""
        return {
            "active": len(self._accounts),
            "total": len(self._accounts),
        }


account_pool = AccountPool()
