# Failure Modes & Handling

This document catalogs every identified failure mode, its detection, and the gateway's response.

## Classification

| Category | Scope | Recovery |
|----------|-------|----------|
| **Transient** | Network, B2 5xx | Automatic retry |
| **Permanent** | Config, auth, capacity | Manual intervention |
| **Ambiguous** | Timeout after B2 accept | `partial` job + reconciliation |
| **Catastrophic** | Disk full, lock contention | Process exit |

---

## 1. Network / B2 Failures

### B2 Unavailable (503/504)

| Trigger | Detection | Response |
|---------|-----------|----------|
| Connection timeout | `requests.Timeout` | Retry (3×, exponential backoff) |
| Read timeout | `b2exc.B2RequestTimeout` | Retry (3×) |
| 5xx from B2 | `b2exc.ServiceError` | Retry (3×) |
| DNS failure | `requests.ConnectionError` | Retry (3×) |

**Retry policy**: 3 attempts, `0.2s × 2^attempt` (account init), `0.5s × 2^attempt` (delete/rollback)

### B2 Rate Limited (429)

| Trigger | Detection | Response |
|---------|-----------|----------|
| HTTP 429 | `b2exc.TooManyRequests` | **Worker-side**: mark account 60s, release reservation, re-route to next account |
| Transaction cap | `b2exc.CapExceeded` | Same as above |
| Bucket transaction cap | `b2exc.TransactionCapExceeded` | Same as above |

**Max retries**: Number of accounts in pool (tries each once)

### B2 Auth Failure (401/403)

| Trigger | Detection | Response |
|---------|-----------|----------|
| Invalid key | `b2exc.Unauthorized` | `b2_unauthorized` (502), no retry |
| Expired token | `b2exc.InvalidAuthToken` | Same |
| Missing capability | `b2exc.NotAllowedByAppKeyError` | Same |

---

## 2. Storage / Disk Failures

### Staging Volume Full

| Trigger | Detection | Response |
|---------|-----------|----------|
| `Content-Length` > free space | `ensure_upload_space()` in middleware | `507 insufficient_storage` **before** multipart parse |
| Write during spool | `OSError` (ENOSPC) | `503 service_unavailable`, cleanup temp |
| < 1 MiB free | `shutil.disk_usage()` in `spool_upload()` | `503 service_unavailable` |

**No bytes accepted** when 507 returned.

### Temp File Cleanup Failure

| Trigger | Detection | Response |
|---------|-----------|----------|
| `os.unlink()` fails | `OSError` in `_cleanup_temp()` | Log `upload.temp_cleanup_failed` (WARNING), continue |

### Lock File Contention

| Trigger | Detection | Response |
|---------|-----------|----------|
| Another writer holds lock | `fcntl.flock()` EAGAIN | Exit 3 (`LockError`), log `startup.lock_failed` |

---

## 3. B2 Semantic Failures

### Object Not Found

| Trigger | Detection | Response |
|---------|-----------|----------|
| Download missing | `b2exc.FileNotPresent` | `b2_not_found` (502) |
| Delete missing | `b2exc.FileAlreadyHidden` | Treat as success (idempotent) |
| Reconcile missing | `list_versions()` gap | Skip, report in `failed` |

### Range Not Satisfiable

| Trigger | Detection | Response |
|---------|-----------|----------|
| Invalid `Range` header | `b2exc.InvalidRange` | `range_not_satisfiable` (416) |
| Object size mismatch | `b2exc.UnsatisfiableRange` | Same |

### Size Mismatch on Upload

| Trigger | Detection | Response |
|---------|-----------|----------|
| B2 stored ≠ spooled bytes | `remote_version.size != job.file_size` | **Try delete bad version** → if succeeds: `failed` (`upload_failed`); if fails: `partial` (`upload_cleanup_failed`) |

---

## 4. Concurrency Conflicts

### Duplicate Name in Batch

| Trigger | Detection | Response |
|---------|-----------|----------|
| Same filename twice | `_plan()` sees duplicate | `duplicate_in_request` (409), per-file |

### Concurrent Upsert/Delete Same Name

| Trigger | Detection | Response |
|---------|-----------|----------|
| Name in `_active_names` | `_blocking_name_locked()` | `job_active` (409) |
| Name in `_deleting` | `begin_delete()` check | `job_active` (409) |

### Race: Upsert vs Delete

| Scenario | Resolution |
|----------|------------|
| Upsert wins name lock | Delete gets `job_active` |
| Delete wins name lock | Upsert gets `job_active` |
| Both check simultaneously | `RLock` serializes; one wins |

---

## 5. Replacement-Specific Failures

### No Replacement Account

