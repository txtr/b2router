"""Runtime settings for the b2router gateway.

Everything is environment driven so the service can run unchanged in a shell,
a container, or a test harness. Invalid values are collected and reported at
once instead of failing one variable at a time.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


class SettingsError(Exception):
    """Raised when one or more environment values are invalid."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


def _package_version() -> str:
    try:
        return version("b2router")
    except PackageNotFoundError:  # pragma: no cover - editable installs vary
        return "0.1.0"


def _bool(value: str, *, name: str, issues: list[str]) -> bool:
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    issues.append(f"{name} must be a boolean, got {value!r}")
    return False


def _int(value: str, *, name: str, issues: list[str], minimum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        issues.append(f"{name} must be an integer, got {value!r}")
        return minimum
    if parsed < minimum:
        issues.append(f"{name} must be >= {minimum}, got {parsed}")
        return minimum
    return parsed


def _float(value: str, *, name: str, issues: list[str], minimum: float) -> float:
    try:
        parsed = float(value)
    except ValueError:
        issues.append(f"{name} must be a number, got {value!r}")
        return minimum
    if parsed < minimum:
        issues.append(f"{name} must be >= {minimum}, got {parsed}")
        return minimum
    return parsed


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable, validated runtime configuration."""

    host: str = "127.0.0.1"
    port: int = 8000
    accounts_path: str = "accounts.yaml"

    connect_timeout: float = 30.0
    read_timeout: float = 120.0

    max_upload_bytes: int = 100 * 1024 * 1024
    max_files_per_request: int = 20
    max_filename_bytes: int = 1000

    upload_workers: int = 2
    queue_max: int = 100
    job_ttl_seconds: int = 3600
    job_history_max: int = 500

    temp_dir: str = ""
    lock_file: str = ""
    acquire_lock: bool = True

    api_token: str | None = None
    secret_key: str | None = None
    cookie_secure: bool = False
    trust_proxy: bool = False
    mutation_rate_per_minute: int = 240
    cors_origins: tuple[str, ...] = field(default_factory=tuple)

    log_format: str = "pretty"
    log_level: str = "INFO"
    metrics_enabled: bool = True

    version: str = field(default_factory=_package_version)

    def __post_init__(self) -> None:
        if not self.temp_dir:
            object.__setattr__(self, "temp_dir", str(Path(tempfile.gettempdir()) / "b2router"))
        if not self.lock_file:
            object.__setattr__(self, "lock_file", str(Path(self.temp_dir) / "instance.lock"))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        issues: list[str] = []

        def get(name: str, default: str) -> str:
            return env.get(name, default)

        log_format = get("B2ROUTER_LOG_FORMAT", "pretty").strip().lower()
        if log_format not in {"json", "pretty"}:
            issues.append("B2ROUTER_LOG_FORMAT must be 'json' or 'pretty'")

        settings = cls(
            host=get("B2ROUTER_HOST", "127.0.0.1"),
            port=_int(get("B2ROUTER_PORT", "8000"), name="B2ROUTER_PORT", issues=issues, minimum=1),
            accounts_path=get("B2ROUTER_ACCOUNTS", "accounts.yaml"),
            connect_timeout=_float(
                get("B2_CONNECT_TIMEOUT", "30"), name="B2_CONNECT_TIMEOUT", issues=issues, minimum=1
            ),
            read_timeout=_float(
                get("B2_READ_TIMEOUT", "120"), name="B2_READ_TIMEOUT", issues=issues, minimum=1
            ),
            max_upload_bytes=_int(
                get("B2ROUTER_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)),
                name="B2ROUTER_MAX_UPLOAD_BYTES",
                issues=issues,
                minimum=1,
            ),
            max_files_per_request=_int(
                get("B2ROUTER_MAX_FILES_PER_REQUEST", "20"),
                name="B2ROUTER_MAX_FILES_PER_REQUEST",
                issues=issues,
                minimum=1,
            ),
            max_filename_bytes=_int(
                get("B2ROUTER_MAX_FILENAME_BYTES", "1000"),
                name="B2ROUTER_MAX_FILENAME_BYTES",
                issues=issues,
                minimum=1,
            ),
            upload_workers=_int(
                get("B2ROUTER_UPLOAD_WORKERS", "2"),
                name="B2ROUTER_UPLOAD_WORKERS",
                issues=issues,
                minimum=1,
            ),
            queue_max=_int(
                get("B2ROUTER_QUEUE_MAX", "100"),
                name="B2ROUTER_QUEUE_MAX",
                issues=issues,
                minimum=1,
            ),
            job_ttl_seconds=_int(
                get("B2ROUTER_JOB_TTL_SECONDS", "3600"),
                name="B2ROUTER_JOB_TTL_SECONDS",
                issues=issues,
                minimum=60,
            ),
            job_history_max=_int(
                get("B2ROUTER_JOB_HISTORY_MAX", "500"),
                name="B2ROUTER_JOB_HISTORY_MAX",
                issues=issues,
                minimum=10,
            ),
            temp_dir=get("B2ROUTER_TEMP_DIR", ""),
            lock_file=get("B2ROUTER_LOCK_FILE", ""),
            acquire_lock=_bool(
                get("B2ROUTER_ACQUIRE_LOCK", "1"),
                name="B2ROUTER_ACQUIRE_LOCK",
                issues=issues,
            ),
            api_token=env.get("B2ROUTER_API_TOKEN") or None,
            secret_key=env.get("B2ROUTER_SECRET_KEY") or None,
            cookie_secure=_bool(
                get("B2ROUTER_COOKIE_SECURE", "0"),
                name="B2ROUTER_COOKIE_SECURE",
                issues=issues,
            ),
            trust_proxy=_bool(
                get("B2ROUTER_TRUST_PROXY", "0"), name="B2ROUTER_TRUST_PROXY", issues=issues
            ),
            mutation_rate_per_minute=_int(
                get("B2ROUTER_MUTATION_RATE_PER_MINUTE", "240"),
                name="B2ROUTER_MUTATION_RATE_PER_MINUTE",
                issues=issues,
                minimum=1,
            ),
            cors_origins=_csv(get("B2ROUTER_CORS_ORIGINS", "")),
            log_format=log_format,
            log_level=get("B2ROUTER_LOG_LEVEL", "INFO").upper(),
            metrics_enabled=_bool(
                get("B2ROUTER_METRICS", "1"), name="B2ROUTER_METRICS", issues=issues
            ),
        )

        if settings.port > 65535:
            issues.append("B2ROUTER_PORT must be <= 65535")
        if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            issues.append("B2ROUTER_LOG_LEVEL must be a standard logging level")

        if issues:
            raise SettingsError(issues)
        return settings
