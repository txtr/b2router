from __future__ import annotations

from typing import Any

from b2router.core.logging import logger
from b2router.services.account import Account


class AccountPool:
    """Pool managing multiple B2 Account instances."""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._failed: dict[str, dict[str, Any]] = {}  # name -> {error, error_type, credentials}

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
        logger.info("account_added", name=name, bucket=account.cache.get("bucket", {}).get("name"))
        return account

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
        """Remove account from pool."""
        if name in self._accounts:
            del self._accounts[name]
            logger.info("account_removed", name=name)

    def remove_failed(self, name: str) -> None:
        """Remove a failed account record, allowing retry."""
        if name in self._failed:
            del self._failed[name]
            logger.info("failed_account_removed", name=name)

    def clear(self) -> None:
        """Remove all accounts."""
        self._accounts.clear()

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

    def refresh_all(self) -> dict[str, bool]:
        """Refresh cache for all accounts. Returns dict of name -> success."""
        results = {}
        for name, account in self._accounts.items():
            try:
                account.refresh_cache()
                results[name] = True
            except Exception as e:
                logger.error("account_refresh_failed", name=name, error_type=type(e).__name__, error=str(e))
                results[name] = False
        return results

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