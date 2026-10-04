"""Configuration loading for B2 Router."""

import yaml
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Account
from .exceptions import ConfigError

if TYPE_CHECKING:
    from .models import Account


def load_config(config_path: str) -> dict[str, Account]:
    """Load accounts from YAML configuration."""
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        raise ConfigError(f"Configuration file not found: {cfg_path}")

    with open(cfg_path, "r") as f:
        raw = yaml.safe_load(f) or {}

    accounts: dict[str, Account] = {}
    raw_accounts = raw.get("accounts", {}) or {}

    # Check for duplicate account IDs across accounts
    seen_account_ids: set[str] = set()
    for acc_name, acc_data in raw_accounts.items():
        try:
            account_id = acc_data["account_id"]
            master_key = acc_data["master_key"]
            capacity_in_gb = acc_data["capacity_in_gb"]

            # Validate credentials are non-empty
            if not account_id or not str(account_id).strip():
                raise ConfigError(f"account_id is empty in account '{acc_name}'")
            if not master_key or not str(master_key).strip():
                raise ConfigError(f"master_key is empty in account '{acc_name}'")
            if not isinstance(capacity_in_gb, int) or capacity_in_gb <= 0:
                raise ConfigError(f"capacity_in_gb must be a positive integer in account '{acc_name}'")

            # Check for duplicate account_id
            if account_id in seen_account_ids:
                raise ConfigError(f"Duplicate account_id '{account_id}' found (also used by another account)")
            seen_account_ids.add(account_id)

            accounts[acc_name] = Account(
                name=acc_name,
                account_id=account_id,
                master_key=master_key,
                capacity_in_gb=capacity_in_gb
            )
        except KeyError:
            raise
    return accounts