"""TypedDict definitions for stable API response shapes.

These types document the JSON contracts and enable precise type checking
in route handlers and tests. They are not used for runtime validation.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class FileEntryDict(TypedDict):
    file_name: str
    file_id: str
    etag: str
    size: int
    content_type: str
    upload_timestamp: int
    uploaded_at: str
    action: Literal["upload", "hide"]
    accounts: list[str]
    account_count: int


class AccountStateDict(TypedDict):
    name: str
    capacity_bytes: int | None
    used_bytes: int
    rem_bytes: int | None
    remaining_bytes: int | None
    file_count: int
    max_files: int
    remaining_files: int
    utilization: float
    rate_limited_until: float


class PoolStatsDict(TypedDict):
    files: int
    size_bytes: int
    accounts: int
    capacity_bytes: int | None
    used_bytes: int
    free_bytes: int | None
    reserved_bytes: int
    reserved_files: int
    revision: int


class RoutingDecisionDict(TypedDict):
    file_name: str
    file_size: int
    account_name: str
    remaining_bytes: int | None
    remaining_files: int
    source_account: str | None
    replaces: bool


class RoutingFailureDict(TypedDict):
    file_name: str
    file_size: int
    reason: Literal["no_account_can_fit", "no_replacement_account", "duplicate_in_batch"]
    message: str
    source_account: str | None


class RoutingPlanDict(TypedDict):
    routable: list[RoutingDecisionDict]
    unroutable: list[RoutingFailureDict]
    all_routable: bool
    revision: int


class JobSnapshotDict(TypedDict):
    id: str
    operation: Literal["upsert"]
    file_name: str
    file_size: int
    content_type: str
    account_name: str | None
    state: Literal["queued", "uploading", "replacing", "complete", "partial", "failed", "cancelled"]
    progress: float
    bytes_sent: int
    error: NotRequired[dict[str, Any] | None]
    result: NotRequired[dict[str, Any] | None]
    replaced: NotRequired[dict[str, Any] | None]
    created_at: str
    started_at: NotRequired[str | None]
    finished_at: NotRequired[str | None]
    request_id: NotRequired[str | None]
    client: NotRequired[str | None]
    seq: int


class AuditEntryDict(TypedDict):
    id: str
    timestamp: str
    action: Literal["upsert", "delete"]
    result: Literal["success", "partial", "failed", "cancelled"]
    file_name: str
    account_name: str | None
    file_size: int
    error_code: str | None
    request_id: str | None
    client: str | None
    duration_ms: float
    details: dict[str, Any]


class ProblemDetailDict(TypedDict):
    type: str
    title: str
    status: int
    detail: NotRequired[str]
    instance: NotRequired[str]
    code: str
    request_id: str


class HealthResponseDict(TypedDict):
    status: Literal["ok"]
    version: str
    started_at: str
    uptime_seconds: float
    pool_revision: int
    jobs_revision: int


class ReadyResponseDict(TypedDict):
    status: Literal["ready"]
    checks: dict[str, bool]
    accounts: int
    files: int
    pool_revision: int


class ServiceInfoDict(TypedDict):
    service: Literal["b2router"]
    version: str
    api: dict[str, str]


class StatsResponseDict(TypedDict):
    items: list[FileEntryDict]
    total: int
    limit: int
    next_cursor: str | None
    revision: int


class FilesListResponseDict(TypedDict):
    items: list[FileEntryDict]
    total: int
    limit: int
    next_cursor: str | None
    revision: int


class FileDetailResponseDict(FileEntryDict):
    pass


class RoutingPlanResponseDict(RoutingPlanDict):
    generated_at: str


class JobsListResponseDict(TypedDict):
    items: list[JobSnapshotDict]
    total: int
    active: int
    queued: int
    revision: int


class JobDetailResponseDict(JobSnapshotDict):
    pass


class JobCancelResponseDict(JobSnapshotDict):
    pass


class AuditListResponseDict(TypedDict):
    items: list[AuditEntryDict]
    total: int
    limit: int
    offset: int


class UpsertResponseDict(TypedDict):
    jobs: list[JobSnapshotDict]
    rejected: list[dict[str, Any]]


class DeleteResponseDict(TypedDict):
    pass  # 204 No Content


class ContentResponseDict(TypedDict):
    pass  # Streaming response
