# b2router

A single-process gateway that spreads files across a pool of Backblaze B2
accounts and exposes one hardened HTTP surface for upserts, downloads, routing,
and observability.

The process is deliberately opinionated:

- **One writer.** A `flock` prevents a second instance from sharing in-memory
  state. `accounts.yaml` is read once at startup; there is no runtime
  add/remove-account API.
- **Jobs for mutation.** Upserts spool to a private temp file, return `202`, and
  report progress through pollable jobs. A missing name is created normally; an
  existing name is staged in a different account before the old version is
  deleted. Deletes stay synchronous because they are cheap and must not leave a
  name in limbo.
- **JSON API, HTML UI.** `/api/*` never returns HTML. Failures are
  `application/problem+json` (RFC 9457). `/ui/*` is server-rendered with HTMX;
  its mutations call the same in-process services as the API, never a second
  implementation.
- **B2 is the source of truth at boot.** The cache is built from the latest
  version in every account before the first request. Duplicate names across
  accounts abort startup loudly instead of silently losing data.

## Quick start

Requirements: Python 3.11+, a B2 application key per account with read/write
access to a single bucket.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'

cp accounts.example.yaml accounts.yaml
chmod 600 accounts.yaml                  # contains live B2 application keys
b2router
# -> http://127.0.0.1:8000/ui/
```

`accounts.yaml` is read before the HTTP server binds:

```yaml
# Optional; default is B2's 1700-file bucket-object limit.
max_files_per_account: 1700

accounts:
  - account_id: 0011aabb22ccdd33
    account_key: K001...your-application-key...
    capacity_in_mb: 10240   # optional; omit for unlimited
  - account_id: 0044eeff55aa66bb
    account_key: K004...
