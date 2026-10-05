"""Simplified config loading for b2router_simple."""

import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import List


@dataclass
class Account:
    name: str
    account_id: str
    master_key: str
    capacity_gb: float = 10.0


def load_config(config_path: str) -> List[Account]:
    """Load accounts from YAML config.

    Expected format:
    accounts:
      account_name:
        account_id: "xxx"
        master_key: "xxx"
        capacity_gb: 10  # optional, default 10
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with path.open() as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}

    accounts = []
    for name, info in data.get("accounts", {}).items():
        accounts.append(Account(
            name=name,
            account_id=info["account_id"],
            master_key=info["master_key"],
            capacity_gb=info.get("capacity_gb", 10.0),
        ))

    if not accounts:
        raise ValueError("No accounts configured")

    return accounts