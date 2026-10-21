"""Find and optionally remove duplicate/orphaned B2 versions.

The gateway's startup cache is built from the latest version of every file.
An upsert whose old-version delete failed leaves the new version in place and
the old bytes behind. Within one account that is invisible to a latest-only
listing; across accounts startup refuses to run. This tool reports both cases
and can reconcile them: keep the newest upload for each name, delete the rest.

Always stop the gateway before using ``--apply``. The script takes the same
single-writer lock as the server so it cannot race a live instance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass

from b2router.core.config_reader import ConfigReader
from b2router.lockfile import InstanceLock, LockError
from b2router.services.account import AccountClient, AccountCredentials, StorageError
from b2router.services.pool import Err
from b2router.settings import Settings


@dataclass(frozen=True)
class VersionRef:
    account: str
    file_name: str
    file_id: str
    size: int
    upload_timestamp: int
    content_type: str

    def to_dict(self) -> dict[str, object]:
        return {
            "account": self.account,
            "file_id": self.file_id,
            "size": self.size,
            "upload_timestamp": self.upload_timestamp,
            "content_type": self.content_type,
        }


def _build_clients(settings: Settings) -> dict[str, AccountClient]:
    result = ConfigReader(settings.accounts_path).read()
    if isinstance(result, Err):
        raise SystemExit(result.error.message)
    clients: dict[str, AccountClient] = {}
    for spec in result.value.accounts:
        clients[spec.name] = AccountClient(
            AccountCredentials(
                name=spec.name,
                application_key_id=spec.application_key_id,
                application_key=spec.application_key,
                connect_timeout=settings.connect_timeout,
                read_timeout=settings.read_timeout,
            )
        )
    return clients


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report or remove duplicate B2 file versions")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete every version except the newest upload for each file name",
    )
    parser.add_argument(
        "--accounts",
        default=os.environ.get("B2ROUTER_ACCOUNTS", "accounts.yaml"),
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    settings = Settings(
        **{
            **settings.__dict__,
            "accounts_path": args.accounts,
        }
    )

    lock = InstanceLock(settings.lock_file)
    if args.apply:
        try:
            lock.acquire()
        except (LockError, OSError) as exc:
            print(
                json.dumps({"error": f"Cannot apply while the gateway is running: {exc}"}),
                file=sys.stderr,
            )
            return 3

    try:
        clients = _build_clients(settings)
        by_name: dict[str, list[VersionRef]] = defaultdict(list)
        account_errors: dict[str, str] = {}
        for name, client in clients.items():
            try:
                for version in client.list_versions():
                    by_name[version.file_name].append(
                        VersionRef(
                            account=name,
                            file_name=version.file_name,
                            file_id=version.file_id,
                            size=version.size,
                            upload_timestamp=version.upload_timestamp,
                            content_type=version.content_type,
                        )
                    )
            except StorageError as exc:
                account_errors[name] = exc.message

        groups: list[dict[str, object]] = []
        deleted: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        for file_name in sorted(by_name):
            versions = by_name[file_name]
            if len(versions) < 2:
                continue
            ordered = sorted(
                versions,
                key=lambda version: (version.upload_timestamp, version.account),
                reverse=True,
            )
            keeper = ordered[0]
            stale = ordered[1:]
            groups.append(
                {
                    "file_name": file_name,
                    "keeper": keeper.to_dict(),
                    "stale": [version.to_dict() for version in stale],
                }
            )
            if not args.apply:
                continue
            for stale_ref in stale:
                client = clients[stale_ref.account]
                try:
                    client.delete_version(stale_ref.file_id, stale_ref.file_name)
                    deleted.append(
                        {
                            "file_name": stale_ref.file_name,
                            "account": stale_ref.account,
                            "file_id": stale_ref.file_id,
                        }
                    )
                except StorageError as exc:
                    failed.append(
                        {
                            "file_name": stale_ref.file_name,
                            "account": stale_ref.account,
                            "file_id": stale_ref.file_id,
                            "error": exc.message,
                            "code": exc.code,
                        }
                    )

        report = {
            "ok": not failed and not account_errors,
            "applied": args.apply,
            "duplicate_files": len(groups),
            "groups": groups,
            "deleted": deleted,
            "failed": failed,
            "account_errors": account_errors,
        }
        print(json.dumps(report, indent=2 if args.pretty else None))
        return 0 if report["ok"] else 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