```

Each application key must be able to list exactly one bucket. The account
display name is derived as `account-<first 8 characters>`; it is stable and is
what `/api/accounts` reports. Capacity is a soft local budget: the router keeps
`capacity_in_mb` worth of bytes plus `max_files_per_account` objects on an
account. B2 itself remains authoritative for what exists.

### Environment

All settings are optional. Invalid values are collected and reported together
at startup.

| Variable | Default | Purpose |
| --- | --- | --- |
| `B2ROUTER_HOST` | `127.0.0.1` | Bind address. Containers usually set `0.0.0.0`; keep the published port on loopback unless `B2ROUTER_API_TOKEN` is set. |
| `B2ROUTER_PORT` | `8000` | Bind port. |
| `B2ROUTER_ACCOUNTS` | `accounts.yaml` | Config path. |
| `B2_CONNECT_TIMEOUT` | `30` | B2 connection timeout (seconds). |
| `B2_READ_TIMEOUT` | `120` | B2 read timeout (seconds). |
| `B2ROUTER_MAX_UPLOAD_BYTES` | `10737418240` | Per-file size limit (10 GiB). |
| `B2ROUTER_MAX_FILES_PER_REQUEST` | `20` | Multipart upsert batch limit. |
| `B2ROUTER_MAX_FILENAME_BYTES` | `1000` | UTF-8 filename limit. |
| `B2ROUTER_UPLOAD_WORKERS` | `2` | Concurrent B2 transfers. |
| `B2ROUTER_QUEUE_MAX` | `100` | Queued-job limit before `429 queue_full`. |
| `B2ROUTER_JOB_TTL_SECONDS` | `3600` | Terminal jobs are pruned after this. |
| `B2ROUTER_JOB_HISTORY_MAX` | `500` | Retained job records. |
| `B2ROUTER_TEMP_DIR` | `<tmp>/b2router` | Spool directory (needs disk ≥ upload size). |
| `B2ROUTER_LOCK_FILE` | `<temp>/instance.lock` | Single-writer lock. |
| `B2ROUTER_ACQUIRE_LOCK` | `1` | Disable only for tests. |
| `B2ROUTER_API_TOKEN` | unset | Optional shared token: bearer auth for `/api`, sign-in for `/ui`. |
| `B2ROUTER_SECRET_KEY` | random each boot | Session signing key; set it to keep UI logins across restarts. |
| `B2ROUTER_COOKIE_SECURE` | `0` | Set to `1` when the UI is served over HTTPS. |
| `B2ROUTER_TRUST_PROXY` | `0` | Trust `X-Forwarded-For` for client identity. |
| `B2ROUTER_MUTATION_RATE_PER_MINUTE` | `240` | Per-IP mutation budget. |
| `B2ROUTER_CORS_ORIGINS` | unset | Comma-separated allowlist for cross-origin API use. |
| `B2ROUTER_LOG_FORMAT` | `pretty` | `json` for production log shipping. |
| `B2ROUTER_LOG_LEVEL` | `INFO` | Standard Python level. |
| `B2ROUTER_METRICS` | `1` | Expose Prometheus metrics. |

The console is same-origin. When `B2ROUTER_API_TOKEN` is set, `/ui` requires a
sign-in with that token and stores only a signed session cookie (HttpOnly,
SameSite=Strict). Every UI form also carries a per-session CSRF token. Without
a token the console is open, which is appropriate only for a trusted,
same-origin deployment. See [Web console](#web-console) and [Security](#security).

## API at a glance

No version prefix. Canonical content URLs use `/api/download/<name>` so names
containing `/` are unambiguous; `/api/files/<name>/content` is an alias.

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/health` | Liveness, uptime, revisions. |
| `GET` | `/ready` | Readiness (pool loaded, workers alive). |
| `GET` | `/metrics` | Prometheus exposition. |
| `GET` | `/` | Service index with links. |
| `GET` | `/api/stats` | Aggregate bytes/files/accounts + `ETag`. |
| `GET` | `/api/accounts` | Per-account capacity, usage, revisions. |
| `GET` | `/api/accounts/{name}` | One account. |
| `GET` | `/api/files` | Cursor list; `q`, `account`, `limit`, `after`, `sort`, `order`. |
| `GET` | `/api/files/{name}` | Cache metadata + `ETag`. |
| `GET`/`HEAD` | `/api/download/{name}` | Object bytes; `Range`, `If-None-Match`. |
| `POST` | `/api/files` | Batch upsert (one or more `file` parts, each under its own filename) → `202` jobs. |
| `DELETE` | `/api/files/{name}` | Synchronous delete → `204`; optional `If-Match`. |
| `POST` | `/api/routing-plans` | Side-effect-free placement preview. |
| `GET` | `/api/jobs` | List jobs; `state`, `updated_since`, `limit`. |
| `GET` | `/api/jobs/{id}` | Poll job state/progress/result. |
| `DELETE` | `/api/jobs/{id}` | Cancel a queued job or dismiss a terminal one. |
| `GET` | `/api/audit` | Recent mutation ring buffer. |

The full failure contract is in [`docs/problems.md`](docs/problems.md).

## Type system & implementation details

### Shared result type

`src/b2router/utils/result.py` defines a generic `Result[T, E] = Ok[T, E] | Err[T, E]` used by
`ConfigReader.read()` and `AccountPool._plan()`. Removes duplicate `Ok/Err` definitions.

### Semantic ID types (`NewType`)

`src/b2router/types.py` provides zero-cost `NewType` aliases:
`FileId`, `JobId`, `AccountName`, `FileName`, `RequestId`, `ReservationId`.
Catch `file_id = job_id` bugs at type-check time; erased at runtime.

### TypedDict API contracts

`src/b2router/api/types.py` documents every JSON response shape with `TypedDict`
(`FileEntryDict`, `JobSnapshotDict`, `RoutingPlanDict`, etc.). Enables precise
type checking in route handlers and tests.

### `ProgressListener` protocol

`src/b2router/services/account.py` defines a `ProgressListener` protocol
instead of depending on `b2sdk.v2.AbstractProgressListener`. The worker's
`_ProgressListener` implements this protocol, decoupling the gateway from the SDK.

### `Literal` for fixed strings

`FileVersion.action: Literal["upload", "hide"]`, `UploadJob.operation: Literal["upsert"]`.
Exhaustiveness-checked by mypy.

