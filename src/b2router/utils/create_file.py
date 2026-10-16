from __future__ import annotations

import argparse
import os
import sys

from b2router.core.config_reader import ConfigReader
from b2router.services.account import Account


def create_test_file(account_name: str, file_name: str, size_mb: int) -> None:
    """Create a test file of specified size in the given account (uploads directly to B2).

    Note: The pool's file cache is built at server startup and is NOT refreshed.
    This file will exist in B2 but won't appear in the web UI until server restart.
    """
    reader = ConfigReader("accounts.yaml")
    accounts = reader.read()

    acc = next((a for a in accounts if a.name == account_name), None)
    if not acc:
        print(f"Account '{account_name}' not found. Available: {[a.name for a in accounts]}")
        sys.exit(1)

    account = Account(application_key_id=acc.account_id, application_key=acc.account_key)

    size_bytes = size_mb * 1024 * 1024
    content = os.urandom(size_bytes)

    print(f"Uploading {file_name} ({size_mb} MB) to account '{account_name}'...")
    file_version = account.bucket.upload_bytes(
        content,
        file_name,
        content_type="application/octet-stream",
    )

    print(f"Uploaded successfully!")
    print(f"  File ID: {file_version.id_}")
    print(f"  Size: {file_version.size} bytes")
    print(f"  Content-Type: {file_version.content_type}")
    print(f"NOTE: File will appear in web UI after server restart (cache is static)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a test file in a B2 account")
    parser.add_argument("account", help="Account name (from accounts.yaml)")
    parser.add_argument("filename", help="Name/path of file to create in B2")
    parser.add_argument("size_mb", type=int, help="Size of file in MB")
    args = parser.parse_args()

    create_test_file(args.account, args.filename, args.size_mb)


if __name__ == "__main__":
    main()