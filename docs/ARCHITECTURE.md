# B2 Router Architecture

## Overview
A Python command-line tool that routes files from a local directory to multiple Backblaze B2 buckets
across different accounts. Each account has a capacity limit, and files are allocated using a
greedy best-fit knapsack algorithm. The tool supports listing bucket contents and moving files
with safety features including dry-run, confirmation prompts, and atomic operations.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              B2 ROUTER                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │   Config     │───▶│   Discovery  │───▶│  Allocation  │───▶│  Upload  │  │
│  │   Loader     │    │   & Query    │    │   Engine     │    │  Engine  │  │
│  └──────────────┘    └──────────────┘    └──────────────┘    └──────────┘  │
│         │                    │                    │                   │      │
│         ▼                    ▼                    ▼                   ▼      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────┐  │
│  │ accounts.yaml│    │  B2 API      │    │ Greedy       │    │ Streaming│  │
│  │ Validation   │    │  (b2sdk)     │    │ Best-Fit     │    │ Upload   │  │
│  │ + Retry      │    │  + Retry     │    │ + Account    │    │ + Progress│ │
│  └──────────────┘    └──────────────┘    │   Capacity   │    │ + Resume │  │
│                                          └──────────────┘    └──────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Core Data Models

### FileMetadata
```python
@dataclass(slots=True, eq=True, frozen=True)
class FileMetadata:
    """Represents a single file in a B2 bucket with full identity."""
    file_name: str
    size: int
    content_sha1: Optional[str] = None
```

### Bucket
```python
@dataclass(slots=True)
class Bucket:
    """Represents a B2 bucket with capacity management."""
    name: str
    id_: str
    capacity_bytes: int = 0
    used_bytes: int = 0
    files: List[FileMetadata] = field(default_factory=list)
    _file_names: set = field(default_factory=set, repr=False)
    account: Optional['Account'] = field(default=None, repr=False)
    _populate_failed: bool = field(default=False, repr=False)
    # Runtime-only fields (added dynamically during allocation):
    _account_remaining: int  # Runtime: account-level remaining bytes
    _account_capacity: int   # Runtime: account total capacity bytes

    @property
    def remaining_bytes(self) -> int:  # capacity_bytes - used_bytes

    def add_file(self, name, size, sha1):  # Updates both files list and _file_names set

    def has_file(self, name) -> bool:      # O(1) set lookup
```

### Account
```python
@dataclass(slots=True)
class Account:
    """Represents a Backblaze B2 account with credentials and buckets."""
    name: str
    account_id: str
    master_key: str
    capacity_in_gb: int
    buckets: List[Bucket] = field(default_factory=list)
    client: Optional[B2Api] = field(default=None, repr=False)  # Cached authenticated client (lazy init)
```

### SourceFile
```python
@dataclass(slots=True)
class SourceFile:
    """Represents a source file to be uploaded."""
    abs_path: Path
    size: int
    rel_path: str
```

### AllocationEntry (for persistence/resume)
```python
@dataclass(slots=True)
class AllocationEntry:
    """Represents a file allocation for persistence."""
    abs_path: str
    size: int
    rel_path: str
    bucket_name: str
    bucket_id: str
    account_name: str
    object_name: str
    uploaded: bool = False
```

### MoveState (for resume capability)
```python
@dataclass(slots=True)
class MoveState:
    """Persisted state for resume capability."""
    source_dir: str
    accounts_config: str
    created_at: str
    updated_at: str
    allocations: List[AllocationEntry]

    @staticmethod
    def create(source_dir, accounts_config, allocation) -> 'MoveState'
    def to_json(self) -> str
    @staticmethod
    def from_json(json_str) -> 'MoveState'
    def save(self, source_dir)
    @staticmethod
    def load(source_dir) -> Optional['MoveState']
    def get_pending(self) -> List[AllocationEntry]
    def mark_uploaded(self, abs_path)
    def is_complete(self) -> bool
    def cleanup(self, source_dir)
```

---

## Configuration System