## Hidden-version accounting

`AccountPool.add_account()` counts **every** upload version (`action == "upload"`) from
`list_versions()`, not just the latest visible version from `list_files()`. This
means hidden/superseded versions consume capacity and file slots in the local
budget, matching what B2 actually stores. Hide markers (`action == "hide"`) are
not counted.

## Immutable `file_size` in upload jobs

`UploadJob.update_progress()` ignores the SDK's reported `total` and uses the
spooled size captured at submit time. This prevents the SDK from overwriting
the value used for capacity accounting and post-upload verification.

## Executor-submit cleanup

If `ThreadPoolExecutor.submit()` raises (pool exhausted), the temp file is
deleted and the per-name lock is released **before** `UploadRejected` is raised.
No leaked temp files or stuck names.

## Log context isolation

Context variables (`job_id_var`, `request_id_var`, `client_label_var`) are set
at worker start and cleared in a `finally` block. No cross-contamination
between concurrent jobs.

## Cancelled-future removal in shutdown

`UploadManager.shutdown()` filters cancelled futures from `self._futures` before
calling `wait()`. Without this, Python 3.14 `concurrent.futures.wait()` would
block indefinitely on cancelled futures.

## Strong `If-Match` comparison

`validation.if_match()` implements **strong** ETag comparison per RFC 9110:
weak validators (`W/"..."`) are rejected; only exact opaque ETag matches
succeed. `validation.etag_matches()` (weak) is used only for `If-None-Match`.

## XSS-safe flash alerts

`static/app.js` builds DOM with `textContent` and `replaceChildren()`, never
`innerHTML`. Server-provided error details (which can contain attacker-controlled
filenames) are safely rendered.

## SIGTERM handler & guaranteed lock release

`server.py` installs a `SIGTERM → KeyboardInterrupt` handler so Docker/systemd
signals trigger the normal shutdown path. The writer lock is released in
**nested `finally` blocks** (`server.py:138-142`, `runtime.py:36-43`), ensuring
it is released even if `manager.shutdown()` raises.

## Docker stop grace period

`docker-compose.yml` sets `stop_grace_period: 1h` so Docker waits for accepted
uploads (up to 10 GiB) to drain before sending `SIGKILL`.

## `FileEntry.account_name` as single source of truth

`FileEntry` stores `account_name: str`. The legacy `.accounts` property returns
`(account_name,)` for API compatibility. `to_dict()` serializes
`"accounts": [account_name]`.

## Dead code removed

Removed: `set_max_files_per_account`, `accounts()`, `get_file_accounts`,
`list_files_for_account`, `file_cache_stats`, `can_fit`, `with_max_files`,
`with_file`, `shutdown_app`, `run_server`, `current_request_id`, `parse_etag`,
`FileEntryLike`, `format_file_entry`, `format_files`.

## Manager consistency guard

`create_app()` validates `manager.pool is pool` and `manager.audit is audit`,
exposing `UploadManager.pool` and `.audit` as read-only properties. Prevents
accidental mismatch between injected dependencies.

## Reconcile `--apply` guard

`b2router-reconcile --apply` refuses to delete anything when `account_errors`
is non-empty (an account could not be listed). The report includes
`apply_refused: true` and a message. This prevents deleting the real keeper
from an unreadable account.

## Centralized error mapping

`storage_error_status(code)` in `errors.py` maps B2 error codes to HTTP status:
`b2_unauthorized→502`, `b2_rate_limited→503`, `b2_unavailable→503`,
`b2_timeout→504`, `range_not_satisfiable→416`.

## Multipart staging security

`ensure_upload_space()` rejects `507 insufficient_storage` **before** Flask
parses the multipart body when the declared `Content-Length` would exhaust the
staging volume. `TMPDIR=/data/tmp` in Docker ensures Werkzeug's internal spool
uses the persistent volume, not the container layer. Spool directory is `0700`,
files `0600`.

## CLI tools (all take the writer lock)

