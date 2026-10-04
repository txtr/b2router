#!/usr/bin/env python3
"""B2 Router – Route files from a local directory to Backblaze B2 buckets.

Commands:
    b2router --accounts=accounts.yaml list [--parallel]
    b2router --accounts=accounts.yaml move /source [--dry-run] [--yes]
    b2router --accounts=accounts.yaml copy /source [--dry-run] [--yes]

Options:
    --dry-run  Show allocation plan without executing uploads
    --yes      Skip the confirmation prompt and proceed directly with upload
"""

from b2router.cli import main

if __name__ == "__main__":
    main()