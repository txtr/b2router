# Problem catalogue

Every failure from the HTTP API is an [RFC 9457][rfc9457] problem document
served as `application/problem+json`. The same catalogue is used by the UI and
by API clients; clients should branch on the stable `code` extension member,
not on prose.

```json
{
  "type": "https://github.com/txtr/b2router/blob/main/docs/problems.md#duplicate_filename",
  "title": "Duplicate file name",
  "status": 409,
  "detail": "File 'reports/q3.pdf' already exists in the pool. Use PUT /api/files/reports/q3.pdf to replace it.",
  "code": "duplicate_filename",
  "request_id": "req_7b0b5c8f2b6d4a6aa8c8f9d5a3f0b1e2",
  "errors": [
    {
      "file_name": "reports/q3.pdf",
      "error": {
        "code": "duplicate_filename",
        "message": "File 'reports/q3.pdf' already exists in the pool."
      }
    }
  ]
}
```

Common members:

| Member | Meaning |
| --- | --- |
| `type` | Stable documentation URL; the fragment is the problem `code`. |
| `title` | Short, human-readable summary. |
| `status` | HTTP status code. |
| `detail` | Human-readable explanation for this occurrence. |
| `instance` | Request path, when useful. |
| `code` | Stable machine-readable identifier listed below. |
| `request_id` | Correlation id; also returned in `X-Request-ID` and logged. |
| `errors` | Optional per-item validation errors for batch requests. |

`errors[]` entries have the shape `{ "file_name": <field or file name>,
"error": { "code": ..., "message": ..., "details": {...} } }`. A batch upload
that rejects every part reports the specific problem when all rejections share
one code (for example `409 duplicate_filename` or `413 payload_too_large`);
mixed rejections fall back to `400 bad_request`.

## Catalogue

### Request problems

| `code` | Status | Meaning | What to do |
| --- | --- | --- | --- |
| `bad_request` | 400 | The request could not be parsed or a required part is missing. | Fix the request shape. |
| `validation_error` | 400 | One or more fields or filenames are invalid. Inspect `errors`. | Correct the listed values. |
| `unauthorized` | 401 | A bearer token is required or does not match. | Send `Authorization: Bearer <token>`. |
| `forbidden` | 403 | The origin or operation is not allowed. | Check the CORS allowlist and token. |
| `not_found` | 404 | No route matches the request. | Check the path; the UI lives under `/ui`, the API under `/api`. |
| `method_not_allowed` | 405 | The route exists but not for this HTTP method. | Use the method in `Allow`. |
| `unsupported_media_type` | 415 | A JSON body or upload was expected. | Send `Content-Type: application/json` or `multipart/form-data`. |
| `payload_too_large` | 413 | The body exceeds the configured limit. | Upload a smaller file or raise `B2ROUTER_MAX_UPLOAD_BYTES`. |
| `range_not_satisfiable` | 416 | The requested byte range is outside the object. | Request a valid range; inspect `Content-Range` on `206`. |
| `too_many_requests` | 429 | The per-client mutation budget was exhausted. | Wait for `Retry-After` seconds. |
| `internal_error` | 500 | An unhandled error occurred. The `request_id` is in the logs. | Report with the `request_id`. |

### File and routing conflicts

| `code` | Status | Meaning | What to do |
| --- | --- | --- | --- |
| `file_not_found` | 404 | The named file is not in the cache, or its latest version disappeared from B2. | Refresh the file list. |
| `duplicate_filename` | 409 | `POST /api/files` cannot create an existing name. | Use `PUT /api/files/{name}` to replace it. |
| `duplicate_in_request` | 409 | The same name appears twice in one upload or routing request. | De-duplicate the request. |
| `job_active` | 409 | Another upload, upsert, delete, or cancel is using that name. | Poll the active job or retry after it finishes. |
| `file_changed` | 409 | The file's `file_id` changed between read and write. | Refresh, then retry with the new `If-Match`. |
| `state_changed` | 409 | A pool invariant changed while the operation was in flight. | Retry; the request is safe to repeat. |
| `no_account_can_fit` | 409 | No account has enough bytes, file slots, or the right capacity. | Free space, raise capacity, or use a smaller file. |
| `precondition_failed` | 412 | `If-Match` or another precondition did not match. | Re-read the resource and retry. |
| `queue_full` | 429 | The bounded upload queue is saturated. | Retry later; inspect `/api/jobs?state=queued`. |

### Job problems

| `code` | Status | Meaning |
| --- | --- | --- |
| `job_not_found` | 404 | The job id is unknown, pruned, or was dismissed. |
| `job_active` | 409 | A running job cannot be cancelled, or the job is already terminal. Dismiss terminal jobs instead. |

A job can itself finish in the `partial` state. That is not an HTTP problem:
the new bytes were stored, but the previous version could not be deleted. See
`GET /api/jobs/{id}`; the job carries `error.code = "old_version_retained"`
and the pool keeps the old bytes accounted for. Run `b2router-reconcile
--apply` with the gateway stopped before restarting, or the startup duplicate
check will refuse to build the cache.

### Storage backend problems

These are translated from `b2sdk` and never expose credentials or raw B2
payloads. They are retried where safe; a `b2_timeout`/`b2_unavailable` during
upload is reconciled by asking B2 whether the object landed.

| `code` | Status | Meaning |
| --- | --- | --- |
| `b2_error` | 502 | B2 rejected the operation. |
| `b2_unauthorized` | 502 | The application key is invalid or lacks a capability. |
| `b2_not_found` | 502 | The object or bucket disappeared. |
| `b2_rate_limited` | 503 | B2 returned `429` or a transaction cap error. |
| `b2_unavailable` | 503 | Connection or transient B2 failure. |
| `b2_timeout` | 504 | A B2 request exceeded the configured read timeout. |

### Availability

| `code` | Status | Meaning |
| --- | --- | --- |
| `not_ready` | 503 | Startup is incomplete; `/ready` is failing. |
| `service_unavailable` | 503 | The process is shutting down or a dependency is unavailable. |

## Idempotency and retries

* `POST /api/files` is **not** idempotent. A repeated request with the same name
  returns `duplicate_filename`; retrying a `202` means polling its job instead.
* `PUT /api/files/{name}` is idempotent for the same bytes. If the response is
  lost, retry the PUT; the final object is the same and older versions are
  removed.
* `DELETE /api/files/{name}` is idempotent from the client's point of view:
  deleting a missing name returns `file_not_found`, which can be treated as
  already deleted.
* `POST /api/routing-plans` has no side effects and is safe to retry.
* Every response carries `X-Request-ID`; supply your own `X-Request-ID` to
  correlate retries.

[rfc9457]: https://www.rfc-editor.org/rfc/rfc9457