### accounts.yaml Structure
```yaml
accounts:
  account_name:          # Arbitrary identifier (used in logs)
    account_id: "..."    # B2 Application Key ID (required, non-empty)
    master_key: "..."    # B2 Application Key (required, non-empty)
    capacity_in_gb: 10   # Positive integer (total account limit)
```

### Validation (load_config)
1. File existence check
2. YAML parsing with safe_load
3. Required keys: `account_id`, `master_key`, `capacity_in_gb`
4. Non-empty string validation for credentials
5. Positive integer validation for capacity
6. Returns `Dict[str, Account]` keyed by account name

---

## B2 API Layer

### Client Management (get_b2_client)
- **Lazy initialization**: Creates `B2Api()` on first use
- **Caching**: Stores client in `Account.client` for reuse
- **Authentication**: `authorize_account(realm="production", account_id, master_key)`
- **Thread-safe**: Each account has its own client instance

### Bucket Discovery (discover_and_add_buckets_for_account)
1. Clear existing buckets (idempotent)
2. `client.list_buckets()` with retry - Get all buckets in account
3. Handle SDK version differences for bucket ID (`id_` vs `bucket_id`)
4. Create `Bucket` objects with placeholder capacity
5. **Equal capacity division**:
   - `total = account_capacity_gb * 1024^3 * 0.99`
   - `per_bucket = total // num_buckets`
6. Set `bucket.capacity_bytes = per_bucket`
7. **Errors propagate** (no silent failures)

### File Population (populate_bucket_files_and_usage)
1. Clear `files` list and `_file_names` set
2. `client.get_bucket_by_name(bucket.name)`
3. `b2_bucket.ls(recursive=True)` with retry - Iterate all objects
4. Sum sizes, create `FileMetadata` entries
5. **Failure handling**: On exception after retries, set `_populate_failed = True`, don't update `used_bytes`
6. On success: `bucket.used_bytes = total`

### Parallel Processing (build_account_state)
- Optional `ThreadPoolExecutor` for multi-account queries (used by `list --parallel`)
- `move` command uses sequential by default (avoids rate limiting)
- Waits for all futures, propagates exceptions

---

## File Collection

### collect_source_files(source_dir)
```python
def collect_source_files(source_dir: str) -> List[SourceFile]:
    """Returns list of SourceFile dataclass objects."""
```

**Behavior:**
- Walks directory tree with `followlinks=False` (security)
- Uses `lstat()` not `stat()` - does NOT follow symlinks
- Filters: `stat.S_ISREG(mode)` - only regular files
- Skips: symlinks, directories, sockets, devices, pipes
- Handles: broken symlinks (OSError caught), permission errors
- Path normalization: `relative_to(source_path)` with `/` separators
- **Includes dotfiles** (hidden files starting with `.`)

---

## Allocation Engine

### Algorithm: Greedy Best-Fit with Account Capacity (allocate_files)
```python
def allocate_files(source_files, accounts) -> Dict[Path, Tuple[Bucket, str]]:
```

**Steps:**
1. **Flatten buckets**: Collect all non-failed buckets across accounts
2. **Calculate account-level usage**: Sum `used_bytes` across all buckets per account
3. **Sort files**: Descending by size (largest first)
4. **For each file**:
   - Find buckets where `min(bucket.remaining, account_remaining) >= file_size`
   - Select bucket with **minimum effective remaining** (best-fit)
   - Generate unique object name via `get_unique_object_name()`
   - Reserve space: update both `bucket.used_bytes` and `bucket._account_remaining`
5. **Skip**: Files that don't fit anywhere (logged)

**Conflict Resolution (get_unique_object_name):**
```
file.txt           → file.txt (if not exists)
file.txt           → Copy of (1) file.txt
Copy of (1) file.txt → Copy of (2) file.txt
```
- No nested prefixes ("Copy of Copy of" never occurs)
- Linear search with O(1) `has_file()` check

**Safety Features:**
- Skips buckets with `_populate_failed = True`
- CAP_SAFETY (0.99) leaves 1% headroom
- Per-bucket AND account-level limits enforced
- Prevents over-allocation when one bucket is empty but account is full

