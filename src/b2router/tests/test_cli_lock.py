"""Mutation CLIs must respect the same single-writer lock as the server."""

from __future__ import annotations

from pathlib import Path

import pytest

from b2router.lockfile import InstanceLock
from b2router.utils import create_file, delete_file

ACCOUNTS = """
accounts:
  - account_id: 0011aabb22ccdd33
    account_key: K001test-application-key
"""


def _accounts_file(tmp_path: Path) -> Path:
    path = tmp_path / "accounts.yaml"
    path.write_text(ACCOUNTS, encoding="utf-8")
    return path


def test_mutation_clis_refuse_to_run_while_gateway_holds_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "instance.lock"
    monkeypatch.setenv("B2ROUTER_LOCK_FILE", str(lock_path))
    monkeypatch.setenv("B2ROUTER_TEMP_DIR", str(tmp_path / "tmp"))
    accounts = _accounts_file(tmp_path)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")

    lock = InstanceLock(str(lock_path))
    lock.acquire()
    try:
        create_status = create_file.main(
            [
                "account-0011aabb",
                "remote.bin",
                "--file",
                str(source),
                "--accounts",
                str(accounts),
            ]
        )
        delete_status = delete_file.main(
            [
                "account-0011aabb",
                "remote.bin",
                "--accounts",
                str(accounts),
            ]
        )
    finally:
        lock.release()

    assert create_status == 3
    assert delete_status == 3
