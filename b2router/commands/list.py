"""List command for B2 Router."""

from ..models import Account
from ..b2_client import build_account_state
from ..utils import format_bytes
from ..allocation import CAP_SAFETY


def list_all(accounts: dict[str, Account], parallel: bool = False) -> None:
    """Print details of all accounts, buckets, and files."""
    # Show progress while fetching
    print("🔍  Fetching account state from B2...")
    build_account_state(accounts, parallel=parallel)

    print("\n" + "=" * 70)
    print("                         B2 ROUTER - ACCOUNT OVERVIEW")
    print("=" * 70)

    total_accounts = len(accounts)
    total_buckets = 0
    total_files = 0
    total_used_gb = 0.0
    total_capacity_gb = 0.0

    for acc_name, account in accounts.items():
        print(f"\n📦  Account: {acc_name}")
        print(f"    ID: {account.account_id}")
        print(f"    Realm: {getattr(account, 'realm', 'production')}")

        account_used = sum(b.used_bytes for b in account.buckets if not b._populate_failed)
        account_capacity = int(account.capacity_in_gb * (1024 ** 3) * CAP_SAFETY)
        account_used_gb = account_used / (1024 ** 3)
        account_capacity_gb = account_capacity / (1024 ** 3)
        pct = (account_used_gb / account_capacity_gb * 100) if account_capacity_gb > 0 else 0

        total_used_gb += account_used_gb
        total_capacity_gb += account_capacity_gb

        # Progress bar for account usage
        bar_width = 30
        filled = int(bar_width * account_used_gb / account_capacity_gb) if account_capacity_gb > 0 else 0
        filled = max(0, min(filled, bar_width))  # Clamp
        bar = "█" * filled + "░" * (bar_width - filled)
        print(f"    Capacity: {account_capacity_gb:.2f} GB / {account.capacity_in_gb} GB (safety: {CAP_SAFETY*100:.0f}%)")
        print(f"    Used:     {account_used_gb:.2f} GB ({pct:.1f}%) [{bar}]")

        account_buckets = 0
        account_files = 0
        for bucket in account.buckets:
            status = " ⚠️  POPULATE FAILED" if bucket._populate_failed else ""
            print(f"\n    🪣  Bucket: {bucket.name}")
            print(f"        ID: {bucket.id_}{status}")

            bucket_used_gb = bucket.used_bytes / (1024 ** 3)
            # Bucket capacity is account-level (B2 has no per-bucket limit)
            bucket_cap_gb = bucket.capacity_bytes / (1024 ** 3)
            bucket_pct = (bucket_used_gb / bucket_cap_gb * 100) if bucket_cap_gb > 0 else 0

            filled = int(20 * bucket_used_gb / bucket_cap_gb) if bucket_cap_gb > 0 else 0
            filled = max(0, min(filled, 20))  # Clamp
            bar = "█" * filled + "░" * (20 - filled)
            print(f"        Capacity: {bucket_cap_gb:.2f} GB (account limit)")
            print(f"        Used:     {bucket_used_gb:.2f} GB ({bucket_pct:.1f}%) [{bar}]")

            if not bucket.files:
                print(f"        Files:    (empty)")
            else:
                print(f"        Files:    {len(bucket.files)}")
                account_files += len(bucket.files)
                total_files += len(bucket.files)
                # Show first 5 files, then truncate
                for i, f in enumerate(bucket.files[:5]):
                    size_str = format_bytes(f.size)
                    print(f"          • {f.file_name} ({size_str})")
                if len(bucket.files) > 5:
                    print(f"          … and {len(bucket.files) - 5} more files")
            account_buckets += 1
            total_buckets += 1

        print(f"\n    📊  Account summary: {account_buckets} buckets, {account_files} files")

    # Global summary
    print("\n" + "=" * 70)
    print("                              GLOBAL SUMMARY")
    print("=" * 70)
    total_pct = (total_used_gb / total_capacity_gb * 100) if total_capacity_gb > 0 else 0
    print(f"  Accounts:   {total_accounts}")
    print(f"  Buckets:    {total_buckets}")
    print(f"  Files:      {total_files}")
    print(f"  Used:       {total_used_gb:.2f} GB / {total_capacity_gb:.2f} GB ({total_pct:.1f}%)")