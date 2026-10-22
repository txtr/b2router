"""Mutation CLIs must respect the same single-writer lock as the server."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from b2router.lockfile import InstanceLock
from b2router.utils import create_file, delete_file, reconcile

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


def test_reconcile_apply_refuses_when_account_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apply refuses to delete when at least one account had listing errors."""
    # Use a separate lock file for this test so we don't hold it
    lock_path = tmp_path / "reconcile-test.lock"
    monkeypatch.setenv("B2ROUTER_LOCK_FILE", str(lock_path))
    monkeypatch.setenv("B2ROUTER_TEMP_DIR", str(tmp_path / "tmp"))

    # Create accounts.yaml with two accounts that will fail to list
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        """
accounts:
  - account_id: 0011aabb22ccdd33
    account_key: K001test-application-key
  - account_id: 0044eeff55aa66bb
    account_key: K004bad-key
""",
        encoding="utf-8",
    )

    # Don't hold the lock - let reconcile acquire it
    status = reconcile.main(
        ["--accounts", str(accounts), "--apply", "--pretty"]
    )

    # Should refuse to apply (exit 1) because account_errors is non-empty
    assert status == 1
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["apply_refused"] is True
    assert "Refusing to delete" in report.get("message", "")
    assert report["account_errors"]  # non-empty
