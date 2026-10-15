#!/usr/bin/env python3
"""Test script to initialize a single Account from config and print its cache."""

import sys
from b2router.core.config_reader import ConfigReader
from b2router.services.account import Account


def main(account_name: str = "account-1") -> None:
    reader = ConfigReader("accounts.yaml")
    accounts = reader.read()

    acc = next((a for a in accounts if a.name == account_name), None)
    if not acc:
        print(f"Account '{account_name}' not found in config")
        print(f"Available: {[a.name for a in accounts]}")
        sys.exit(1)

    print(f"Initializing Account({acc.name})...")
    account = Account(application_key_id=acc.account_id, application_key=acc.account_key)

    if acc.capacity_in_mb:
        account.cache["capacity_mb"] = acc.capacity_in_mb
        account.cache["capacity_bytes"] = acc.capacity_bytes

    print("\n" + "=" * 60)
    print("ACCOUNT CACHE")
    print("=" * 60)

    for key, value in account.cache.items():
        if key == "files":
            print(f"  {key}: {len(value)} file(s)")
            for f in value:
                print(f"    - {f['file_name']} ({f['size']} bytes)")
        else:
            print(f"  {key}: {value}")

    print("=" * 60)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "account-1"
    main(name)