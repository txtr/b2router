from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from b2router.core.logging import logger
from b2router.services.account import Account


@dataclass
class FileCacheEntry:
    """Cache entry for a file across multiple accounts."""

    file_name: str
    file_id: str
    size: int
    content_type: str
    upload_timestamp: int
    action: str
    accounts: list[str]  # Account names that have this file

    def add_account(self, account_name: str) -> None:
        if account_name not in self.accounts:
            self.accounts.append(account_name)

    def remove_account(self, account_name: str) -> None:
        if account_name in self.accounts:
            self.accounts.remove(account_name)

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


class AccountPool:
    """Pool managing multiple B2 Account instances with centralized file cache."""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._failed: dict[str, dict[str, Any]] = {}  # name -> {error, error_type, credentials}
        self._file_cache: dict[str, FileCacheEntry] = {}  # file_name -> FileCacheEntry

    def add(self, name: str, application_key_id: str, application_key: str) -> Account | None:
        """Create and add an Account to the pool. Returns None if initialization fails."""
        if name in self._accounts:
            raise ValueError(f"Account '{name}' already exists")
        if name in self._failed:
            raise ValueError(f"Account '{name}' previously failed; use remove_failed() first")

        try:
            account = Account(application_key_id=application_key_id, application_key=application_key)
        except Exception as e:
            error_info = {
                "error": str(e),
                "error_type": type(e).__name__,
                "application_key_id": application_key_id,
            }
            self._failed[name] = error_info
            logger.error(
                "account_init_failed",
                name=name,
                error_type=type(e).__name__,
                error=str(e),
            )
            return None

        self._accounts[name] = account
        self._enrich_file_cache(name, account)
        logger.info("account_added", name=name, bucket=account.bucket_name)
        return account

    def _enrich_file_cache(self, account_name: str, account: Account) -> None:
        """Fetch files from account and update centralized file cache."""
        files = account.list_files()
        for f in files:
            file_name = f["file_name"]
            if file_name in self._file_cache:
                entry = self._file_cache[file_name]
                entry.add_account(account_name)
                # Update metadata (use latest from this account)
                entry.file_id = f["file_id"]
                entry.size = f["size"]
                entry.content_type = f["content_type"]
                entry.upload_timestamp = f["upload_timestamp"]
                entry.action = f["action"]
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
        logger.info("file_cache_enriched", account=account_name, files_added=len(files))

    def _remove_account_from_file_cache(self, account_name: str) -> None:
        """Remove account from all file cache entries."""
        to_delete = []
        for file_name, entry in self._file_cache.items():
            entry.remove_account(account_name)
            if not entry.accounts:
                to_delete.append(file_name)
        for file_name in to_delete:
            del self._file_cache[file_name]

    def get(self, name: str) -> Account | None:
        """Get account by name."""
        return self._accounts.get(name)

    def require(self, name: str) -> Account:
        """Get account by name, raise if not found."""
        account = self._accounts.get(name)
        if account is None:
            raise KeyError(f"Account '{name}' not found")
        return account

    def remove(self, name: str) -> None:
        """Remove account from pool and file cache."""
        if name in self._accounts:
            self._remove_account_from_file_cache(name)
            del self._accounts[name]
            logger.info("account_removed", name=name)

    def remove_failed(self, name: str) -> None:
        """Remove a failed account record, allowing retry."""
        if name in self._failed:
            del self._failed[name]
            logger.info("failed_account_removed", name=name)

    def clear(self) -> None:
        """Remove all accounts and clear file cache."""
        self._accounts.clear()
        self._file_cache.clear()

    def clear_failed(self) -> None:
        """Clear all failed account records."""
        self._failed.clear()

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

    def failed_names(self) -> list[str]:
        return list(self._failed.keys())

    def get_failed(self, name: str) -> dict[str, Any] | None:
        """Get failure info for a failed account."""
        return self._failed.get(name)

    def get_all_failed(self) -> dict[str, dict[str, Any]]:
        """Get all failed accounts with their error info."""
        return dict(self._failed)

    # --- File Cache Methods ---

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

    def refresh_all(self) -> dict[str, bool]:
        """Refresh file cache for all accounts. Returns dict of name -> success."""
        results = {}
        # Clear file cache and rebuild
        self._file_cache.clear()
        for name, account in self._accounts.items():
            try:
                self._enrich_file_cache(name, account)
                results[name] = True
            except Exception as e:
                logger.error("account_refresh_failed", name=name, error_type=type(e).__name__, error=str(e))
                results[name] = False
        return results

    def refresh_account(self, account_name: str) -> bool:
        """Refresh file cache for a single account."""
        account = self._accounts.get(account_name)
        if not account:
            return False
        # Remove old entries for this account
        self._remove_account_from_file_cache(account_name)
        # Re-enrich
        try:
            self._enrich_file_cache(account_name, account)
            return True
        except Exception as e:
            logger.error("account_refresh_failed", name=account_name, error_type=type(e).__name__, error=str(e))
            return False

    def get_default(self) -> Account | None:
        """Get the first account (default), or None if empty."""
        return next(iter(self._accounts.values()), None)

    def stats(self) -> dict[str, Any]:
        """Get pool statistics."""
        return {
            "active": len(self._accounts),
            "failed": len(self._failed),
            "total_attempted": len(self._accounts) + len(self._failed),
        }


account_pool = AccountPool()