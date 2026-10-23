# Scaling Limits & Performance

## Hard Limits

| Dimension | Limit | Source |
|-----------|-------|--------|
| Max file size | 10 GiB | `B2ROUTER_MAX_UPLOAD_BYTES` (default) |
| Max filename | 1000 bytes | `B2ROUTER_MAX_FILENAME_BYTES` |
| Max files per request | 20 | `B2ROUTER_MAX_FILES_PER_REQUEST` |
| Max accounts | ~1000 | Memory, `max_files_per_account` |
| Max files per account | 1700 | B2 bucket object limit |
| Max job history | 500 | `B2ROUTER_JOB_HISTORY_MAX` |
| Max audit log | 1000 | `max(job_history_max, 1000)` |
| Concurrent uploads | 2 | `B2ROUTER_UPLOAD_WORKERS` |
| Queued jobs | 100 | `B2ROUTER_QUEUE_MAX` |
| Mutation rate | 240/min/IP | `B2ROUTER_MUTATION_RATE_PER_MINUTE` |

## Theoretical Maximums

| Metric | Value | Notes |
|--------|-------|-------|
| Total files (10 accounts × 1700) | 17,000 | B2 limit |
| Total capacity (10 × 10 GiB) | 100 GiB | Soft limit |
| Concurrent uploads | 2 | Single-process, 1 worker per upload |
| Request throughput | ~500 req/s | Flask dev server, simple routes |

## Memory Profile

| Component | Est. Memory |
|-----------|-------------|
| Base Flask | ~50 MiB |
| AccountPool (17K files) | ~15 MiB |
| UploadManager (100 jobs) | ~5 MiB |
| AuditLog (1000 entries) | ~2 MiB |
| B2 SDK cache | ~10 MiB |
| **Total** | **~80 MiB** |

## Performance Characteristics

| Operation | Latency (P99) | Bottleneck |
|-----------|---------------|------------|
| `GET /health` | < 5 ms | Flask routing |
| `GET /api/stats` | < 10 ms | Pool lock |
| `GET /api/files` (100) | < 20 ms | Cache iteration |
| `POST /api/files` (1 file) | 50–200 ms | Spool + enqueue |
| Upload to B2 (100 MiB) | 2–10 s | Network + B2 |
| Replace (100 MiB) | 3–15 s | Upload + delete old |
| `DELETE /api/files` | 100–500 ms | B2 delete |

## Scaling Strategies

| Strategy | Feasibility | Notes |
|----------|-------------|-------|
| Increase `UPLOAD_WORKERS` | ⚠️ Limited | Flask dev server + GIL; >4 workers = diminishing returns |
| Increase `QUEUE_MAX` | ✅ Easy | Memory linear |
| Add accounts | ✅ Primary | Linear capacity/slot scaling |
| Increase `max_files_per_account` | ❌ No | B2 hard limit (1700) |
| Run multiple gateways | ❌ No | `flock` prevents; single-writer design |

## Known Non-Goals

| Feature | Status | Alternative |
|---------|--------|-------------|
| Horizontal scaling | ❌ Won't fix | Single-process design |
| Multi-region | ❌ Won't fix | Single B2 endpoint |
| S3 API compatibility | ❌ Won't fix | Custom JSON API |
| Streaming uploads >10 GiB | ❌ Won't fix | Chunked not supported |

## Benchmark Baseline (Local SSD, 1 Gbps LAN)

| File Size | Upload Time | Throughput |
|-----------|-------------|------------|
| 1 MiB | 0.3 s | ~3 MiB/s |
| 100 MiB | 2.5 s | ~40 MiB/s |
| 1 GiB | 18 s | ~55 MiB/s |
| 10 GiB | 180 s | ~55 MiB/s |

*B2 throughput varies by region, time of day, and account tier.*
