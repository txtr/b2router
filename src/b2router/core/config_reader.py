"""Strict, side-effect-free parsing of ``accounts.yaml``.

The reader validates the whole document before the server touches B2. Errors
are structured and never echo application keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from b2router.services.pool import Err, Ok


@dataclass(frozen=True)
class ConfigError:
    kind: str
    message: str


@dataclass(frozen=True)
class AccountSpec:
    name: str
    application_key_id: str
    application_key: str
    capacity_bytes: int | None = None


@dataclass(frozen=True)
class Config:
    max_files_per_account: int
    accounts: list[AccountSpec]


def account_name(application_key_id: str) -> str:
    """Deterministic, human-friendly account name."""
    return f"account-{application_key_id[:8]}"


class ConfigReader:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> Ok[Config, ConfigError] | Err[Config, ConfigError]:
        if not self.path.exists():
            return Err(ConfigError("file_not_found", f"Config file not found: {self.path}"))
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            return Err(ConfigError("read_error", f"Could not read {self.path}: {exc}"))

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return Err(ConfigError("invalid_yaml", f"Invalid YAML: {exc}"))

        if data is None:
            return Err(ConfigError("empty_yaml", "The configuration file is empty."))
        if not isinstance(data, dict):
            return Err(ConfigError("invalid_yaml", "The configuration root must be a mapping."))

        max_files = data.get("max_files_per_account", 1700)
        if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files <= 0:
            return Err(
                ConfigError(
                    "invalid_max_files",
                    "max_files_per_account must be a positive integer.",
                )
            )

        accounts_raw = data.get("accounts")
        if not isinstance(accounts_raw, list) or not accounts_raw:
            return Err(
                ConfigError(
                    "missing_accounts", "The configuration must define a non-empty 'accounts' list."
                )
            )

        accounts: list[AccountSpec] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for index, item in enumerate(accounts_raw):
            if not isinstance(item, dict):
                return Err(ConfigError("invalid_account", f"Account #{index} must be a mapping."))
            key_id = item.get("account_id")
            key = item.get("account_key")
            capacity_mb = item.get("capacity_in_mb")
            missing = [
                field
                for field, value in (("account_id", key_id), ("account_key", key))
                if not isinstance(value, str) or not value.strip()
            ]
            if missing:
                return Err(
                    ConfigError(
                        "missing_account_fields",
                        f"Account #{index} is missing or has an empty {', '.join(missing)}.",
                    )
                )
            assert isinstance(key_id, str) and isinstance(key, str)
            key_id = key_id.strip()
            key = key.strip()
            if key_id in seen_ids:
                return Err(
                    ConfigError(
                        "duplicate_account",
                        f"Account #{index} repeats account_id {key_id[:4]}…; account ids must be unique.",
                    )
                )
            seen_ids.add(key_id)

            if capacity_mb is not None:
                if (
                    not isinstance(capacity_mb, int)
                    or isinstance(capacity_mb, bool)
                    or capacity_mb < 0
                ):
                    return Err(
                        ConfigError(
                            "invalid_capacity",
                            f"Account #{index}: capacity_in_mb must be a non-negative integer.",
                        )
                    )
                capacity_bytes = capacity_mb * 1024 * 1024
            else:
                capacity_bytes = None

            name = account_name(key_id)
            if name in seen_names:
                return Err(
                    ConfigError(
                        "duplicate_account_name",
                        f"Account #{index} derives the same display name {name!r} as another account.",
                    )
                )
            seen_names.add(name)
            accounts.append(
                AccountSpec(
                    name=name,
                    application_key_id=key_id,
                    application_key=key,
                    capacity_bytes=capacity_bytes,
                )
            )

        return Ok(Config(max_files_per_account=max_files, accounts=accounts))
