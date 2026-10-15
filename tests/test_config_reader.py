#!/usr/bin/env python3
"""Test script to validate ConfigReader parsing and output structured results."""

import json
import sys
from pathlib import Path

from b2router.core.config_reader import ConfigReader


def test_config_reader(config_file: str = "accounts.yaml") -> dict:
    """Test ConfigReader and return structured results for programmatic use."""
    reader = ConfigReader(config_file)
    accounts = reader.read()

    results = {
        "config_file": config_file,
        "total_accounts": len(accounts),
        "accounts": [],
        "errors": [],
        "warnings": [],
    }

    for acc in accounts:
        if not acc.account_id:
            results["errors"].append(f"{acc.name}: missing account_id")
        if not acc.account_key:
            results["errors"].append(f"{acc.name}: missing account_key")
        if acc.capacity_in_mb is not None and acc.capacity_in_mb <= 0:
            results["warnings"].append(f"{acc.name}: capacity_in_mb should be positive")

        account_data = {
            "name": acc.name,
            "account_id": acc.account_id,
            "account_key_masked": f"{acc.account_key[:8]}...{acc.account_key[-8:]}",
            "capacity_in_mb": acc.capacity_in_mb,
            "capacity_bytes": acc.capacity_bytes,
        }
        results["accounts"].append(account_data)

    return results


def print_results(results: dict) -> None:
    """Print formatted results to stdout."""
    print(f"Config file: {results['config_file']}")
    print(f"Total accounts: {results['total_accounts']}")
    print("=" * 60)

    for acc in results["accounts"]:
        print(f"\n--- {acc['name']} ---")
        print(f"  account_id: {acc['account_id']}")
        print(f"  account_key: {acc['account_key_masked']}")
        print(f"  capacity_in_mb: {acc['capacity_in_mb']}")
        print(f"  capacity_bytes: {acc['capacity_bytes']}")

    if results["warnings"]:
        print("\n⚠️  Warnings:")
        for w in results["warnings"]:
            print(f"  - {w}")

    if results["errors"]:
        print("\n❌ Errors:")
        for e in results["errors"]:
            print(f"  - {e}")

    print("\n" + "=" * 60)
    status = "PASS" if not results["errors"] else "FAIL"
    print(f"Validation: {status}")


def main(config_file: str = "accounts.yaml", output_json: bool = False) -> int:
    """Run test and return exit code (0=success, 1=validation errors)."""
    path = Path(config_file)
    if not path.exists():
        print(f"Error: Config file not found: {config_file}")
        return 1

    results = test_config_reader(config_file)

    if output_json:
        print(json.dumps(results, indent=2))
    else:
        print_results(results)

    return 0 if not results["errors"] else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test ConfigReader parsing")
    parser.add_argument("config_file", nargs="?", default="accounts.yaml", help="Path to accounts.yaml")
    parser.add_argument("--json", action="store_true", help="Output JSON for programmatic use")
    args = parser.parse_args()

    sys.exit(main(args.config_file, args.json))