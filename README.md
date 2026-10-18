# B2 Router

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

A Flask service for Backblaze B2 account pool management with best-fit upload routing, centralized file cache, and a web UI.

## Tech Stack

- **Python 3.11+**
- **b2sdk 2.13.1** — Backblaze B2 SDK
- **Flask 3.x** — Web framework and routing
- **HTMX** — Vendored in `static/` for the interactive UI (HTML-over-HTML, no SPA)
- **pyyaml 6.x** — Configuration parsing
- **structlog** — Structured logging (dependency)
- **ruff / mypy** — Linting and type checking (dev)

## Feature Set

- **Account pool management**: Load and manage multiple B2 accounts from `accounts.yaml`
- **Centralized file cache**: Built once at server startup; provides read-only view of all files across all accounts
- **Best-fit upload routing**: Selects the account that maximizes packing efficiency (most full account that can still fit the file)
- **Capacity tracking**: Tracks both byte capacity (configurable per account) and file count limits (default 1700 per account)
- **Web UI** (HTML + HTMX, single-page-ish, no JS framework):
  - Live file listing with metadata (name, size, content type, upload time, accounts)
  - Real-time server-side search filtering as you type (HTMX fragment swap)
  - One-click streaming downloads from B2
  - Multi-file upload with best-fit routing (HTMX; table + stats refresh live)
  - Account status dashboard with live stat cards
- **JSON API endpoints**:
  - `GET /api/files` — List all files with metadata
  - `GET /api/accounts` — Account status and active count
- **Test scripts**: Validate config, initialize accounts, create/delete test files
- **Docker support**: Ready-to-use Dockerfile and docker-compose.yml

## Constraints

- **File cache is static**: Built once at startup and never refreshed. New files uploaded outside the server won't appear until restart.
- **No duplicate filenames**: Each file name must be unique across the entire pool; uploading a duplicate raises an `AssertionError`.
- **Accounts immutable after cache build**: Individual accounts cannot be removed after the cache is built.
- **Capacity limits enforced**: Both byte capacity and max file count per account are enforced during upload routing.
- **Single bucket per account**: The code expects exactly one bucket per account (`assert len(buckets) == 1`).

## Goal

Provide a minimal, clean B2 account pool management library with centralized caching and best-fit routing, accessible via a simple web interface and JSON API. Designed for step-by-step execution without unnecessary frameworks or CLI abstractions — credentials go in `accounts.yaml` (gitignored), with an `accounts.yaml.example` template.

## Project Structure

```
b2router/
├── Dockerfile            # Python 3.11-slim image, installs package in editable mode
├── docker-compose.yml    # Builds b2router; mounts ./accounts.yaml:/app/accounts.yaml:ro
├── accounts.yaml         # **Gitignored** — your B2 credentials and account config
├── accounts.yaml.example # Template with placeholder credentials
├── pyproject.toml        # Project metadata, dependencies, ruff/mypy config
├── LICENSE               # Apache 2.0
├── src/
│   └── b2router/
│       ├── __init__.py           # "# B2 Router - Flask service for Backblaze B2"
│       ├── server.py             # Entrypoint: load accounts, run Flask
│       ├── web.py                # Flask app + HTMX fragment endpoints
│       ├── static/               # Vendored front-end assets
│       │   ├── htmx.min.js       # HTMX (HTML-over-HTML interactivity)
│       │   └── style.css         # Theme (light/dark, responsive)
│       ├── templates/            # Jinja2 templates (HTMX fragments)
│       │   ├── base.html         # Layout: nav + stylesheet + HTMX
│       │   ├── index.html        # Shell: stats, search, file table, upload region
│       │   └── fragments/
│       │       ├── stats.html    # Stat cards fragment
│       │       ├── files_table.html  # File table / empty-state fragment
│       │       └── upload_form.html  # Upload drop-zone fragment
│       ├── core/
│       │   └── config_reader.py  # YAML parsing → AccountConfig / Config dataclasses
│       ├── services/
│       │   ├── account.py        # Account dataclass wrapping B2Api + Bucket
│       │   └── pool.py           # AccountPool with cache, best-fit routing, upload API
│       ├── tests/
│       │   ├── __init__.py
│       │   ├── test_accounts.py   # Init all accounts, print cache state
│       │   ├── test_config_reader.py  # Validate config, output structured results
│       │   └── test_single_account.py # Init single account, list its files
│       └── utils/
│           ├── __init__.py       # "# Utils package"
│           ├── create_file.py    # CLI: upload test file to a specific account
│           └── delete_file.py    # CLI: delete a file from an account
```

## Quick Start

1. **Copy the example config** and fill in your credentials:

   ```bash
   cp accounts.yaml.example accounts.yaml
   # Edit accounts.yaml with your B2 account_id, account_key, and desired capacity_in_mb
   ```

2. **Start the server**:

   ```bash
   python -m b2router.server
   # or via Docker: docker compose up --build
   ```

3. **Open your browser** at `http://localhost:8000`

4. **Upload files** via the web UI or use the test scripts:

   ```bash
   # List all files and cache stats
   python -m b2router.tests.test_accounts

   # Validate config parsing
   python -m b2router.tests.test_config_reader

   # Create a test file in a specific account
   python -m b2router.utils.create_file account-1 myfile.txt 10
   ```

## Configuration (`accounts.yaml`)

```yaml
max_files_per_account: 1700
accounts:
  - account_id: "f059ee450928"
    account_key: "005a216fca39fe8cfe8216043591e947c16a538340"
    capacity_in_mb: 10240  # 10 GB (optional; omit for unlimited)
  # ... more accounts
```

- `max_files_per_account`: Global default per account (default 1700, overridden per-account if specified)
- Each account requires: `account_id`, `account_key`
- `capacity_in_mb`: If set, enforces byte capacity limit; `capacity_bytes` property converts MB → bytes
- Account name is auto-generated from `account_id` (e.g., `account-f059ee45`)

## API Endpoints

| Method | Endpoint          | Description                                              |
|--------|-------------------|----------------------------------------------------------|
| `GET`  | `/`               | File listing + upload form (HTMX shell)                  |
| `GET`  | `/files?q=`       | HTMX fragment: file table (server-side search filter)    |
| `GET`  | `/stats`          | HTMX fragment: account-pool stat cards                   |
| `POST` | `/upload`         | HTMX: upload files; OOB refresh of table + stats         |
| `GET`  | `/download/<name>`| Stream a file from B2                                    |
| `GET`  | `/api/files`      | JSON: file cache + stats                                 |
| `GET`  | `/api/accounts`   | JSON: active accounts + stats                            |

## Development

- **Lint**: `ruff check .`
- **Type check**: `mypy src/b2router/`
- **Run tests**: `python -m pytest tests/` (if configured) or run the test scripts directly
- **Docker**: `docker compose up --build` builds and starts the server

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