| Tool | Purpose |
|------|---------|
| `b2router-reconcile [--apply] [--pretty]` | Cross-account duplicate detection; `--apply` deletes all but newest per name; refuses if any account unreadable; exits 3 if gateway holds lock |
| `b2router-create-file <account> <name> (--file <path> \| --size-mb <N>)` | Upload test object directly to one account; visible after gateway restart; exits 3 if gateway holds lock |
| `b2router-delete-file <account> <name>` | Delete object directly from one account; exits 3 if gateway holds lock |

## Lockfile (`InstanceLock`)

Advisory `fcntl.flock()` on a small file (`B2ROUTER_LOCK_FILE`). Writes owning
PID for debugging. Non-blocking (`LOCK_NB`); fails fast with `LockError` if
another writer exists. Context manager support.

## Audit log

Thread-safe ring buffer (`deque` with `maxlen=max(job_history_max, 1000)`).
Each entry: `id`, `timestamp`, `action` (upsert/delete), `result`
(success/partial/failed/cancelled), `file_name`, `account_name`, `file_size`,
`error_code`, `request_id`, `client`, `duration_ms`, `details`. `GET /api/audit`
supports pagination.

## Metrics (Prometheus)

Exposed at `/metrics` (enabled by `B2ROUTER_METRICS=1`). Bounded label
cardinality: route rules, stable error codes, operation names. Key metrics:

| Metric | Type | Labels |
|--------|------|--------|
| `b2router_requests` | Counter | method, route, status |
| `b2router_request_duration_seconds` | Histogram | method, route |
| `b2router_files` / `b2router_used_bytes` / `b2router_capacity_bytes` | Gauge | — |
| `b2router_jobs_queued` / `b2router_jobs_active` | Gauge | — |
| `b2router_uploads` | Counter | result (success/partial/failed) |
| `b2router_b2_request_duration_seconds` | Histogram | operation |
| `b2router_b2_errors` | Counter | operation, code |

`/metrics` returns 404 if `B2ROUTER_METRICS=0`.

## Health & Ready endpoints

| Endpoint | Purpose | Checks |
|----------|---------|--------|
| `GET /health` | Liveness | Version, uptime, pool/jobs revision |
| `GET /ready` | Readiness | `len(pool) > 0`, workers > 0, `not manager.closed` |

`/ready` returns 503 with `checks` extension if any check fails.

## Docker multipart spool

Both `B2ROUTER_TEMP_DIR` and `TMPDIR` are set to `/data/tmp` in `docker-compose.yml`
and `Dockerfile`. This routes **both** the gateway's spool and Werkzeug's internal
multipart parser to the persistent volume.

## Login limiter proxy-aware

`_client_address(settings)` reads `X-Forwarded-For` when `B2ROUTER_TRUST_PROXY=1`
for both the mutation limiter and the login limiter. Prevents bypass via
spoofed `X-Client-Name`.

## `jobs_snapshot()` atomic ETag

`UploadManager.jobs_snapshot()` returns `(jobs, total, revision)` from a single
consistent read under the job lock. `GET /api/jobs` uses this for `ETag` and
`X-B2Router-Revision` headers.

## Rate-limit retry across accounts

`AccountState.rate_limited_until` + `mark_rate_limited(duration=60)`. Routing
skips rate-limited accounts. Worker retries on next available account (up to
*N* attempts where *N* = pool size). Avoids hammering a throttled account.

## Web console

`/ui` is now a **separate repository** (`b2router-ui`) communicating via the
gateway's JSON API. It shares no code with the gateway; templates and static
assets were moved. The gateway's `/ui` routes were removed. See
<https://github.com/txtr/b2router-ui>.

## Security (additional)

