# b2router

A single-process gateway that spreads files across a pool of Backblaze B2
accounts and exposes one hardened HTTP surface for uploads, downloads, routing,
and observability.

The process is deliberately opinionated:

- **One writer.** A `flock` prevents a second instance from sharing in-memory
  state. `accounts.yaml` is read once at startup; there is no runtime
  add/remove-account API.
- **Jobs for mutation.** Uploads and upserts spool to a private temp file,
  return `202`, and report progress through pollable jobs. Deletes stay
  synchronous because they are cheap and must not leave a name in limbo.
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

cp accounts.example.yaml accounts.yaml   # edit securely
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
| `B2ROUTER_MAX_UPLOAD_BYTES` | `104857600` | Per-file size limit (100 MiB). |
| `B2ROUTER_MAX_FILES_PER_REQUEST` | `20` | Multipart upload batch limit. |
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
| `POST` | `/api/files` | Multipart upload (one or more `file` parts) → `202` jobs. |
| `PUT` | `/api/files/{name}` | Multipart upsert → `202` job; optional `If-Match`. |
| `DELETE` | `/api/files/{name}` | Synchronous delete → `204`; optional `If-Match`. |
| `POST` | `/api/routing-plans` | Side-effect-free placement preview. |
| `GET` | `/api/jobs` | List jobs; `state`, `updated_since`, `limit`. |
| `GET` | `/api/jobs/{id}` | Poll job state/progress/result. |
| `DELETE` | `/api/jobs/{id}` | Cancel a queued job or dismiss a terminal one. |
| `GET` | `/api/audit` | Recent mutation ring buffer. |

The full failure contract is in [`docs/problems.md`](docs/problems.md).

## Web console

`/ui` is a server-rendered operator console. It reads the same in-memory pool,
job manager, and audit log as the API, and calls the same mutation services, so
there is no second implementation to drift. Navigation is ordinary HTML;
HTMX supplies live stats, file search/filtering, job polling, upload results,
and delete/refresh fragments. Downloads are proxied through
`/ui/download/<name>` so a session cookie is enough (the raw API still expects a
bearer token when configured).

Pages:

| Path | Purpose |
| --- | --- |
| `/ui/` | Pool hero, live stats, quick upload, recent files/jobs/activity. |
| `/ui/files` | Searchable/sortable file cache with download, replace, delete. |
| `/ui/uploads` | New upload, upsert/replace, and live job history. |
| `/ui/accounts` | Per-account budget, utilization, slot usage. |
| `/ui/audit` | Filterable mutation audit trail. |
| `/ui/login` | Token sign-in when `B2ROUTER_API_TOKEN` is set. |

Static assets are vendored under `src/b2router/static/vendor/` (HTMX 2.0.11,
Bootstrap 5.3.8, Bootstrap Icons 1.13.2); the console makes no CDN requests.
Uploads are queued as jobs and polled as fragments, so a slow B2 transfer never
blocks the page. A completed job emits an HTMX `jobs-changed` event that
refreshes the visible file table and stats.

### Upload

```bash
curl -sS -X POST http://127.0.0.1:8000/api/files \
  -F 'file=@report.pdf;type=application/pdf' \
  -H 'X-Client-Name: nightly-export'
# 202
# {"jobs":[{"id":"job_…","operation":"upload","file_name":"report.pdf",
#           "state":"queued","progress":0.0,...}]}
```

Poll each id:

```bash
curl -sS http://127.0.0.1:8000/api/jobs/job_…
```

