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
        # Capacity info stored in account config, not in cache anymore

    print("\n" + "=" * 60)
    print("ACCOUNT POOL FILE CACHE")
    print("=" * 60)

    # Show file cache stats
    stats = account_pool.file_cache_stats()
    print(f"\nFile Cache Stats: {stats}")

    # Show files per account
    for name in account_pool.names():
        files = account_pool.list_files_for_account(name)
        print(f"\n--- {name} ---")
        print(f"  Bucket: {account_pool[name].bucket_name}")
        print(f"  Files: {len(files)}")
        for f in files[:5]:
            print(f"    - {f['file_name']} ({f['size']} bytes)")
        if len(files) > 5:
            print(f"    ... and {len(files) - 5} more")

    print("\n" + "=" * 60)
    print(f"Total accounts in pool: {len(account_pool)}")
    print("=" * 60)


if __name__ == "__main__":
    main()