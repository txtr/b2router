#!/usr/bin/env python3
"""Empty all B2 buckets for testing - USE WITH CAUTION!"""

import logging
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from b2router.config import load_config
from b2router.b2_client import get_b2_client, build_account_state

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def empty_all_buckets(accounts_yaml: str = "accounts.yaml") -> int:
    """Delete all files from all buckets in all accounts."""
    accounts = load_config(accounts_yaml)
    
    # Discover buckets
    build_account_state(accounts, parallel=True)
    
    total_deleted = 0
    for name, account in accounts.items():
        logger.info(f"Processing account: {name}")
        client = get_b2_client(account)
        
        for bucket in account.buckets:
            logger.info(f"  Emptying bucket: {bucket.name} ({bucket.id_})")
            b2_bucket = client.get_bucket_by_id(bucket.id_)
            
            # List and delete all files
            deleted_count = 0
            for file_version, _ in b2_bucket.ls(recursive=True, latest_only=True):
                try:
                    b2_bucket.delete_file_version(file_version.id_, file_version.file_name)
                    deleted_count += 1
                    if deleted_count % 100 == 0:
                        logger.info(f"    Deleted {deleted_count} files...")
                except Exception as e:
                    logger.error(f"    Failed to delete {file_version.file_name}: {e}")
            
            logger.info(f"  Deleted {deleted_count} files from {bucket.name}")
            total_deleted += deleted_count
    
    logger.info(f"Total files deleted: {total_deleted}")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Empty all B2 buckets")
    parser.add_argument("--accounts", default="accounts.yaml", help="Accounts YAML file")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    args = parser.parse_args()
    
    if not args.yes:
        print("⚠️  This will DELETE ALL FILES from ALL buckets in ALL accounts!")
        print("Type 'DELETE ALL' to confirm:")
        response = input("> ").strip()
        if response != "DELETE ALL":
            print("Cancelled.")
            sys.exit(1)
    
    sys.exit(empty_all_buckets(args.accounts))