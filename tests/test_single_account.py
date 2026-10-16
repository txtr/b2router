#!/usr/bin/env python3
"""Test script to initialize a single Account from config and print its files."""

import sys
from b2router.core.config_reader import ConfigReader
from b2router.services.account import Account


def main(account_name: str = "account-1") -> None:
    reader = ConfigReader("accounts.yaml")
    config = reader.read()

    acc = next((a for a in config.accounts if a.name == account_name), None)
    if not acc:
        print(f"Account '{account_name}' not found in config")
        print(f"Available: {[a.name for a in config.accounts]}")
        sys.exit(1)

    print(f"Initializing Account({acc.name})...")
    account = Account(application_key_id=acc.account_id, application_key=acc.account_key)

    files = account.list_files()

    print("\n" + "=" * 60)
    print("ACCOUNT FILES")
    print("=" * 60)
    print(f"Bucket: {account.bucket_name}")
    print(f"Files: {len(files)}")
    for f in files:
        print(f"  - {f['file_name']} ({f['size']} bytes)")
    print("=" * 60)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "account-1"
    main(name)