- **Constant-time token comparison**: `hmac.compare_digest()` for bearer and UI sign-in.
- **Network-identity rate limiting**: Mutation budget keyed by client IP (`X-Forwarded-For` if `trust_proxy`), never `X-Client-Name`.
- **Request ID correlation**: Accepted from `X-Request-ID`, echoed in response header and logs.
- **Content-Security-Policy**: Self-hosted scripts/styles only, `img-src data:` for favicon, `frame-ancestors 'none'`.
- **Referrer-Policy: no-referrer**, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, minimal `Permissions-Policy`.
- **Cookie security**: `HttpOnly`, `SameSite=Strict`, `Secure` when `B2ROUTER_COOKIE_SECURE=1`.
- **CSRF**: Per-session token in UI forms, validated on every mutation.
- **Bound everything**: Body size, batch size, queue length, worker count, job history, audit history, filename length, request rate.

## Development

```bash
pip install -e '.[dev]'
ruff check src/b2router
ruff format --check src/b2router
mypy src/b2router
PYTHONPATH=src pytest -q
```

Tests use a fake storage backend and never contact B2. The suite covers
routing/reservations, conflict handling, async jobs, ambiguous-upsert recovery,
RFC 9457 shapes, auth, rate limiting, CORS, ranges/ETags, config/settings
validation, and the UI's sign-in, CSRF, fragments, upserts, deletes, and
download proxy.

The process is intentionally the Flask development server for 0.0.1
(`threaded=True`, no reloader). The in-memory cache, reservations, jobs, and
`flock` depend on a **single process**; do not run it behind a preforking
application server.

## Docker

```bash
docker compose up --build
# http://127.0.0.1:8000/ui/
```

The image runs as a non-root user, exposes `8000`, installs the package with its
templates/static assets, and keeps spool/lock state in the `b2router-data`
volume. Mount `accounts.yaml` read-only at `/config/accounts.yaml`; keep the
host file mode at `0600`. Compose allows up to an hour for graceful shutdown so
accepted uploads can drain before Docker sends `SIGKILL`.
Compose publishes `127.0.0.1:8000:8000` by default. Exposing the port on a
remote interface requires an explicit `B2ROUTER_API_TOKEN` and a deliberate
port/network change; without the token the console and API are unauthenticated.

`/ui` is a server-rendered operator console. It reads the same in-memory pool,
job manager, and audit log as the API, and calls the same mutation services, so
there is no second implementation to drift. Navigation is ordinary HTML;
HTMX supplies live stats, file search/filtering, job polling, upsert results,
and delete/refresh fragments. Downloads are proxied through
`/ui/download/<name>` so a session cookie is enough (the raw API still expects a
bearer token when configured).

Pages:

| Path | Purpose |
| --- | --- |
| `/ui/` | Pool hero, live stats, recent files/jobs/activity. |
| `/ui/files` | Searchable/sortable file cache with download and delete. |
| `/ui/uploads` | Multi-file upsert form and live job history. |
| `/ui/accounts` | Per-account budget, utilization, slot usage. |
| `/ui/audit` | Filterable mutation audit trail. |
| `/ui/login` | Token sign-in when `B2ROUTER_API_TOKEN` is set. |

Static assets are vendored under `src/b2router/static/vendor/` (HTMX 2.0.11,
Bootstrap 5.3.8, Bootstrap Icons 1.13.2); the console makes no CDN requests.
Upserts are queued as jobs and polled as fragments, so a slow B2 transfer never
blocks the page. A completed job emits an HTMX `jobs-changed` event that
refreshes the visible file table and stats.

### Upsert

Every create-or-replace is an upsert. `POST /api/files` accepts one or more
`file` parts; each part keeps its own filename, so neither the API nor the
console needs a separate target-name field:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/files \
  -F 'file=@report.pdf;type=application/pdf' \
  -H 'X-Client-Name: nightly-export'