---

## Upload Engine

### upload_file (Streaming Upload with Retry)
```python
def upload_file(bucket, abs_path, object_name, size) -> bool:
```

**Implementation:**
```python
def _do_upload():
    b2_bucket = client.get_bucket_by_name(bucket.name)
    b2_bucket.upload_local_file(str(abs_path), object_name)
    return True

retry_with_backoff(_do_upload, max_retries=3, base_delay=1.0)
```

**Why upload_local_file:**
- Streams file from disk → avoids loading entire file into memory
- Handles large files (GB+) without OOM
- Automatic multipart upload for large files
- Computes SHA-1 on the fly for verification
- **Retry on transient errors** (B2Error, ConnectionError, TimeoutError)

### Atomic Move Operation (_execute_move)
```python
for abs_path, (bucket, object_name) in allocation.items():
    if upload_file(bucket, abs_path, object_name, size):
        if not delete_source_file(abs_path):
            raise RuntimeError("Deletion failed - INCONSISTENT STATE")
        success += 1
    else:
        raise RuntimeError("Upload failed - STOPPING IMMEDIATELY")
```

**Guarantees:**
- **Atomic per-file**: Delete only after successful upload
- **Fail-fast**: Any failure stops entire operation
- **No partial state**: Source files remain if upload fails
- **Verification**: Uses B2's built-in SHA-1 verification
- **Progress tracking**: tqdm progress bar with ETA, rate, elapsed

### Parallel Uploads
- `--parallel-uploads N` (1-10 workers)
- ThreadPoolExecutor with progress bar updates
- On failure: cancels pending futures, stops immediately

### Resume Capability (--resume)
- State persisted to `.b2router_state.json` in source directory
- Contains: allocation plan, upload status per file
- On resume: rebuilds allocation from state, skips completed uploads
- On success: removes state file
- On failure: state preserved for next resume attempt

### Post-Move Cleanup (cleanup_empty_dirs)
- `os.walk(topdown=False, followlinks=False)` - bottom-up, no symlinks
- `rmdir()` only empty directories
- Preserves non-empty directories and symlinks

---

## CLI Interface

### Commands
| Command | Purpose | Key Options |
|---------|---------|-------------|
| `list` | Query and display all accounts/buckets/files | `--parallel`, `-v`, `-q` |
| `move` | Move files from source to B2 | `--dry-run`, `--yes`, `--parallel-uploads N`, `--resume`, `-v`, `-q` |
| `copy` | Copy files from source to B2 (no deletion) | `--dry-run`, `--yes`, `--parallel-uploads N`, `--resume`, `-v`, `-q` |

### Global Options
- `--accounts PATH` - Config file (default: `accounts.yaml`)
- `-v, --verbose` - Debug output
- `-q, --quiet` - Suppress non-error output (also disables progress bar)
- `--version` - Show program version and exit
- `--realm {production,test}` - B2 realm (default: production)

### Move Command Safety Flow
```
1. Validate source directory exists
2. Collect source files
3. If --resume: load state, rebuild allocation
   Else: Query B2 state (build_account_state)
4. Allocate files (allocate_files)
5. Display allocation plan
6. If --dry-run: STOP
7. If not --yes: Prompt user, STOP if no
8. Execute uploads with progress bar + atomic delete
9. Save state after each file (for resume)
10. On failure: stop, preserve state for resume
11. On success: cleanup state file, cleanup empty dirs
12. Report success/failure
```

### Copy Command Safety Flow
```
1. Validate source directory exists
2. Collect source files
3. If --resume: load state, rebuild allocation
   Else: Query B2 state (build_account_state)
4. Allocate files (allocate_files)
5. Display allocation plan
6. If --dry-run: STOP
7. If not --yes: Prompt user, STOP if no
8. Execute uploads with progress bar (NO deletion)
9. Save state after each file (for resume)
10. On failure: stop, preserve state for resume
11. On success: cleanup state file
12. Report success/failure
```

