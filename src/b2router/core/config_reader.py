from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AccountConfig:
    """Configuration for a single B2 account."""

    name: str
    account_id: str
    account_key: str
    capacity_in_mb: int | None = None

    @property
    def capacity_bytes(self) -> int | None:
        if self.capacity_in_mb is None:
            return None
        return self.capacity_in_mb * 1024 * 1024


class ConfigReader:
    """Reads and parses account configuration from YAML file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[AccountConfig]:
        """Read and parse the YAML file, returning list of AccountConfig."""
        assert self.path.exists(), f"Config file not found: {self.path}"

        with self.path.open("r") as f:
            data = yaml.safe_load(f)

        assert data and "accounts" in data, "YAML must contain an 'accounts' list"

        accounts = []
        for acc in data["accounts"]:
            accounts.append(self._parse_account(acc))

        return accounts

    def _parse_account(self, data: dict[str, Any]) -> AccountConfig:
        """Parse a single account entry."""
        name = data.get("name")
        account_id = data.get("account_id")
        account_key = data.get("account_key")
        capacity_mb = data.get("capacity_in_mb")

        assert all([name, account_id, account_key]), f"Account missing required fields: {', '.join(k for k, v in [('name', name), ('account_id', account_id), ('account_key', account_key)] if not v)}"

        return AccountConfig(
            name=str(name),
            account_id=str(account_id),
            account_key=str(account_key),
            capacity_in_mb=capacity_mb,
        )