# 202
# {"jobs":[{"id":"job_…","operation":"upsert","file_name":"report.pdf",
#           "state":"queued","progress":0.0,...}]}
```

A missing name is created in the best-fit account. An existing name is staged
in a **different** account with a free file slot and enough free bytes; after
that upload succeeds, the old version is deleted. The source account is never
used for the replacement, even when it has room. A single-account pool can
therefore create files but cannot replace them.

Poll each id:

```bash
curl -sS http://127.0.0.1:8000/api/jobs/job_…
```

Terminal states are `complete`, `partial`, `failed`, and `cancelled`. The
result carries the winning `account_name`, `file_id`, and the new cache entry.
A replacement also sets `replaced` to the old entry. `complete` means the old
version was removed. If the old version cannot be deleted, the router rolls the
new version back and reports `failed` with `old_version_retained`, so the same
file name does not remain duplicated. If no other account can stage the
replacement, the job fails with `no_replacement_account` and the original is
untouched.

`partial` means the router could not prove the final B2 state: either the old
version could not be confirmed removed and rollback also failed, or an upload
timed out after B2 may have accepted it. The latter reports
`error.code = "upload_outcome_unknown"`. Upserts carry a `b2router_job_id`
marker in B2 `fileInfo`; recovery only commits an exact marker and size match.
After bounded retries without a match, the job stays `partial` rather than
guessing. See [Reconciliation](#reconciliation).

The filename in each multipart part is the target name. There is no separate
named-upsert route; to replace `report.pdf`, upload a part whose filename is
`report.pdf`.

### Download and ranges

```bash
curl -sS -D - http://127.0.0.1:8000/api/download/report.pdf \
  -H 'Range: bytes=0-1023'
```

`200` includes `ETag: "<opaque-etag>"` and `Accept-Ranges: bytes`; `206` includes
`Content-Range`; `304` is returned for a matching `If-None-Match`; `416` is a
problem document. File detail and content return the same opaque ETag, and the
delete `If-Match` precondition accepts it, so clients never need to mix
`file_id` and metadata generations. Downloads stream from B2 in 64 KiB chunks
and never buffer the whole object; if B2 fails mid-stream the connection is
aborted rather than returning a silently truncated `200`.

### Routing plans

Preview the pool's choice without reserving anything:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/routing-plans \
  -H 'Content-Type: application/json' \
  -d '{"files":[{"name":"a.bin","size":1048576}]}'
```

The response separates `routable` from `unroutable` with stable reasons
(`duplicate_in_batch`, `no_account_can_fit`, `no_replacement_account`) and
includes the remaining bytes/slots the decision would leave. Existing names
report their `source_account` and `replaces: true`; the chosen account is always
a different account. Routing is **best fit by remaining bytes**, with a stable
account-name tiebreak; it accounts for in-flight reservations.

## Router logic (account selection for upserts)

The router decides **which account receives a new or replacement file** at
`POST /api/files` time (and for preview via `POST /api/routing-plans`). The
algorithm is pure — no B2 calls — and runs under the pool lock so in-flight
reservations are visible to subsequent decisions in the same batch.

### 1. Inputs per file

| Input | Source |
|-------|--------|
| `file_name` | Multipart part filename (or `routing-plans` request body) |
| `file_size` | Declared `Content-Length` / spool size |
| `source_account` | Current cache holder of `file_name`, or `null` for new files |

### 2. Eligibility filter (per account, in deterministic name order)

An account is **eligible** for a file iff all hold:

- **Not the source account** (for replacements). A replacement is *always* staged
  in a different account; the source account's old bytes and slot stay occupied
  until the old version is deleted.
- **Not rate limited**. Accounts returning `429`/`b2_rate_limited` are marked
  for 60 s and skipped during routing. The worker will retry on another account.
- **Has a free file slot**. `remaining_files = max_files - file_count - reserved_files - batch_files > 0`.
- **Has enough free bytes** (if capacity is configured). `effective_remaining = capacity - used - reserved_bytes - batch_bytes ≥ file_size`. Unlimited accounts (`capacity_bytes = null`) always pass this check.

If no account passes, the file is **unroutable**:
- `no_replacement_account` — file exists but no *other* account can fit it
- `no_account_can_fit` — new file, no account has space/slots

### 3. Best-fit selection

Among eligible accounts, pick the one with the **smallest `remaining_after`**
(i.e. least free space left after placing this file):

```
remaining_after = effective_remaining - file_size
```

- Unlimited accounts get a synthetic `remaining_after = 10^18` (sorted last).
- Tiebreaker: account name (lexicographic), so the result is deterministic.