Terminal states are `complete`, `partial`, `failed`, and `cancelled`. The
result carries the winning `account_name`, `file_id`, and the new cache entry.
For upserts, `complete` means the old version was removed. If the old version
cannot be deleted, the router rolls the new version back and reports `failed`,
so the same file name does not remain duplicated. `partial` means the router
could not prove the final B2 state: either the old version could not be
confirmed removed and rollback also failed, or an upload timed out after B2 may
have accepted it. The latter reports `error.code = "upload_outcome_unknown"`.
Uploads carry a `b2router_job_id` marker in B2 `fileInfo`; recovery only commits
an exact marker and size match. After bounded retries without a match, the job
stays `partial` rather than guessing. See [Reconciliation](#reconciliation).

### Upsert

`PUT` routes the new bytes normally (best fit), uploads, then deletes the older
version of the same name. It creates the file if it did not exist. If the old
version cannot be deleted, the newly uploaded version is deleted again and the
job fails, leaving the original file unchanged:

```bash
curl -sS -X PUT http://127.0.0.1:8000/api/files/report.pdf \
  -H 'If-Match: "previous-etag"' \
  -F 'file=@report-v2.pdf;type=application/pdf'
```

`If-Match` is optional but strongly recommended when a UI or script read the
old metadata. Use the opaque `etag` from the detail response (or the `ETag`
response header); it is not the B2 `file_id`. A mismatch is `412
precondition_failed` before any upload is spooled, and the worker rechecks the
same ETag before replacing. `If-Match: *` requires the file to exist (use it to
avoid accidental creates).

### Download and ranges

```bash
curl -sS -D - http://127.0.0.1:8000/api/download/report.pdf \
  -H 'Range: bytes=0-1023'
```

`200` includes `ETag: "<opaque-etag>"` and `Accept-Ranges: bytes`; `206` includes
`Content-Range`; `304` is returned for a matching `If-None-Match`; `416` is a
problem document. File detail and content return the same opaque ETag, and
delete/upsert `If-Match` preconditions accept it, so clients never need to mix
`file_id` and metadata generations. Downloads stream from B2 in 64 KiB chunks
and never buffer the whole object; if B2 fails mid-stream the connection is
aborted rather than returning a silently truncated `200`.

### Routing plans

Preview the pool's choice without reserving anything:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/routing-plans \
  -H 'Content-Type: application/json' \
  -d '{"operation":"upload","files":[{"name":"a.bin","size":1048576}]}'
```

The response separates `routable` from `unroutable` with stable reasons
(`duplicate_filename`, `duplicate_in_batch`, `no_account_can_fit`) and includes
the remaining bytes/slots the decision would leave. `"operation":"upsert"`
allows replacing existing names. Routing is **best fit by remaining bytes**,
with a stable account-name tiebreak; it accounts for in-flight reservations.

## Semantics worth knowing

### Conflict matrix

| Situation | Response |
| --- | --- |
| `POST` a name that exists | `409 duplicate_filename` |
| Any upload/upsert/delete while a job or delete owns that name | `409 job_active` |
| `DELETE` a missing name | `404 file_not_found` |
| `If-Match` differs from the current file `etag` | `412 precondition_failed` |
| Queue is full | `429 queue_full` with `Retry-After`, checked before the multipart body is parsed/spooled |
| Same name twice in one batch | Per-file `409 duplicate_in_request`; `202` if another part was accepted, otherwise the batch problem |

B2 is never mutated until a name is exclusively claimed. The claim is released
in a `finally` block, so a crashed worker cannot permanently freeze a name.

### Caching and restarts

The cache is rebuilt at boot from every account's latest version. Uploads,
deletes, and upserts mutate it at runtime through the API or console; both use
the same services. Account capacity is recomputed from B2 on restart. A job that was
mid-transfer when the process died leaves a spool file (cleaned at next start)
and no cache entry; B2 may or may not contain the object. If it does, the next
boot adopts it. Startup rejects the whole pool if two accounts report the same
name, because that would make downloads ambiguous:

```
$ b2router
{"event":"startup.account_failed","issue":"File 'x' exists in accounts ..."}
```

Stop the gateway and run the reconciler with `--apply` to keep the newest copy.

### Reconciliation

A single account should contain at most one visible upload version of a given
file name. The router rolls back a new upsert version when the old-version
delete fails. If both deletes fail, or the process dies between the upload and
the old-version delete, B2 can still contain two versions. A latest-only
listing cannot see the older copy, but its bytes still consume the account.
Startup lists all visible versions and refuses to build the cache for that
account if a name has more than one upload version, so run the reconciler.
`b2router-reconcile` lists *all* upload versions, reports groups with more than
one copy, and can delete everything except the newest:

```bash
B2ROUTER_ACCOUNTS=/etc/b2router/accounts.yaml b2router-reconcile --pretty
B2ROUTER_ACCOUNTS=/etc/b2router/accounts.yaml b2router-reconcile --apply --pretty
```

It takes the same `flock` as the server, so `--apply` refuses to run while the
gateway is live. The `b2router-create-file` and `b2router-delete-file`
maintenance commands also take the writer lock and exit with status `3` when
the server is running. Run mutating tools only with the gateway stopped. Without `--apply`
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
- Upload filenames are normalised, length-checked, and rejected if they contain
  NULs, backslashes, `.`/`..` segments, control characters, or a leading `/`.
  The spool directory is mode `0700`, and every spool file is `0600`. Spooling
  refuses to start with less than 1 MiB free and converts disk write failures
  into `503 service_unavailable` instead of leaking a server error.
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
routing/reservations, conflict handling, async jobs, ambiguous-upload recovery,
RFC 9457 shapes, auth, rate limiting, CORS, ranges/ETags, config/settings
validation, and the UI's sign-in, CSRF, fragments, uploads, deletes, and
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
volume. Mount `accounts.yaml` read-only at `/config/accounts.yaml`.
Compose publishes `127.0.0.1:8000:8000` by default. Exposing the port on a
remote interface requires an explicit `B2ROUTER_API_TOKEN` and a deliberate
port/network change; without the token the console and API are unauthenticated.
