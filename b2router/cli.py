"""CLI entry point for B2 Router."""

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .commands.list import list_all
from .commands.move import run_move
from .commands.copy import run_copy


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure logging based on verbosity flags."""
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    # Use stderr for logs to not interfere with stdout output
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)]
    )

    # Reduce tqdm output noise
    logging.getLogger("tqdm").setLevel(logging.WARNING)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        description="B2 Router – manage and move files to Backblaze B2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--accounts", default="accounts.yaml",
                       help="Path to accounts YAML configuration")
    parser.add_argument("-v", "--verbose", action="store_true",
                       help="Enable verbose (debug) output")
    parser.add_argument("-q", "--quiet", action="store_true",
                       help="Suppress non-error output")
    parser.add_argument("--version", action="version",
                       version=f"%(prog)s {__version__}")
    parser.add_argument("--realm", default="production",
                       choices=["production", "test"],
                       help="B2 realm (default: production)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # List command
    list_parser = subparsers.add_parser("list", help="List all accounts, buckets, and files")
    list_parser.add_argument("--parallel", action="store_true",
                            help="Fetch accounts in parallel for faster results")

    # Move command
    move_parser = subparsers.add_parser("move", help="Move files from source directory to B2")
    move_parser.add_argument("source", help="Local directory containing files to move")
    move_parser.add_argument("--dry-run", action="store_true",
                            help="Show allocation plan without execution")
    move_parser.add_argument("--yes", action="store_true",
                            help="Skip the confirmation prompt and proceed directly with upload")
    move_parser.add_argument("--parallel-uploads", type=int, default=1, metavar="N",
                            help="Number of parallel uploads (default: 1, max: 10)")
    move_parser.add_argument("--resume", action="store_true",
                            help="Resume interrupted move from saved state file")
    move_parser.add_argument("--skip-existing", action="store_true",
                            help="Skip files that already exist in B2 with matching SHA-1")
    move_parser.add_argument("--check", action="store_true",
                            help="Verify files in B2 match local (no upload), exit with code 1 if mismatch")
    move_parser.add_argument("--verify-only", action="store_true",
                            help="Only verify SHA-1 of already uploaded files (no upload)")
    move_parser.add_argument("--simple-log", action="store_true",
                            help="Simple line-based logging per file (no progress bar, works in Colab)")

    # Copy command
    copy_parser = subparsers.add_parser("copy", help="Copy files from source directory to B2 (no deletion)")
    copy_parser.add_argument("source", help="Local directory containing files to copy")
    copy_parser.add_argument("--dry-run", action="store_true",
                            help="Show allocation plan without execution")
    copy_parser.add_argument("--yes", action="store_true",
                            help="Skip the confirmation prompt and proceed directly with upload")
    copy_parser.add_argument("--parallel-uploads", type=int, default=1, metavar="N",
                            help="Number of parallel uploads (default: 1, max: 10)")
    copy_parser.add_argument("--resume", action="store_true",
                            help="Resume interrupted copy from saved state file")
    copy_parser.add_argument("--skip-existing", action="store_true",
                            help="Skip files that already exist in B2 with matching SHA-1")
    copy_parser.add_argument("--check", action="store_true",
                            help="Verify files in B2 match local (no upload), exit with code 1 if mismatch")
    copy_parser.add_argument("--verify-only", action="store_true",
                            help="Only verify SHA-1 of already uploaded files (no upload)")
    copy_parser.add_argument("--simple-log", action="store_true",
                            help="Simple line-based logging per file (no progress bar, works in Colab)")

    return parser


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()

    # Validate parallel_uploads range
    if hasattr(args, "parallel_uploads") and not (1 <= args.parallel_uploads <= 10):
        logging.error("--parallel-uploads must be between 1 and 10")
        return 1

    setup_logging(verbose=args.verbose, quiet=args.quiet)

    try:
        accounts = load_config(args.accounts)
    except Exception as exc:
        logging.error(f"Failed to load configuration: {exc}")
        return 1

    # Store realm for use in get_b2_client
    for account in accounts.values():
        account.realm = args.realm

    if args.command == "list":
        list_all(accounts, parallel=args.parallel)
        return 0

    if args.command == "move":
        return run_move(args, accounts)

    if args.command == "copy":
        return run_copy(args, accounts)

    return 0


if __name__ == "__main__":
    sys.exit(main())