This "best fit" packs files tightly, leaving larger contiguous gaps for future
large uploads — analogous to bin packing.

### 4. Batch-aware accounting

When a request contains multiple files (`max_files_per_request`, default 20),
the router processes them **sequentially in request order** and updates
`batch_bytes` / `batch_files` after each decision. This means:

- File *N* sees the reservations of files *1..N-1* in the same batch.
- A large file early in the batch can "reserve" space that a later file would
  have used, causing the later file to route elsewhere or fail.

### 5. Reservations vs. commits

- **Routing (`plan`)** is read-only — no state changes.
- **`reserve(file_name, file_size)`** atomically routes *and* creates a
  `Reservation` (bytes + slot held) under the pool lock. The reservation is
  released in a `finally` block when the worker finishes (success, failure, or
  cancellation).
- **`commit_upload` / `commit_replace`** convert the reservation into a cache
  entry and update account `used_bytes` / `file_count`.

### 6. Rate-limit retry (worker side)

If the B2 upload returns `b2_rate_limited`:
1. The account is marked rate-limited for 60 s (`mark_rate_limited`).
2. The reservation is released.
3. The worker loops and calls `reserve()` again, which now skips that account.
4. Retries continue up to *N* attempts (where *N* = number of accounts in pool).

This avoids hammering a throttled account and gracefully fails over.

### 7. Preview endpoint

`POST /api/routing-plans` runs the exact same `_plan` logic **without** creating
reservations. Use it to dry-run a batch:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/routing-plans \
  -H 'Content-Type: application/json' \
  -d '{"files":[{"name":"a.bin","size":1048576},{"name":"b.bin","size":2097152}]}'
