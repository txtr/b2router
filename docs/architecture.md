# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            b2router Gateway                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        HTTP Layer (Flask)                            │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │    │
│  │  │ /health  │ │ /ready   │ │ /metrics │ │ /api/*   │ │ /ui/*    │  │    │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                        │
│  ┌─────────────────────────────────▼─────────────────────────────────────┐  │
│  │                      Middleware (web.py)                              │  │
│  │  Request ID │ Auth │ Rate Limit │ CORS │ Queue Check │ Space Check   │  │
│  └─────────────────────────────────┬─────────────────────────────────────┘  │
│                                    │                                        │
│  ┌─────────────────────────────────▼─────────────────────────────────────┐  │
│  │                      Service Layer                                    │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │  │
│  │  │ UploadManager│  │ AccountPool │  │  AuditLog   │  │   Lock      │  │  │
│  │  │ (jobs/workers)│  │ (routing)   │  │ (mutations) │  │  (flock)    │  │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  │  │
│  └─────────────────────────────────┬─────────────────────────────────────┘  │
│                                    │                                        │
│  ┌─────────────────────────────────▼─────────────────────────────────────┐  │
│  │                      Storage Abstraction (account.py)                 │  │
│  │  StorageBackend Protocol ◄─── AccountClient (b2sdk wrapper)           │  │
│  └─────────────────────────────────┬─────────────────────────────────────┘  │
│                                    │                                        │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
             ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
             │  B2 Account │  │  B2 Account │  │  B2 Account │
             │     A       │  │     B       │  │     C       │
             └─────────────┘  └─────────────┘  └─────────────┘
```

## Component Responsibilities

| Component | Responsibility | Key Files |
|-----------|----------------|-----------|
| **Flask App** | HTTP routing, middleware, error handling | `web.py`, `server.py` |
| **UploadManager** | Job lifecycle, worker pool, spool management | `services/uploads.py` |
| **AccountPool** | Routing decisions, reservations, cache, commits | `services/pool.py` |
| **AccountClient** | B2 SDK wrapper, auth, retries, error translation | `services/account.py` |
| **AuditLog** | Mutation ring buffer | `services/audit.py` |
| **InstanceLock** | Single-writer enforcement | `lockfile.py` |

## Data Flow: Upsert Request

```
POST /api/files (multipart)
        │
        ▼
┌───────────────────────┐
│ Middleware (web.py)   │
│ - Request ID          │
│ - Auth (bearer)       │
│ - Rate limit (IP)     │
│ - Queue check         │
│ - Space check (507)   │
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ Spool to temp file    │
│ (0600, private dir)   │
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ UploadManager.submit()│
│ - Create UploadJob    │
│ - Claim name lock     │
│ - Enqueue to executor │
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ Worker (_run)         │
│ - Reserve (atomic)    │
│ - Upload to B2        │
│ - Delete old (replace)│
│ - Commit or partial   │
└───────────┬───────────┘
            ▼
┌───────────────────────┐
│ Release reservation   │
│ Release name lock     │
│ Cleanup temp file     │
└───────────┬───────────┘
            ▼
       202 Accepted
       {jobs: [{id, state}]}
```

## Concurrency Model

| Resource | Protection | Granularity |
|----------|------------|-------------|
| AccountPool state | `RLock` | Entire pool |
| Per-file name | `_active_names` dict + lock | Per-file |
| Job registry | `RLock` + per-job `Lock` | Per-job |
| Writer process | `fcntl.flock` | Process-wide |
| B2 operations | None (sequential per worker) | Per-upload |

## State Machines

### UploadJob States

```
QUEUED ──► UPLOADING ──► REPLACING ──► COMPLETE
   │           │            │
   │           │            └──► PARTIAL (old delete failed)
   │           │
   │           └──► PARTIAL (size mismatch / commit failed)
   │
   └──► CANCELLED (via DELETE /api/jobs/{id})
```

### Terminal States: `COMPLETE`, `PARTIAL`, `FAILED`, `CANCELLED`

## Startup Sequence

```
main()
  │
  ├─► Settings.from_env()     # Validate all env vars
  ├─► InstanceLock.acquire()  # Fail fast if another writer
  ├─► ConfigReader.read()     # Parse accounts.yaml
  ├─► AccountPool.load_accounts()
  │     ├─► authorize each account
  │     ├─► list_files() + list_versions()
  │     ├─► Detect duplicates (startup fail)
  │     └─► Build cache + accounting
  ├─► UploadManager(pool, audit, settings)
  ├─► create_app()            # Flask + routes
  ├─► cleanup_temp_dir()      # Remove stale spool files
  └─► app.run()               # Block until SIGTERM/KeyboardInterrupt
```

## Shutdown Sequence

```
SIGTERM → KeyboardInterrupt
         │
         ▼
manager.shutdown()
  ├─ _closed = True
  ├─ Cancel queued jobs (cleanup temp)
  ├─ Wait for active uploads (executor.shutdown)
  ├─ Wait for in-flight deletes (_delete_done)
  └─ Clear _active_names, _futures
         │
         ▼
lock.release()    # In finally block (nested)
```

## Configuration Sources

| Source | Priority | Example |
|--------|----------|---------|
| Defaults | 1 | `host="127.0.0.1"` |
| `accounts.yaml` | 2 | `max_files_per_account: 1700` |
| Environment | 3 | `B2ROUTER_HOST=0.0.0.0` |
| CLI args | 4 | `--port 9000` |

Invalid values are aggregated and reported together at startup.