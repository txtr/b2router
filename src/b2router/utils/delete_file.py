from __future__ import annotations

import argparse
import sys

from b2router.core.config_reader import ConfigReader
from b2router.services.account import Account


def delete_file(account_name: str, file_name: str) -> None:
    """Delete a file from the given B2 account."""
    reader = ConfigReader("accounts.yaml")
    accounts = reader.read()

    acc = next((a for a in accounts if a.name == account_name), None)
    if not acc:
        print(f"Account '{account_name}' not found. Available: {[a.name for a in accounts]}")
        sys.exit(1)

    account = Account(application_key_id=acc.account_id, application_key=acc.account_key)

    # Get file info first to get file_id
    file_info = account.bucket.get_file_info_by_name(file_name)
    if not file_info:
        print(f"File '{file_name}' not found in account '{account_name}'")
        sys.exit(1)

    print(f"Deleting {file_name} from account '{account_name}'...")
    account.bucket.delete_file_version(file_info.id_, file_name)
    print(f"Deleted successfully!")


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete a file from a B2 account")
    parser.add_argument("account", help="Account name (from accounts.yaml)")
    parser.add_argument("filename", help="Name of file to delete from B2")
    args = parser.parse_args()

    delete_file(args.account, args.filename)


if __name__ == "__main__":
    main()