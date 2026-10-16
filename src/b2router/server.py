#!/usr/bin/env python3
"""Run the Flask web server with accounts loaded from config."""

from b2router.core.config_reader import ConfigReader
from b2router.services import account_pool
from b2router.web import run_server


def load_accounts() -> None:
    """Load accounts from accounts.yaml into the pool."""
    reader = ConfigReader("accounts.yaml")
    config = reader.read()

    for acc in config.accounts:
        account_pool.add(acc.name, acc.account_id, acc.account_key, acc.capacity_in_mb or 0)

    # Set max files per account from config
    account_pool.set_max_files_per_account(config.max_files_per_account)

    print(f"Loaded {len(account_pool)} accounts into pool")


def main(host: str = "0.0.0.0", port: int = 8000) -> None:
    load_accounts()
    print(f"Starting server on {host}:{port}")
    run_server(host=host, port=port)


if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    main(host, port)