---

## Error Handling Strategy

| Scenario | Behavior |
|----------|----------|
| Config file missing | Exit with error |
| Invalid YAML | Exit with parse error |
| Missing config keys | Exit with KeyError |
| Empty credentials | Exit with ValueError |
| Invalid capacity | Exit with ValueError |
| B2 auth failure | Exception propagates (traceback) |
| Bucket listing fails | Retry 3x with backoff, then propagate |
| File listing fails | Retry 3x, then mark bucket failed, skip allocation |
| File upload fails | Retry 3x, then stop entire operation |
| File delete fails | Stop entire operation immediately |
| Source file unreadable | Skip file, log warning |
| Broken symlink | Skip, log warning |
| No space in any bucket | Skip file, log warning |
| Transient network error | Auto-retry with exponential backoff (1s, 2s, 4s...) |

---

## Security Considerations

1. **No symlink following**: Both collection and cleanup use `followlinks=False`
2. **Credential validation**: Rejects empty credentials at load time
3. **No credential logging**: Keys never printed
4. **Atomic operations**: Prevents partial uploads leaving inconsistent state
5. **Path resolution**: `Path.resolve()` prevents directory traversal
6. **State file**: Contains no credentials, only allocation metadata

---

## Performance Characteristics

| Operation | Complexity | Notes |
|-----------|------------|-------|
| Config load | O(accounts) | Negligible |
| Bucket discovery | O(buckets) | One API call per account + retries |
| File listing | O(total_objects) | One `ls()` per bucket + retries |
| Allocation | O(files × buckets) | Greedy best-fit with account capacity |
| has_file lookup | O(1) | Set-based |
| Upload | O(file_size) | Streaming, network-bound |
| Parallel upload | O(files/workers) | Near-linear speedup for small files |

**Optimizations:**
- O(1) file existence check via `_file_names` set
- Client caching avoids re-authentication
- Optional parallel account processing (`list --parallel`)
- Optional parallel uploads (`move --parallel-uploads N`)
- Largest-first allocation reduces fragmentation
- Account-level capacity tracking prevents over-allocation
- Resume avoids re-uploading completed files

---

## Constants & Tuning

```python
CAP_SAFETY = 0.99              # Use 99% of declared capacity (1% safety margin)
STATE_FILE_NAME = ".b2router_state.json"
MAX_RETRIES = 3                # Retry attempts for transient errors
RETRY_BASE_DELAY = 1.0         # Initial retry delay in seconds
RETRY_MAX_DELAY = 30.0         # Maximum retry delay in seconds
MAX_PARALLEL_UPLOADS = 10      # Cap on parallel workers
```

**Rationale:**
- B2 has eventual consistency in usage reporting
- Prevents allocation edge cases at capacity boundary
- Accounts for B2 overhead (metadata, versions)
- Exponential backoff handles rate limiting gracefully

---

## Test Suite (test_bugs.py)

18 comprehensive tests covering:
1. Config validation (empty, missing keys, invalid values)
2. Unique name generation (no nesting, correct numbering)
3. Capacity division math
4. Idempotent bucket discovery
5. Bucket ID fallback handling
6. File deletion handling
7. Dotfile inclusion
8. Broken symlink skipping
9. Valid symlink skipping (not followed)
10. O(1) lookup performance
11. Populate failure flag behavior
12. Empty directory cleanup
13. Symlink safety in cleanup
14. FileMetadata equality semantics

---

## Known Limitations

1. **Per-bucket equal initial division**: Doesn't account for existing bucket usage skew initially
2. **No account-level capacity in config**: Derived from bucket count × per-bucket
3. **No resume for list command**: Only move command supports resume
4. **Single-threaded discovery**: `build_account_state` parallel but per-account sequential
5. **No progress bar for list**: Only move has progress bar
6. **No encryption/retention options**: Uses B2 defaults
7. **State file not encrypted**: Contains file paths and bucket names

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `b2sdk` | Official Backblaze B2 Python SDK |
| `pyyaml` | YAML configuration parsing |
| `tqdm` | Progress bar for uploads |
| `stdlib` | Everything else |

