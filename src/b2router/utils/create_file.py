"""Upload a test object directly to one B2 account.

The running gateway never mutates its cache behind its back, so a file created
here is only visible after the next restart. This is a debugging tool, not part
of the HTTP API.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

from b2router.core.config_reader import AccountSpec, ConfigReader
from b2router.lockfile import InstanceLock, LockError
from b2router.services.account import AccountClient, AccountCredentials, StorageError
from b2router.services.pool import Err
from b2router.settings import Settings


def _find_account(specs: list[AccountSpec], needle: str) -> AccountSpec | None:
    return next(
        (
            spec
            for spec in specs
            if spec.name == needle
            or spec.application_key_id == needle
            or spec.name.endswith(needle)
        ),
        None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload a file directly to one B2 account")
    parser.add_argument("account", help="Account name from accounts.yaml")
    parser.add_argument("name", help="File name to create in B2 (may contain '/')")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Upload this local file")
    source.add_argument("--size-mb", type=int, help="Generate a random file of this size")
    parser.add_argument("--content-type", default="application/octet-stream")
    parser.add_argument("--accounts", default=os.environ.get("B2ROUTER_ACCOUNTS", "accounts.yaml"))
    args = parser.parse_args(argv)

    result = ConfigReader(args.accounts).read()
    if isinstance(result, Err):
        print(json.dumps({"error": result.error.message}), file=sys.stderr)
        return 2
    spec = _find_account(result.value.accounts, args.account)
    if spec is None:
        print(
            json.dumps(
                {
                    "error": f"Unknown account {args.account!r}",
                    "available": [account.name for account in result.value.accounts],
                }
            ),
            file=sys.stderr,
        )
        return 2

    settings = Settings.from_env()
    lock = InstanceLock(settings.lock_file)
    try:
        lock.acquire()
    except (LockError, OSError) as exc:
        print(
            json.dumps({"error": f"Cannot mutate while the gateway is running: {exc}"}),
            file=sys.stderr,
        )
        return 3
    client = AccountClient(
        AccountCredentials(
            name=spec.name,
            application_key_id=spec.application_key_id,
            application_key=spec.application_key,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
        )
    )

    generated: str | None = None
    try:
        if args.file:
            local_path = str(Path(args.file))
            if not Path(local_path).is_file():
                print(json.dumps({"error": f"Not a file: {local_path}"}), file=sys.stderr)
                return 2
        else:
            if args.size_mb is None or args.size_mb < 0:
                print(json.dumps({"error": "--size-mb must be >= 0"}), file=sys.stderr)
                return 2
            with tempfile.NamedTemporaryFile(prefix="b2router-gen-", delete=False) as handle:
                generated = handle.name
                remaining = args.size_mb * 1024 * 1024
                while remaining > 0:
                    chunk = os.urandom(min(remaining, 1024 * 1024))
                    handle.write(chunk)
                    remaining -= len(chunk)
            local_path = generated

        version = client.upload_local_file(local_path, args.name, args.content_type)
        print(
            json.dumps(
                {
                    "ok": True,
                    "account": spec.name,
                    "file_name": version.file_name,
                    "file_id": version.file_id,
                    "size": version.size,
                    "content_type": version.content_type,
                    "uploaded_timestamp": version.upload_timestamp,
                }
            )
        )
        return 0
    except StorageError as exc:
        print(json.dumps({"error": exc.message, "code": exc.code}), file=sys.stderr)
        return 1
    finally:
        if generated:
            with contextlib.suppress(OSError):
                os.unlink(generated)
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
