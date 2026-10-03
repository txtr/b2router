# B2 Router

[![CI](https://github.com/txtr/b2router/actions/workflows/ci.yml/badge.svg)](https://github.com/txtr/b2router/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Code Style](https://img.shields.io/badge/code%20style-ruff-black.svg)](https://github.com/astral-sh/ruff)

Route files from a local directory to Backblaze B2 buckets across multiple accounts with greedy best-fit allocation.

## Features

- **Multi-account management** - Manage 100+ B2 accounts with individual capacity limits
- **Greedy best-fit allocation** - Intelligently distributes files across buckets while respecting account-level capacity
- **Streaming uploads** - Handles large files (GB+) without memory issues
- **Atomic operations** - Upload then delete; fail-fast on any error
- **Resume capability** - Persisted state enables recovery from interruptions
- **Parallel uploads** - 1-10 concurrent workers with progress tracking
- **Progress bar** - Real-time ETA, transfer rate, and elapsed time
- **Retry with backoff** - Exponential backoff + jitter for transient B2 errors
- **Symlink safety** - Never follows symlinks; prevents directory traversal
- **Comprehensive tests** - 18 test cases covering edge cases
- **Copy command** - Upload without deleting source files
- **Config validation** - Duplicate account_id detection
- **B2 realm support** - Production and test environments
- **Type safety** - Full mypy type checking

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
python b2.py --accounts=accounts.yaml list --parallel

# Dry-run move (preview allocation)
python b2.py --accounts=accounts.yaml move /path/to/data --dry-run

# Execute move
python b2.py --accounts=accounts.yaml move /path/to/data --yes

# Dry-run copy (preview allocation)
python b2.py --accounts=accounts.yaml copy /path/to/data --dry-run

# Execute copy (no deletion of local files)
python b2.py --accounts=accounts.yaml copy /path/to/data --yes

# Resume interrupted move
python b2.py --accounts=accounts.yaml move /path/to/data --resume --yes

# Resume interrupted copy
python b2.py --accounts=accounts.yaml copy /path/to/data --resume --yes
```

## Configuration

Create `accounts.yaml` with your B2 credentials:

```yaml
accounts:
  my-account:
    account_id: "your_application_key_id"
    master_key: "your_application_key"
    capacity_in_gb: 10
```

See [accounts.yaml.example](accounts.yaml.example) for the full template.

## Commands

| Command | Description |
|---------|-------------|
| `list` | Query and display all accounts/buckets/files |
| `move` | Move files from source directory to B2 |
| `copy` | Copy files from source directory to B2 (no deletion) |

### Global Options
- `--accounts PATH` - Config file (default: `accounts.yaml`)
- `-v, --verbose` - Debug output
- `-q, --quiet` - Errors only (disables progress bar)
- `--version` - Show version and exit
- `--realm {production,test}` - B2 realm (default: production)

### Move/Copy Options
- `--dry-run` - Preview allocation without uploading
- `--yes` - Skip confirmation prompt
- `--parallel-uploads N` - Concurrent uploads (1-10, default: 1)
- `--resume` - Resume from previous interrupted operation

## Documentation

- [Architecture Overview](docs/ARCHITECTURE.md) - Detailed system design
- [Contributing Guide](CONTRIBUTING.md) - How to contribute
- [Security Policy](SECURITY.md) - Vulnerability reporting
- [Code of Conduct](CODE_OF_CONDUCT.md) - Community standards

## Testing

```bash
# Run all tests
python test_bugs.py

# Run with verbose output
python test_bugs.py -v

# Lint code
ruff check b2.py
```

## Docker

```bash
# Build image
docker build -t b2router .

# Run (mount config and source directory)
docker run --rm -v ./accounts.yaml:/app/accounts.yaml -v /data:/data \
  b2router move /data --yes
```

## Security

- **Never commit credentials** - `accounts.yaml` is gitignored
- Use B2 **application keys** with minimal required capabilities
- See [SECURITY.md](SECURITY.md) for vulnerability reporting

## License

[MIT License](LICENSE) - Copyright (c) 2024 txtr