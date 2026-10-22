"""Environment-driven settings."""

from __future__ import annotations

import pytest

from b2router.settings import Settings, SettingsError


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.max_upload_bytes == 10 * 1024 * 1024 * 1024
    assert settings.temp_dir.endswith("b2router")
    assert settings.lock_file == f"{settings.temp_dir}/instance.lock"
    assert settings.cors_origins == ()


def test_parses_all_supported_values() -> None:
    settings = Settings.from_env(
        {
            "B2ROUTER_HOST": "0.0.0.0",
            "B2ROUTER_PORT": "9000",
            "B2ROUTER_ACCOUNTS": "/etc/b2router/accounts.yaml",
            "B2_CONNECT_TIMEOUT": "5.5",
            "B2_READ_TIMEOUT": "30",
            "B2ROUTER_MAX_UPLOAD_BYTES": "1234",
            "B2ROUTER_MAX_FILES_PER_REQUEST": "3",
            "B2ROUTER_MAX_FILENAME_BYTES": "100",
            "B2ROUTER_UPLOAD_WORKERS": "4",
            "B2ROUTER_QUEUE_MAX": "9",
            "B2ROUTER_JOB_TTL_SECONDS": "120",
            "B2ROUTER_JOB_HISTORY_MAX": "50",
            "B2ROUTER_TEMP_DIR": "/tmp/b2",
            "B2ROUTER_LOCK_FILE": "/tmp/b2.lock",
            "B2ROUTER_ACQUIRE_LOCK": "false",
            "B2ROUTER_API_TOKEN": "token",
            "B2ROUTER_SECRET_KEY": "session-secret",
            "B2ROUTER_COOKIE_SECURE": "1",
            "B2ROUTER_TRUST_PROXY": "yes",
            "B2ROUTER_MUTATION_RATE_PER_MINUTE": "10",
            "B2ROUTER_CORS_ORIGINS": "https://a.test, https://b.test",
            "B2ROUTER_LOG_FORMAT": "json",
            "B2ROUTER_LOG_LEVEL": "debug",
            "B2ROUTER_METRICS": "0",
        }
    )
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.acquire_lock is False
    assert settings.secret_key == "session-secret"
    assert settings.cookie_secure is True
    assert settings.trust_proxy is True
    assert settings.cors_origins == ("https://a.test", "https://b.test")
    assert settings.log_level == "DEBUG"
    assert settings.metrics_enabled is False


def test_invalid_values_are_aggregated() -> None:
    with pytest.raises(SettingsError) as excinfo:
        Settings.from_env(
            {
                "B2ROUTER_PORT": "not-a-port",
                "B2ROUTER_UPLOAD_WORKERS": "0",
                "B2ROUTER_ACQUIRE_LOCK": "maybe",
                "B2ROUTER_LOG_FORMAT": "xml",
                "B2ROUTER_LOG_LEVEL": "LOUD",
            }
        )
    assert len(excinfo.value.issues) >= 5


def test_port_bounds() -> None:
    with pytest.raises(SettingsError):
        Settings.from_env({"B2ROUTER_PORT": "70000"})
