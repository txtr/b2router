"""accounts.yaml validation."""

from __future__ import annotations

from pathlib import Path

from b2router.core.config_reader import ConfigReader
from b2router.services.pool import Err, Ok


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "accounts.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_config(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
max_files_per_account: 42
accounts:
  - account_id: aaaa1111bbbb
    account_key: secret-one
    capacity_in_mb: 10
  - account_id: cccc2222dddd
    account_key: secret-two
""",
    )
    result = ConfigReader(path).read()
    assert isinstance(result, Ok)
    config = result.value
    assert config.max_files_per_account == 42
    assert [account.name for account in config.accounts] == [
        "account-aaaa1111",
        "account-cccc2222",
    ]
    assert config.accounts[0].capacity_bytes == 10 * 1024 * 1024
    assert config.accounts[1].capacity_bytes is None


def test_missing_file(tmp_path: Path) -> None:
    result = ConfigReader(tmp_path / "nope.yaml").read()
    assert isinstance(result, Err)
    assert result.error.kind == "file_not_found"


def test_empty_file(tmp_path: Path) -> None:
    result = ConfigReader(_write(tmp_path, "")).read()
    assert isinstance(result, Err)
    assert result.error.kind == "empty_yaml"


def test_root_must_be_mapping(tmp_path: Path) -> None:
    result = ConfigReader(_write(tmp_path, "- one\n- two\n")).read()
    assert isinstance(result, Err)
    assert result.error.kind == "invalid_yaml"


def test_missing_accounts(tmp_path: Path) -> None:
    result = ConfigReader(_write(tmp_path, "max_files_per_account: 10\n")).read()
    assert isinstance(result, Err)
    assert result.error.kind == "missing_accounts"


def test_duplicate_account_id(tmp_path: Path) -> None:
    result = ConfigReader(
        _write(
            tmp_path,
            """
accounts:
  - account_id: same-id
    account_key: one
  - account_id: same-id
    account_key: two
""",
        )
    ).read()
    assert isinstance(result, Err)
    assert result.error.kind == "duplicate_account"


def test_missing_credentials_do_not_echo_secrets(tmp_path: Path) -> None:
    result = ConfigReader(
        _write(
            tmp_path,
            """
accounts:
  - account_id: aaaa1111
    account_key: super-secret-value
  - account_id: bbbb2222
""",
        )
    ).read()
    assert isinstance(result, Err)
    assert "super-secret-value" not in result.error.message


def test_invalid_capacity(tmp_path: Path) -> None:
    result = ConfigReader(
        _write(
            tmp_path,
            """
accounts:
  - account_id: aaaa1111
    account_key: value
    capacity_in_mb: -1
""",
        )
    ).read()
    assert isinstance(result, Err)
    assert result.error.kind == "invalid_capacity"


def test_invalid_max_files(tmp_path: Path) -> None:
    result = ConfigReader(
        _write(
            tmp_path,
            """
max_files_per_account: 0
accounts:
  - account_id: aaaa1111
    account_key: value
""",
        )
    ).read()
    assert isinstance(result, Err)
    assert result.error.kind == "invalid_max_files"