```

The response includes `routable` (with `account_name`, `remaining_bytes`,
`remaining_files`, `replaces`) and `unroutable` (with `reason`, `message`).

## Semantics worth knowing

### Conflict matrix

| Situation | Response |
| --- | --- |
| `POST` a name that exists | `202`; the job stages a replacement in another account and deletes the old copy |
| Existing name with no other account holding a free slot and enough bytes | `202`, then the job fails with `no_replacement_account`; the original is unchanged |
| Any upsert/delete while a job or delete owns that name | `409 job_active` |
| `DELETE` a missing name | `404 file_not_found` |
| `DELETE` with an `If-Match` that differs from the current file `etag` | `412 precondition_failed` |
| Queue is full | `429 queue_full` with `Retry-After`, checked before the multipart body is parsed/spooled |
| Same name twice in one batch | Per-file `409 duplicate_in_request`; `202` if another part was accepted, otherwise the batch problem |

B2 is never mutated until a name is exclusively claimed. The claim is released
in a `finally` block, so a crashed worker cannot permanently freeze a name.

### Caching and restarts

The cache is rebuilt at boot from every account's latest version. Upserts and
deletes mutate it at runtime through the API or console; both use the same
services. Account capacity is recomputed from B2 on restart. A job that was
mid-transfer when the process died leaves a spool file (cleaned at next start)
and no cache entry; B2 may or may not contain the object. If it does, the next
boot adopts it. An upsert that died after uploading the new version to another
account but before deleting the old one can temporarily leave the same name in
two accounts. Startup rejects that ambiguity, because downloads would otherwise
have no single answer:

```
$ b2router
{"event":"startup.account_failed","issue":"File 'x' exists in accounts ..."}
```

Stop the gateway and run the reconciler with `--apply` to keep the newest copy.

### Reconciliation

A file name should resolve to one visible upload version across the pool.
The router rolls back a new version when the old-version delete fails, but two
cases can still leave copies behind:

* If both the old-version delete and the rollback fail, the old and new versions
  remain in their respective accounts.
* If the process dies after staging the new version in another account but
  before deleting the old one, the same name exists in two accounts.

A latest-only listing cannot see any older same-account copy, but its bytes still
consume capacity. Startup lists all visible versions and refuses to run if a name
has more than one upload version in an account or appears in multiple accounts.
`b2router-reconcile` groups all upload versions by name **across accounts**,
reports groups with more than one copy, and can delete everything except the
newest:

```bash
B2ROUTER_ACCOUNTS=/etc/b2router/accounts.yaml b2router-reconcile --pretty
B2ROUTER_ACCOUNTS=/etc/b2router/accounts.yaml b2router-reconcile --apply --pretty
```

It takes the same `flock` as the server, so `--apply` refuses to run while the
gateway is live. `--apply` also deletes nothing if any account could not be
listed, because an unreadable account may hold the real keeper or additional
versions. The `b2router-create-file` and `b2router-delete-file` maintenance
commands also take the writer lock and exit with status `3` when the server is
running. Run mutating tools only with the gateway stopped. Without `--apply`
the reconciler is read-only.

## Security

- B2 keys live only in `accounts.yaml`; parsed errors never echo key material.
  The file is never exposed by the API or UI.
- Optional `B2ROUTER_API_TOKEN` is compared in constant time for API bearers
  and UI sign-in. Sessions are signed with `B2ROUTER_SECRET_KEY` (random at boot
  when unset); cookies are HttpOnly and SameSite=Strict.
- UI POSTs require a per-session CSRF token in addition to the session cookie.
  Sign-in attempts are rate limited, and failures are audited in the logs.
- Disable the token only on a trusted network / same-origin deployment.
- Every mutation is rate limited by **network identity**, not the spoofable
  `X-Client-Name` header.
- Request ids are accepted from `X-Request-ID` and echoed; logs are JSON by
  default in containers for correlation.
- Upsert filenames are normalised, length-checked, and rejected if they contain
  NULs, backslashes, `.`/`..` segments, control characters, or a leading `/`.
  The spool directory is mode `0700`, and every spool file is `0600`. Uploads
  are rejected with `507 insufficient_storage` before multipart parsing when the
  declared body would consume the staging volume. Spooling also refuses to
  start with less than 1 MiB free and converts disk write failures into `503
  service_unavailable` instead of leaking a server error. Container deployments
  set both `B2ROUTER_TEMP_DIR` and `TMPDIR` to the persistent `/data/tmp` volume
  so Werkzeug's multipart spool never writes to the image layer.
- Content-Disposition uses RFC 5987 encoding, never reflecting a raw header.
- Errors never include B2 response bodies, keys, or stack traces. `5xx`
  responses include a `request_id` and a generic detail.
- Bound everything: body size, batch size, queue length, worker count, job
  history, audit history, filename length, and request rate.
- UI responses send a restrictive Content-Security-Policy (self-hosted scripts
  and styles only, `img-src` data for the inline favicon), plus `nosniff`,
  `DENY` framing, and a minimal Permissions-Policy.

## Development

```bash
pip install -e '.[dev]'
ruff check src/b2router
ruff format --check src/b2router
mypy src/b2router
PYTHONPATH=src pytest -q
```

Tests use a fake storage backend and never contact B2. The suite covers
routing/reservations, conflict handling, async jobs, ambiguous-upsert recovery,
RFC 9457 shapes, auth, rate limiting, CORS, ranges/ETags, config/settings
validation, and the UI's sign-in, CSRF, fragments, upserts, deletes, and
download proxy.

The process is intentionally the Flask development server for 0.0.1
(`threaded=True`, no reloader). The in-memory cache, reservations, jobs, and
`flock` depend on a **single process**; do not run it behind a preforking
application server.

## Docker

```bash
docker compose up --build
# http://127.0.0.1:8000/ui/
```

The image runs as a non-root user, exposes `8000`, installs the package with its
templates/static assets, and keeps spool/lock state in the `b2router-data`
volume. Mount `accounts.yaml` read-only at `/config/accounts.yaml`; keep the
host file mode at `0600`. Compose allows up to an hour for graceful shutdown so
accepted uploads can drain before Docker sends `SIGKILL`.
Compose publishes `127.0.0.1:8000:8000` by default. Exposing the port on a
remote interface requires an explicit `B2ROUTER_API_TOKEN` and a deliberate
port/network change; without the token the console and API are unauthenticated.
