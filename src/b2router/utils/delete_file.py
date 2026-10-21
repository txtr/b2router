"""Delete one object directly from a B2 account (debugging tool)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from b2router.core.config_reader import AccountSpec, ConfigReader
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
    parser = argparse.ArgumentParser(description="Delete a file directly from one B2 account")
    parser.add_argument("account", help="Account name from accounts.yaml")
    parser.add_argument("name", help="File name in B2")
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
    client = AccountClient(
        AccountCredentials(
            name=spec.name,
            application_key_id=spec.application_key_id,
            application_key=spec.application_key,
            connect_timeout=settings.connect_timeout,
            read_timeout=settings.read_timeout,
        )
    )
    try:
        info = client.get_file_info(args.name)
        client.delete_version(info.file_id, info.file_name)
        print(
            json.dumps(
                {
                    "ok": True,
                    "account": spec.name,
                    "file_name": info.file_name,
                    "file_id": info.file_id,
                    "size": info.size,
                }
            )
        )
        return 0
    except StorageError as exc:
        print(json.dumps({"error": exc.message, "code": exc.code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
