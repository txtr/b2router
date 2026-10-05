# B2 Router

[![CI](https://github.com/txtr/b2router/actions/workflows/ci.yml/badge.svg)](https://github.com/txtr/b2router/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/code%20style-ruff-black.svg)](https://github.com/astral-sh/ruff)

Lean, fast CLI for routing files from a local directory to Backblaze B2 buckets across multiple accounts. Designed for Colab and simple automation.

## Features

- **Multi-account management** - Manage multiple B2 accounts with individual capacity limits
- **Greedy best-fit allocation** - Intelligently distributes files across buckets while respecting account-level capacity (minimal free space)
- **Single-threaded, sequential uploads** - Simple, predictable, no parallel complexity
- **Copy & Move commands** - Copy (keep local) or Move (delete local after upload)
- **Auto-discovers buckets** - No bucket_name in config; uses first bucket per account via B2 SDK
- **Fail-fast on errors** - No retry backoff, no partial failures
- **Type safety** - Full mypy type checking
- **Zero dependencies beyond b2sdk + pyyaml** - ~600 lines total

## Quick Start

```bash
# Clone and setup
git clone https://github.com/txtr/b2router.git
cd b2router
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure (copy template and add your B2 credentials)
cp accounts.yaml.example accounts.yaml
# Edit accounts.yaml with your B2 Application Keys

# List all buckets and files
python b2.py --accounts=accounts.yaml list

# Copy files to B2 (keeps local files)
python b2.py --accounts=accounts.yaml copy /path/to/data

# Move files to B2 (deletes local files after upload)
python b2.py --accounts=accounts.yaml move /path/to/data
```

## Configuration

Create `accounts.yaml` with your B2 credentials:

```yaml
accounts:
  my-account:
    account_id: "your_application_key_id"
    master_key: "your_application_key"
    capacity_gb: 10  # optional, default 10
```

**Note**: No `bucket_name` needed - the tool auto-discovers the first bucket in each account.

See [accounts.yaml.example](accounts.yaml.example) for the full template.

## Commands

| Command | Description |
|---------|-------------|
| `list`  | Query and display all accounts/buckets/files |
| `move`  | Move files from source directory to B2 (deletes local) |
| `copy`  | Copy files from source directory to B2 (keeps local) |

### Global Options
- `--accounts PATH` - Config file (default: `accounts.yaml`)

### Move/Copy Options
- `source` - Local directory path (required positional argument)

## Allocation Algorithm

Uses **greedy best-fit decreasing**:
1. Sort files by size descending (largest first)
2. For each file, place in the bucket with **least remaining space** that can fit it
3. If no bucket fits, skip file (with warning)

This minimizes fragmentation and free space across accounts.

## Docker

```bash
# Build image
docker build -t b2router .

# Run (mount config and source directory)
docker run --rm -v ./accounts.yaml:/app/accounts.yaml -v /data:/data \
  b2router copy /data
```

## Security

- **Never commit credentials** - `accounts.yaml` is gitignored
- Use B2 **application keys** with minimal required capabilities
- See [SECURITY.md](SECURITY.md) for vulnerability reporting

## License

[MIT License](LICENSE) - Copyright (c) 2024 txtr