**Python**: 3.10+ (uses `dataclass(slots=True)`, `stat.S_ISREG`, `Path`, `typing`)

---

## File Structure

```
/home/txtr/b2router/
├── b2.py                 # Main application (single file)
├── architecture.md       # This document
├── accounts.yaml         # Production credentials (100+ accounts)
├── accounts.yaml.example # Template
├── test_bugs.py          # Comprehensive test suite
├── test_accounts.yaml    # Minimal test config
├── test_2accounts.yaml   # 2-account test config
├── test_3accounts.yaml   # 3-account test config
├── accounts_filtered.yaml# Subset of production accounts
└── .venv/                # Virtual environment
```

---

## Usage Examples

### List all buckets and files
```bash
python b2.py --accounts=accounts.yaml list --parallel
```

### Dry-run move (preview allocation)
```bash
python b2.py --accounts=accounts.yaml move /data --dry-run
```

### Execute move with confirmation
```bash
python b2.py --accounts=accounts.yaml move /data
# Prompts for confirmation
```

### Execute move non-interactively
```bash
python b2.py --accounts=accounts.yaml move /data --yes
```

### Parallel uploads (4 workers)
```bash
python b2.py --accounts=accounts.yaml move /data --yes --parallel-uploads 4
```

### Resume interrupted move
```bash
python b2.py --accounts=accounts.yaml move /data --resume --yes
```

### Verbose output
```bash
python b2.py --accounts=accounts.yaml move /data --yes -v
```

### Quiet output (errors only)
```bash
python b2.py --accounts=accounts.yaml move /data --yes -q
```

### Copy files (no deletion)
```bash
python b2.py --accounts=accounts.yaml copy /data --yes
```

### Copy files with parallel uploads
```bash
python b2.py --accounts=accounts.yaml copy /data --yes --parallel-uploads 4
```

### Show version
```bash
python b2.py --version
```

---

## Version History

### Bug Fixes Applied
1. **Bucket population failure handling** - Prevents over-allocation
2. **Streaming upload** - Fixes OOM on large files
3. **Symlink safety** - Prevents directory traversal
4. **Credential validation** - Early failure with clear messages
5. **O(1) file lookup** - Set-based `has_file()`
6. **FileMetadata equality** - Includes size and SHA-1
7. **Empty directory cleanup** - Post-move hygiene
8. **Fail-fast on upload/delete failure** - Atomicity guarantee
9. **Silent bucket discovery failure fixed** - Errors now propagate

### Features Added
1. **Parallel uploads** - `--parallel-uploads N` (1-10 workers)
2. **Resume capability** - `--resume` from persisted state file
3. **Progress bar** - tqdm with ETA, rate, elapsed time
4. **Account-level capacity** - Allocation respects total account limit
5. **Retry logic** - Exponential backoff for transient B2 errors
6. **Dataclasses with slots** - Cleaner code, better performance
7. **Logging module** - Structured logging with verbose/quiet flags
8. **Copy command** - Upload without deleting source files
9. **Batch state saves** - Save state every 5 files for performance
10. **Config validation** - Duplicate account_id detection
11. **Realm support** - `--realm` for B2 partner/testing environments
12. **Logging to stderr** - Clean stdout for piping

---

## Future Enhancement Opportunities

1. **Config-driven bucket weights** - Prefer specific buckets for specific file types
2. **Parallel discovery** - ThreadPoolExecutor for bucket listing within account
3. **Checksum verification** - Optional post-upload SHA-1 verification
4. **Metrics export** - Prometheus/JSON output for monitoring
5. **Bucket lifecycle rules** - Auto-transition to cold storage
6. **File filtering** - Include/exclude patterns (glob, regex)
7. **Bandwidth limiting** - Rate limiting for uploads
8. **Web UI / TUI** - Interactive bucket browser
9. **State file encryption** - For sensitive environments
10. **Multi-source merge** - Combine multiple source dirs into single move