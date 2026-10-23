# Glossary

## Core Terms

| Term | Definition |
|------|------------|
| **Upsert** | Create-or-replace operation. `POST /api/files` with a filename that may or may not exist. If it exists, stages replacement in a different account. |
| **Reservation** | Atomic claim on bytes + file slot in a specific account. Created by `AccountPool.reserve()`, released in `finally` block. |
| **Partial** | Job state where the router cannot prove final B2 state. Four codes: `upload_outcome_unknown`, `old_version_retained`, `upload_cleanup_failed`, `commit_failed`. |
| **Reconciliation** | Process of detecting and resolving duplicate file versions across accounts. Run via `b2router-reconcile --apply`. |
| **Spool** | Private temp file (`0600` in `0700` dir) holding upload bytes before B2 transfer. |
| **Flock** | `fcntl.flock()` advisory lock on `B2ROUTER_LOCK_FILE`. Enforces single-writer process. |
| **Account** | One B2 application key + bucket. Display name: `account-<first 8 chars of key_id>`. |
| **Capacity** | Soft local budget (`capacity_in_mb` from config). B2 remains authoritative. |
| **Slot** | File count budget (`max_files_per_account`, default 1700). Counts all upload versions. |
| **Job** | Async upsert operation with states: `queued` → `uploading` → `replacing` → `complete\|partial\|failed\|cancelled`. |
| **Marker** | `b2router_job_id` in B2 `fileInfo`. Used for ambiguous upload recovery. |
| **ETag** | Opaque validator: `SHA256("file\0{file_id}\0{upload_timestamp}")[:20]`. Used for `If-Match`/`If-None-Match`. |
| **Partial** | Job state where final B2 state is unproven. Four subcodes. Never reported as success. |
| **Flock** | Process-level advisory lock (`fcntl.flock`). Single-writer enforcement. |
| **Spool** | Temp file staging upload bytes. `0600` perms, `0700` dir. |
| **Reservation** | Atomic hold on bytes+slot in an account. Created by `reserve()`, released in `finally`. |
| **Partial** | Job state: final B2 state unproven. Four subcodes. Never "success". |
| **Reconciliation** | Cross-account duplicate detection + cleanup. `b2router-reconcile --apply`. |

---

## API Terms

| Term | Meaning |
|------|---------|
| **202 Accepted** | Upsert enqueued; poll `/api/jobs/{id}` for result. |
| **If-Match** | Strong ETag comparison (rejects weak `W/"..."`). |
| **If-None-Match** | Weak ETag comparison (for caching). |
| **Range** | Byte-range request (`bytes=0-1023`). Returns `206` + `Content-Range`. |
| **Problem** | RFC 9457 error document (`application/problem+json`). |
| **Queue Full** | `429` before multipart parse; `Retry-After: 5`. |
| **Partial** | Job state; inspect `GET /api/jobs/{id}` for `error.code`. |
| **Reconcile** | Run `b2router-reconcile --apply` with gateway stopped. |

---

## CLI Tools

| Tool | Purpose |
|------|---------|
| `b2router` | Start gateway (daemon). |
| `b2router-reconcile [--apply] [--pretty]` | Detect/fix duplicates. `--apply` deletes stale. |
| `b2router-create-file <account> <name> (--file\|--size-mb)` | Direct upload (debug). |
| `b2router-delete-file <account> <name>` | Direct delete (debug). |

---

## State Machines

### Job States

```
QUEUED → UPLOADING → REPLACING → COMPLETE
              │             │
              │             └── PARTIAL (delete failed)
              │
              └── PARTIAL (size mismatch / commit failed)
              │
              └── FAILED (no replacement account / rollback)
              │
              └── CANCELLED (DELETE /api/jobs/{id} while queued)
```

Terminal: `COMPLETE`, `PARTIAL`, `FAILED`, `CANCELLED`

### Pool Revisions

| Revision | Incremented On |
|----------|----------------|
| Pool | Cache mutation (commit, delete, reserve, release) |
| Jobs | Job state transition, dismissal |

---

## Error Codes (RFC 9457)

| Code | HTTP | Context |
|------|------|---------|
| `bad_request` | 400 | Malformed request |
| `validation_error` | 400 | Field/filename invalid |
| `unauthorized` | 401 | Missing/invalid bearer |
| `forbidden` | 403 | CORS/origin denied |
| `not_found` | 404 | Route missing |
| `file_not_found` | 404 | File not in cache |
| `method_not_allowed` | 405 | Wrong method |
| `duplicate_in_request` | 409 | Same name twice |
| `job_active` | 409 | Name locked |
| `file_changed` | 409 | ETag mismatch |
| `state_changed` | 409 | Pool invariant changed |
| `no_account_can_fit` | 409 | No space/slots |
| `no_replacement_account` | 409 | Replace needs other account |
| `precondition_failed` | 412 | Weak/strong ETag mismatch |
| `payload_too_large` | 413 | Exceeds limit |
| `range_not_satisfiable` | 416 | Bad byte range |
| `queue_full` | 429 | Bounded queue full |
| `too_many_requests` | 429 | Rate limit |
| `insufficient_storage` | 507 | Staging volume full |
| `b2_error` | 502 | Generic B2 failure |
| `b2_unauthorized` | 502 | Bad credentials |
| `b2_not_found` | 502 | Object missing |
| `b2_rate_limited` | 503 | B2 429 / cap |
| `b2_unavailable` | 503 | B2 transient |
| `b2_timeout` | 504 | B2 read timeout |
| `not_ready` | 503 | Startup incomplete |
| `service_unavailable` | 503 | Shutdown / deps down |

---

## Job Error Codes

| Code | State | Meaning |
|------|-------|---------|
| `old_version_retained` | `failed` | Replace rolled back; original intact |
| `no_replacement_account` | `failed` | No other account fits |
| `upload_outcome_unknown` | `partial` | Timeout after B2 accept |
| `old_version_retained` | `partial` | Delete + rollback both failed |
| `upload_cleanup_failed` | `partial` | Size mismatch + cleanup failed |
| `commit_failed` | `partial` | B2 ok, cache commit failed |

---

## Metrics Labels

| Metric | Labels |
|--------|--------|
| `b2router_requests` | `method`, `route`, `status` |
| `b2router_b2_errors` | `operation`, `code` |
| `b2router_uploads` | `result` (success/partial/failed) |
| `b2router_routing_failures` | `reason` |

---

## Environment Variables

| Variable | Default | Scope |
|----------|---------|-------|
| `B2ROUTER_HOST` | `127.0.0.1` | Bind |
| `B2ROUTER_PORT` | `8000` | Port |
| `B2ROUTER_MAX_UPLOAD_BYTES` | `10737418240` | 10 GiB |
| `B2ROUTER_UPLOAD_WORKERS` | `2` | Concurrency |
| `B2ROUTER_QUEUE_MAX` | `100` | Backpressure |
| `B2ROUTER_API_TOKEN` | unset | Auth |
| `B2ROUTER_TRUST_PROXY` | `0` | X-Forwarded-For |

---

## File Permissions

| Path | Mode |
|------|------|
| `accounts.yaml` | `0600` |
| Spool dir | `0700` |
| Spool file | `0600` |
| Lock file | `0644` (owned by gateway user) |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Config/settings error |
| 2 | Config file/YAML error |
| 3 | Lock held by another process |
| 4 | Startup validation failure |