| Trigger | Detection | Response |
|---------|-----------|----------|
| File exists, no other account has slot+bytes | `_plan()` returns `NO_REPLACEMENT_ACCOUNT` | `no_replacement_account` (409) **before any upload** |

### Old Version Delete Fails

| Trigger | Detection | Response |
|---------|-----------|----------|
| B2 delete returns error | `_delete_old()` retries 3× | → `_old_version_exists()` check |
| Old version still exists | `_old_version_exists()` returns `True` | → `_rollback_new_version()` (3 retries) |
| Rollback succeeds | `_rollback_new_version()` returns `True` | `failed` (`old_version_retained`) |
| Rollback fails | `_rollback_new_version()` returns `False` | `partial` (`old_version_retained` + `rollback_failed: true`) |
| Old version gone | `_old_version_exists()` returns `False` | Treat as deleted, commit normally |

### Ambiguous Delete Timeout

| Trigger | Detection | Response |
|---------|-----------|----------|
| Delete times out (response lost) | `_delete_old()` all retries fail | `_old_version_exists()` queries B2 |
| B2 query fails | `_old_version_exists()` returns `None` | `partial` (both copies may exist) |

---

## 6. Ambiguous Outcomes → Partial Jobs

| Partial Code | Cause | Resolution |
|--------------|-------|------------|
| `upload_outcome_unknown` | Upload timeout after B2 may have accepted | Reconcile `--apply` (marker + size match) |
| `old_version_retained` | Delete failed + rollback failed | Reconcile `--apply` (keep newest) |
| `upload_cleanup_failed` | Size mismatch + cleanup failed | Reconcile `--apply` (delete bad version) |
| `commit_failed` | Bytes on B2 but cache commit failed | Reconcile `--apply` (rebuild cache) |

**Never reported as successful**. Client must poll job, see `partial`, run reconcile.

---

## 7. Startup Failures (Hard Stops)

| Failure | Exit Code | Resolution |
|---------|-----------|------------|
| Config file missing | 2 | Provide `accounts.yaml` |
| Invalid YAML | 2 | Fix syntax |
| Duplicate account_id | 4 | Unique keys in config |
| Duplicate filename across accounts | 4 | Run `reconcile --apply` |
| Multiple upload versions in one account | 4 | Run `reconcile --apply` |
| Account authorize fails | 4 | Fix credentials |
| Lock held by another process | 3 | Stop other gateway |

**No partial startup** — pool fully built before HTTP binds.

---

## 8. Shutdown / Process Death

| Scenario | On Restart |
|----------|------------|
| Job queued | Cancelled, temp cleaned, `cancelled` state |
| Job uploading | Wait for completion (1h Docker grace), then commit/partial |
| Job replacing | Wait for delete/rollback, then commit/partial |
| Delete in-flight | Wait for completion |
| Process killed mid-upload | Temp file remains (cleaned at next start), B2 may have object (adopted on next boot) |
| Process killed after upload, before delete | Two copies in different accounts → startup refuses, reconcile required |

---

## 9. Reconciliation Scenarios

| Scenario | Detection | `--apply` Action |
|----------|-----------|------------------|
| Same name in multiple accounts | Startup listing | Keep newest, delete others |
| Multiple upload versions in one account | `list_versions()` > 1 | Keep newest, delete older |
| Account unreadable | `list_versions()` fails | **Refuse** `--apply` (may hold real keeper) |
| `--apply` with gateway running | `flock` conflict | Exit 3 |

---

## 10. Observability for Failures

| Signal | Where |
|--------|-------|
| Structured logs | JSON (`log_event`) with `error_code`, `duration_ms` |
| Metrics | `b2router_b2_errors`, `b2router_uploads{result="failed\|partial"}` |
| Job state | `GET /api/jobs/{id}` → `state`, `error.code`, `error.details` |
| Audit trail | `GET /api/audit` → `error_code`, `result` |
| Request correlation | `X-Request-ID` header + log field |

---

## Summary Table

| Failure Type | HTTP Code | Job State | Auto-Recover | Manual Action |
|--------------|-----------|-----------|--------------|---------------|
| B2 transient | 503/504 | — | ✅ (retry) | — |
| B2 rate limit | 503 | — | ✅ (re-route) | — |
| B2 auth | 502 | — | ❌ | Fix credentials |
| Disk full (pre-check) | 507 | — | ❌ | Free space |
| Disk full (write) | 503 | — | ❌ | Free space |
| Size mismatch | — | `failed`/`partial` | ✅ (cleanup) | Reconcile if partial |
| No replacement account | 409 | `failed` | ❌ | Add capacity/account |
| Concurrent mutation | 409 | — | ✅ (retry) | — |
| Ambiguous delete | — | `partial` | ❌ | Reconcile |
| Lock contention | Exit 3 | — | ❌ | Stop other process |
