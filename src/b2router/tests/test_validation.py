"""Spooling safety: permissions, free-space admission, and size enforcement."""

from __future__ import annotations

import io
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.datastructures import FileStorage

from b2router.api.validation import spool_upload
from b2router.errors import Problem
from b2router.settings import Settings


def _part(data: bytes, name: str = "payload.bin") -> FileStorage:
    return FileStorage(stream=io.BytesIO(data), filename=name)


def test_spool_creates_private_directory_and_file(settings: Settings) -> None:
    spooled = spool_upload(
        _part(b"abc"),
        file_name="safe.bin",
        settings_max_bytes=settings.max_upload_bytes,
        temp_dir=settings.temp_dir,
    )
    try:
        directory = stat.S_IMODE(Path(settings.temp_dir).stat().st_mode)
        mode = stat.S_IMODE(Path(spooled.path).stat().st_mode)
        assert directory == 0o700
        assert mode == 0o600
        assert spooled.size == 3
    finally:
        Path(spooled.path).unlink(missing_ok=True)


def test_spool_rejects_low_disk_before_creating_a_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "b2router.api.validation.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )

    with pytest.raises(Problem) as excinfo:
        spool_upload(
            _part(b"abc"),
            file_name="safe.bin",
            settings_max_bytes=settings.max_upload_bytes,
            temp_dir=settings.temp_dir,
        )

    assert excinfo.value.code == "service_unavailable"
    assert excinfo.value.status == 503


def test_spool_converts_staging_oserror_to_service_unavailable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("b2router.api.validation.tempfile.mkstemp", fail)

    with pytest.raises(Problem) as excinfo:
        spool_upload(
            _part(b"abc"),
            file_name="safe.bin",
            settings_max_bytes=settings.max_upload_bytes,
            temp_dir=settings.temp_dir,
        )

    assert excinfo.value.code == "service_unavailable"
    assert excinfo.value.status == 503


def test_spool_enforces_per_file_limit(settings: Settings) -> None:
    with pytest.raises(Problem) as excinfo:
        spool_upload(
            _part(b"too large"),
            file_name="big.bin",
            settings_max_bytes=4,
            temp_dir=settings.temp_dir,
        )

    assert excinfo.value.code == "payload_too_large"
