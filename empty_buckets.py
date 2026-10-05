#!/usr/bin/env python3
"""Empty all buckets for test accounts - cleanup after E2E tests."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from b2router.config import load_config
from b2router.b2_client import authorize_account, list_files_in_bucket, delete_file


def empty_bucket(account):
    """Delete all files in the account's bucket."""
    api = authorize_account(account.account_id, account.master_key)
    buckets = list(api.list_buckets())
    if not buckets:
        print(f"  No bucket found for {account.name}")
        return 0

    bucket = buckets[0]
    print(f"  Emptying {account.name} ({bucket.name})...")

    deleted = 0
    for file_name, _ in list_files_in_bucket(api, bucket.id_):
        file_info = api.get_bucket_by_id(bucket.id_).get_file_info_by_name(file_name)
        if delete_file(api, bucket.id_, file_name, file_info.id_):
            deleted += 1
        else:
            print(f"    Failed to delete: {file_name}")

    print(f"    Deleted {deleted} files")
    return deleted


def main():
    config_path = "accounts_test.yaml"
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        sys.exit(1)

    accounts = load_config(config_path)
    print(f"Emptying buckets for {len(accounts)} accounts...\n")

    total = 0
    for acc in accounts:
        total += empty_bucket(acc)

    print(f"\nTotal files deleted: {total}")


if __name__ == "__main__":
    main()