#!/usr/bin/env python3
"""Test script to initialize all accounts from accounts.yaml and print their cache."""

from b2router.core.config_reader import ConfigReader
from b2router.services import account_pool


def main() -> None:
    print("Loading accounts from accounts.yaml...")
    reader = ConfigReader("accounts.yaml")
    accounts = reader.read()

    print(f"Found {len(accounts)} account(s)\n")

    for acc in accounts:
        print(f"Initializing {acc.name}...")
        account_pool.add(acc.name, acc.account_id, acc.account_key)
        if acc.capacity_in_mb:
            account = account_pool[acc.name]
            account.cache["capacity_mb"] = acc.capacity_in_mb
            account.cache["capacity_bytes"] = acc.capacity_bytes

    print("\n" + "=" * 60)
    print("ACCOUNT CACHES")
    print("=" * 60)

    for name in account_pool.names():
        account = account_pool[name]
        print(f"\n--- {name} ---")
        for key, value in account.cache.items():
            if key == "files":
                print(f"  {key}: {len(value)} file(s)")
                for f in value[:5]:  # Show first 5 files
                    print(f"    - {f['file_name']} ({f['size']} bytes)")
                if len(value) > 5:
                    print(f"    ... and {len(value) - 5} more")
            else:
                print(f"  {key}: {value}")

    print("\n" + "=" * 60)
    print(f"Total accounts in pool: {len(account_pool)}")
    print("=" * 60)


if __name__ == "__main__":
    main()