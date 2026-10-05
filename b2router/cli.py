"""CLI entry point for b2router_simple."""

import argparse
import logging
import sys

from .config import load_config
from .commands.list import run_list
from .commands.copy import run_copy
from .commands.move import run_move


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)]
    )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="b2router_simple – Copy/move files to Backblaze B2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--accounts", default="accounts.yaml",
                       help="Path to accounts YAML configuration")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # List command
    subparsers.add_parser("list", help="List all files in all buckets")

    # Copy command
    copy_parser = subparsers.add_parser("copy", help="Copy files to B2 (no deletion)")
    copy_parser.add_argument("source", help="Local directory to copy")

    # Move command
    move_parser = subparsers.add_parser("move", help="Move files to B2 (delete local after upload)")
    move_parser.add_argument("source", help="Local directory to move")

    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    setup_logging()

    try:
        accounts = load_config(args.accounts)
    except Exception as exc:
        logging.error(f"Failed to load configuration: {exc}")
        return 1

    if args.command == "list":
        return run_list(accounts)
    elif args.command == "copy":
        return run_copy(accounts, args.source)
    elif args.command == "move":
        return run_move(accounts, args.source)

    return 0


if __name__ == "__main__":
    sys.exit(main())