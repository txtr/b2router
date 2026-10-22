"""In-memory account pool: the routing and accounting brain of the gateway.

Responsibilities:
  * build the file cache once at startup from every account,
  * answer placement questions with best-fit packing,
  * reserve bytes/slots for in-flight uploads,
  * commit uploads, replacements and deletes atomically.

No B2 calls happen here. The pool depends only on ``StorageBackend`` and can be
exercised in tests with fake accounts.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from b2router.services.account import FileVersion, StorageBackend, StorageError

T = TypeVar("T")
E = TypeVar("E")


# ---------------------------------------------------------------------------
# Result types (kept for the config reader and CLI helpers)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Ok(Generic[T, E]):
    value: T


@dataclass(frozen=True)
class Err(Generic[T, E]):
    error: E


class PoolError(Exception):
    """Structured pool failure that maps onto an HTTP problem."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class PoolStartupError(Exception):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


# ---------------------------------------------------------------------------
# File cache
# ---------------------------------------------------------------------------
def iso_timestamp(upload_timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(upload_timestamp_ms / 1000, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def make_file_etag(file_id: str, upload_timestamp: int) -> str:
    """Return the opaque validator used by every file representation."""
    digest = hashlib.sha256(f"file\x00{file_id}\x00{upload_timestamp}".encode()).hexdigest()[:20]
    return digest


@dataclass(frozen=True)
class FileEntry:
    file_name: str
    file_id: str
    size: int
    content_type: str
    upload_timestamp: int
    action: str
    accounts: tuple[str, ...]

    @property
    def uploaded_at(self) -> str:
        return iso_timestamp(self.upload_timestamp)

    @property
    def etag(self) -> str:
        return make_file_etag(self.file_id, self.upload_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "file_id": self.file_id,
            "etag": self.etag,
            "size": self.size,
            "content_type": self.content_type,
            "upload_timestamp": self.upload_timestamp,
            "uploaded_at": self.uploaded_at,
            "action": self.action,
            "accounts": list(self.accounts),
            "account_count": len(self.accounts),
        }


FileEntryLike = FileEntry


@dataclass(frozen=True)
class AccountState:
    name: str
    capacity_bytes: int | None
    used_bytes: int = 0
    file_count: int = 0
    max_files: int = 1700

    @property
    def remaining_bytes(self) -> int | None:
        if self.capacity_bytes is None:
            return None
        return self.capacity_bytes - self.used_bytes

    @property
    def remaining_files(self) -> int:
        return self.max_files - self.file_count

    @property
    def utilization(self) -> float:
        if not self.capacity_bytes:
            return 0.0
        return self.used_bytes / self.capacity_bytes

    def can_fit(self, file_size: int) -> bool:
        if self.remaining_files <= 0:
            return False
        remaining = self.remaining_bytes
        return remaining is None or remaining >= file_size

    def with_file(self, file_size: int, file_slots: int = 1) -> AccountState:
        return replace(
            self,
            used_bytes=self.used_bytes + file_size,
            file_count=self.file_count + file_slots,
        )

    def with_max_files(self, max_files: int) -> AccountState:
        return replace(self, max_files=max_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capacity_bytes": self.capacity_bytes,
            "used_bytes": self.used_bytes,
            "rem_bytes": self.remaining_bytes,
            "remaining_bytes": self.remaining_bytes,
            "file_count": self.file_count,
            "max_files": self.max_files,
            "remaining_files": self.remaining_files,
            "utilization": round(self.utilization, 6),
        }


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reservation:
    id: str
    account_name: str
    file_name: str
    file_size: int
    file_slots: int = 1


# ---------------------------------------------------------------------------
# Routing (pure)
# ---------------------------------------------------------------------------
class RoutingFailureReason(StrEnum):
    DUPLICATE_FILENAME = "duplicate_filename"
    NO_ACCOUNT_CAN_FIT = "no_account_can_fit"
    DUPLICATE_IN_BATCH = "duplicate_in_batch"


@dataclass(frozen=True)
class RoutingDecision:
    file_name: str
    file_size: int
    account_name: str
    remaining_bytes: int | None
    remaining_files: int


@dataclass(frozen=True)
class RoutingFailure:
    file_name: str
    reason: RoutingFailureReason
    message: str


RoutingResult = Ok[RoutingDecision, RoutingFailure] | Err[RoutingDecision, RoutingFailure]


@dataclass(frozen=True)
class RoutingJob:
    file_name: str
    file_size: int
    decision: RoutingResult


@dataclass(frozen=True)
class RoutingPlan:
    jobs: list[RoutingJob]
    revision: int

    @property
    def all_routable(self) -> bool:
        return all(isinstance(job.decision, Ok) for job in self.jobs)

    @property
    def routable_jobs(self) -> list[RoutingJob]:
        return [job for job in self.jobs if isinstance(job.decision, Ok)]

    @property
    def unroutable_jobs(self) -> list[RoutingJob]:
        return [job for job in self.jobs if isinstance(job.decision, Err)]

    def to_dict(self) -> dict[str, Any]:
        routable: list[dict[str, Any]] = []
        unroutable: list[dict[str, Any]] = []
        for job in self.jobs:
            if isinstance(job.decision, Ok):
                decision = job.decision.value
                routable.append(
                    {
                        "file_name": decision.file_name,
                        "file_size": decision.file_size,
                        "account_name": decision.account_name,
                        "remaining_bytes": decision.remaining_bytes,
                        "remaining_files": decision.remaining_files,
                    }
                )
            else:
                failure = job.decision.error
                unroutable.append(
                    {
                        "file_name": failure.file_name,
                        "file_size": job.file_size,
                        "reason": failure.reason.value,
                        "message": failure.message,
                    }
                )
        return {
            "routable": routable,
            "unroutable": unroutable,
            "all_routable": self.all_routable,
            "revision": self.revision,
        }


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------
class AccountPool:
    """Thread-safe, single-writer pool of B2 accounts and their file cache."""

    def __init__(self, max_files_per_account: int = 1700) -> None:
        if max_files_per_account <= 0:
            raise PoolError("invalid_max_files", "max_files_per_account must be positive")
        self._accounts: dict[str, StorageBackend] = {}
        self._states: dict[str, AccountState] = {}
        self._file_cache: dict[str, FileEntry] = {}
        self._reservations: dict[str, Reservation] = {}
        self._default_max_files = max_files_per_account
        self._revision = 0
        self._lock = threading.RLock()

    # -- construction --------------------------------------------------
    def add_account(
        self,
        client: StorageBackend,
        capacity_bytes: int | None,
        *,
        max_files: int | None = None,
    ) -> AccountState:
        """Authorize an account and merge its files into the cache atomically.

        The cache is only touched after every file from this account has been
        validated, so a failure cannot leave partial state behind.
        """
        with self._lock:
            if client.name in self._accounts:
                raise PoolError(
                    "duplicate_account",
                    f"Account {client.name!r} is configured more than once.",
                    account=client.name,
                )
            try:
                versions: list[FileVersion] = client.list_files()
                all_versions: list[FileVersion] = client.list_versions()
            except StorageError as exc:
                raise PoolError(
                    "account_init_failed", f"Could not initialize {client.name!r}: {exc.message}"
                ) from exc

            visible_names = {version.file_name for version in versions}
            versions_by_name: dict[str, list[FileVersion]] = {}
            for version in all_versions:
                # A latest-only listing is enough to decide whether a file is
                # visible, but not whether B2 still stores older versions of
                # it. Only check visible names so a hide marker cannot
                # resurrect an otherwise hidden object.
                if version.file_name in visible_names:
                    versions_by_name.setdefault(version.file_name, []).append(version)
            for file_name, file_versions in versions_by_name.items():
                if len(file_versions) > 1:
                    raise PoolError(
                        "duplicate_filename",
                        (
                            f"File {file_name!r} has {len(file_versions)} upload versions "
                            f"in account {client.name!r}. Run b2router-reconcile --apply "
                            "with the gateway stopped before starting the router."
                        ),
                        file_name=file_name,
                        account=client.name,
                        version_count=len(file_versions),
                    )

            staged: dict[str, FileEntry] = {}
            for version in versions:
                if version.file_name in self._file_cache or version.file_name in staged:
                    existing = self._file_cache.get(version.file_name) or staged[version.file_name]
                    raise PoolError(
                        "duplicate_filename",
                        f"File {version.file_name!r} exists in both "
                        f"{existing.accounts[0]!r} and {client.name!r}.",
                        file_name=version.file_name,
                        accounts=list(existing.accounts) + [client.name],
                    )
                staged[version.file_name] = FileEntry(
                    file_name=version.file_name,
                    file_id=version.file_id,
                    size=version.size,
                    content_type=version.content_type,
                    upload_timestamp=version.upload_timestamp,
                    action=version.action,
                    accounts=(client.name,),
                )

            used = sum(version.size for version in versions)
            state = AccountState(
                name=client.name,
                capacity_bytes=capacity_bytes,
                used_bytes=used,
                file_count=len(versions),
                max_files=max_files or self._default_max_files,
            )
            self._accounts[client.name] = client
            self._states[client.name] = state
            self._file_cache.update(staged)
            self._revision += 1
            return state

    def load_accounts(self, accounts: Iterable[tuple[StorageBackend, int | None]]) -> None:
        """Load every account, aggregating failures into one startup error."""
        issues: list[str] = []
        for client, capacity in accounts:
            try:
                self.add_account(client, capacity)
            except PoolError as exc:
                issues.append(exc.message)
        if issues:
            raise PoolStartupError(issues)

    def set_max_files_per_account(self, max_files: int) -> None:
        if max_files <= 0:
            raise PoolError("invalid_max_files", "max_files must be positive")
        with self._lock:
            self._default_max_files = max_files
            for name, state in self._states.items():
                self._states[name] = state.with_max_files(max_files)
            self._revision += 1

    # -- read access ---------------------------------------------------
    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._accounts)

    def accounts(self) -> list[StorageBackend]:
        with self._lock:
            return [self._accounts[name] for name in sorted(self._accounts)]

    def get(self, name: str) -> StorageBackend | None:
        with self._lock:
            return self._accounts.get(name)

    def require(self, name: str) -> StorageBackend:
        client = self.get(name)
        if client is None:
            raise PoolError("account_not_found", f"Account {name!r} is not in the pool.")
        return client

    def __len__(self) -> int:
        with self._lock:
            return len(self._accounts)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._accounts

    def get_file(self, file_name: str) -> FileEntry | None:
        with self._lock:
            return self._file_cache.get(file_name)

    def get_file_accounts(self, file_name: str) -> list[str]:
        with self._lock:
            entry = self._file_cache.get(file_name)
            return list(entry.accounts) if entry else []

    def list_files(self) -> list[FileEntry]:
        with self._lock:
            return [self._file_cache[name] for name in sorted(self._file_cache)]

    def files_snapshot(self) -> tuple[list[FileEntry], int]:
        """Return files and the matching revision from one consistent read."""
        with self._lock:
            return [self._file_cache[name] for name in sorted(self._file_cache)], self._revision

    def list_files_for_account(self, account_name: str) -> list[FileEntry]:
        with self._lock:
            return [
                entry
                for name, entry in sorted(self._file_cache.items())
                if account_name in entry.accounts
            ]

    def get_account_state(self, name: str) -> AccountState | None:
        with self._lock:
            return self._states.get(name)

    def account_states(self) -> list[AccountState]:
        with self._lock:
            return [self._states[name] for name in sorted(self._states)]

    def accounts_snapshot(self) -> tuple[list[AccountState], int]:
        """Return account states and the matching revision from one read."""
        with self._lock:
            return [self._states[name] for name in sorted(self._states)], self._revision

    def stats(self) -> dict[str, Any]:
        with self._lock:
            capacity: int | None = 0
            for state in self._states.values():
                if state.capacity_bytes is None:
                    capacity = None
                    break
                assert capacity is not None
                capacity += state.capacity_bytes
            used = sum(state.used_bytes for state in self._states.values())
            reserved = sum(reservation.file_size for reservation in self._reservations.values())
            if capacity is None:
                free: int | None = None
            else:
                free = max(capacity - used, 0)
            return {
                "files": len(self._file_cache),
                "size_bytes": sum(entry.size for entry in self._file_cache.values()),
                "accounts": len(self._accounts),
                "capacity_bytes": capacity,
                "used_bytes": used,
                "free_bytes": free,
                "reserved_bytes": reserved,
                "reserved_files": len(self._reservations),
                "revision": self._revision,
            }

    # Backwards-compatible alias used by earlier tooling.
    def file_cache_stats(self) -> dict[str, Any]:
        return self.stats()

    # -- routing -------------------------------------------------------
    @staticmethod
    def _plan(
        file_descriptors: list[tuple[str, int]],
        states: Mapping[str, AccountState],
        file_cache: Mapping[str, FileEntry],
        reserved_bytes: Mapping[str, int],
        reserved_files: Mapping[str, int],
        allow_existing: frozenset[str],
    ) -> list[RoutingJob]:
        batch_bytes: dict[str, int] = {}
        batch_files: dict[str, int] = {}
        seen: set[str] = set()
        jobs: list[RoutingJob] = []

        for file_name, file_size in file_descriptors:
            if file_name in seen:
                jobs.append(
                    RoutingJob(
                        file_name,
                        file_size,
                        Err(
                            RoutingFailure(
                                file_name,
                                RoutingFailureReason.DUPLICATE_IN_BATCH,
                                "The same file name appears more than once in this request.",
                            )
                        ),
                    )
                )
                continue
            seen.add(file_name)

            if file_name in file_cache and file_name not in allow_existing:
                jobs.append(
                    RoutingJob(
                        file_name,
                        file_size,
                        Err(
                            RoutingFailure(
                                file_name,
                                RoutingFailureReason.DUPLICATE_FILENAME,
                                "A file with this name already exists in the pool.",
                            )
                        ),
                    )
                )
                continue

            best_name: str | None = None
            best_remaining: int | None = None
            replacement = file_name in file_cache and file_name in allow_existing
            old_entry = file_cache[file_name] if replacement else None
            old_account = old_entry.accounts[0] if old_entry is not None else None
            for name in sorted(states):
                state = states[name]
                remaining = state.remaining_bytes
                free_slots = (
                    state.remaining_files - reserved_files.get(name, 0) - batch_files.get(name, 0)
                )
                if replacement and name == old_account:
                    # A same-account replacement frees the old slot and its bytes
                    # when the old version is deleted, so a full account can
                    # still be replaced.
                    free_slots += 1
                if free_slots <= 0:
                    continue
                if remaining is None:
                    effective = None
                    rem_after: int | None = 10**18
                else:
                    effective = remaining - reserved_bytes.get(name, 0) - batch_bytes.get(name, 0)
                    if replacement and name == old_account and old_entry is not None:
                        effective += old_entry.size
                    if effective < file_size:
                        continue
                    rem_after = effective - file_size
                if best_remaining is None or (rem_after is not None and rem_after < best_remaining):
                    best_remaining = rem_after
                    best_name = name

            if best_name is None:
                jobs.append(
                    RoutingJob(
                        file_name,
                        file_size,
                        Err(
                            RoutingFailure(
                                file_name,
                                RoutingFailureReason.NO_ACCOUNT_CAN_FIT,
                                "No account has enough free bytes and file slots.",
                            )
                        ),
                    )
                )
                continue

            state = states[best_name]
            remaining = state.remaining_bytes
            effective = (
                None
                if remaining is None
                else remaining - reserved_bytes.get(best_name, 0) - batch_bytes.get(best_name, 0)
            )
            if (
                replacement
                and best_name == old_account
                and old_entry is not None
                and effective is not None
            ):
                effective += old_entry.size
            final_remaining = None if effective is None else effective - file_size
            jobs.append(
                RoutingJob(
                    file_name,
                    file_size,
                    Ok(
                        RoutingDecision(
                            file_name=file_name,
                            file_size=file_size,
                            account_name=best_name,
                            remaining_bytes=final_remaining,
                            remaining_files=state.remaining_files
                            - reserved_files.get(best_name, 0)
                            - batch_files.get(best_name, 0)
                            - 1
                            + (1 if replacement and best_name == old_account else 0),
                        )
                    ),
                )
            )
            batch_bytes[best_name] = batch_bytes.get(best_name, 0) + file_size
            batch_files[best_name] = batch_files.get(best_name, 0) + 1

        return jobs

    def plan(
        self, file_descriptors: list[tuple[str, int]], *, allow_existing: Iterable[str] = ()
    ) -> RoutingPlan:
        with self._lock:
            states = dict(self._states)
            cache = dict(self._file_cache)
            reserved_bytes: dict[str, int] = {}
            reserved_files: dict[str, int] = {}
            for reservation in self._reservations.values():
                reserved_bytes[reservation.account_name] = (
                    reserved_bytes.get(reservation.account_name, 0) + reservation.file_size
                )
                reserved_files[reservation.account_name] = (
                    reserved_files.get(reservation.account_name, 0) + reservation.file_slots
                )
            revision = self._revision
        return RoutingPlan(
            self._plan(
                file_descriptors,
                states,
                cache,
                reserved_bytes,
                reserved_files,
                frozenset(allow_existing),
            ),
            revision,
        )

    def route_one(
        self, file_name: str, file_size: int, *, allow_existing: bool = False
    ) -> RoutingDecision:
        plan = self.plan(
            [(file_name, file_size)],
            allow_existing=(file_name,) if allow_existing else (),
        )
        job = plan.jobs[0]
        if isinstance(job.decision, Err):
            raise PoolError(
                job.decision.error.reason.value, job.decision.error.message, file_name=file_name
            )
        return job.decision.value

    # -- reservations --------------------------------------------------
    def try_reserve(
        self,
        account_name: str,
        file_name: str,
        file_size: int,
        *,
        replace_entry: FileEntry | None = None,
    ) -> Reservation:
        with self._lock:
            state = self._states.get(account_name)
            if state is None:
                raise PoolError(
                    "account_not_found", f"Account {account_name!r} is not in the pool."
                )
            reserved_bytes = sum(
                r.file_size for r in self._reservations.values() if r.account_name == account_name
            )
            reserved_files = sum(
                r.file_slots for r in self._reservations.values() if r.account_name == account_name
            )
            same_account_replacement = (
                replace_entry is not None and replace_entry.accounts[0] == account_name
            )
            remaining = state.remaining_bytes
            effective = None if remaining is None else remaining - reserved_bytes
            if effective is not None and same_account_replacement and replace_entry is not None:
                effective += replace_entry.size
            if effective is not None and effective < file_size:
                raise PoolError(
                    "no_account_can_fit",
                    f"Account {account_name!r} no longer has room for this file.",
                    account=account_name,
                )
            free_slots = state.remaining_files - reserved_files
            if same_account_replacement:
                free_slots += 1
            if free_slots <= 0:
                raise PoolError(
                    "no_account_can_fit",
                    f"Account {account_name!r} has no file slots left.",
                    account=account_name,
                )
            reservation = Reservation(
                id=uuid.uuid4().hex,
                account_name=account_name,
                file_name=file_name,
                file_size=file_size,
            )
            self._reservations[reservation.id] = reservation
            self._revision += 1
            return reservation

    def release(self, reservation_id: str) -> None:
        with self._lock:
            if self._reservations.pop(reservation_id, None) is not None:
                self._revision += 1

    def reservations(self) -> list[Reservation]:
        with self._lock:
            return list(self._reservations.values())

    # -- commits -------------------------------------------------------
    def commit_upload(self, reservation: Reservation, version: FileVersion) -> FileEntry:
        with self._lock:
            self._require_reservation(reservation)
            if version.file_name != reservation.file_name:
                raise PoolError(
                    "state_changed",
                    "The uploaded file name does not match the reservation.",
                    expected=reservation.file_name,
                    actual=version.file_name,
                )
            if version.file_name in self._file_cache:
                raise PoolError(
                    "duplicate_filename",
                    f"File {version.file_name!r} appeared in the cache during upload.",
                    file_name=version.file_name,
                )
            state = self._states[reservation.account_name]
            if state.remaining_files <= 0:
                raise PoolError(
                    "no_account_can_fit",
                    f"Account {reservation.account_name!r} has no file slots left.",
                )
            if state.remaining_bytes is not None and state.remaining_bytes < version.size:
                raise PoolError(
                    "no_account_can_fit",
                    f"Account {reservation.account_name!r} has no room left for this file.",
                )
            self._states[reservation.account_name] = state.with_file(version.size)
            entry = FileEntry(
                file_name=version.file_name,
                file_id=version.file_id,
                size=version.size,
                content_type=version.content_type,
                upload_timestamp=version.upload_timestamp,
                action=version.action,
                accounts=(reservation.account_name,),
            )
            self._file_cache[version.file_name] = entry
            del self._reservations[reservation.id]
            self._revision += 1
            return entry

    def commit_replace(
        self,
        reservation: Reservation,
        old_entry: FileEntry,
        version: FileVersion,
        *,
        old_deleted: bool,
    ) -> FileEntry:
        """Commit a replacement, optionally accounting for the old copy.

        When ``old_deleted`` is false the old version could not be removed and
        the router could not roll the new version back either; both copies are
        still stored, so both bytes and slots stay counted while the cache
        points at the newest version. The job is marked partial and the next
        startup will refuse the duplicate until it is reconciled.
        """
        with self._lock:
            self._require_reservation(reservation)
            if not (version.file_name == old_entry.file_name == reservation.file_name):
                raise PoolError(
                    "state_changed",
                    "The replacement names do not match the reservation.",
                )
            current = self._file_cache.get(old_entry.file_name)
            if current is None or current.file_id != old_entry.file_id:
                raise PoolError(
                    "file_changed",
                    "The cached file changed during replacement.",
                    file_name=old_entry.file_name,
                )

            new_account = reservation.account_name
            old_account = old_entry.accounts[0]
            new_state = self._states[new_account]
            projected_used = new_state.used_bytes + version.size
            projected_count = new_state.file_count + 1
            if old_deleted and old_account == new_account:
                projected_used -= old_entry.size
                projected_count -= 1

            if old_deleted:
                if (
                    new_state.capacity_bytes is not None
                    and projected_used > new_state.capacity_bytes
                ):
                    raise PoolError(
                        "no_account_can_fit",
                        f"Account {new_account!r} has no room for the replacement.",
                    )
                if projected_count > new_state.max_files:
                    raise PoolError(
                        "no_account_can_fit",
                        f"Account {new_account!r} has no file slots for the replacement.",
                    )

            if old_account == new_account:
                self._states[new_account] = replace(
                    new_state,
                    used_bytes=max(projected_used, 0),
                    file_count=max(projected_count, 0),
                )
            else:
                self._states[new_account] = new_state.with_file(version.size)
                if old_deleted:
                    old_state = self._states[old_account]
                    self._states[old_account] = replace(
                        old_state,
                        used_bytes=max(old_state.used_bytes - old_entry.size, 0),
                        file_count=max(old_state.file_count - 1, 0),
                    )

            entry = FileEntry(
                file_name=version.file_name,
                file_id=version.file_id,
                size=version.size,
                content_type=version.content_type,
                upload_timestamp=version.upload_timestamp,
                action=version.action,
                accounts=(new_account,),
            )
            self._file_cache[version.file_name] = entry
            del self._reservations[reservation.id]
            self._revision += 1
            return entry

    def commit_delete(self, file_name: str, *, expected_file_id: str | None = None) -> FileEntry:
        with self._lock:
            entry = self._file_cache.get(file_name)
            if entry is None:
                raise PoolError("file_not_found", f"File {file_name!r} is not in the pool.")
            if expected_file_id is not None and expected_file_id != entry.file_id:
                raise PoolError(
                    "file_changed",
                    "The file changed since it was read; refresh and retry.",
                    file_name=file_name,
                    expected_file_id=expected_file_id,
                    actual_file_id=entry.file_id,
                )
            account = entry.accounts[0]
            state = self._states[account]
            self._states[account] = replace(
                state,
                used_bytes=max(state.used_bytes - entry.size, 0),
                file_count=max(state.file_count - 1, 0),
            )
            del self._file_cache[file_name]
            self._revision += 1
            return entry

    def _require_reservation(self, reservation: Reservation) -> None:
        current = self._reservations.get(reservation.id)
        if current is None:
            raise PoolError(
                "state_changed",
                "The reservation for this upload is no longer valid.",
                reservation_id=reservation.id,
            )
        if current != reservation:
            raise PoolError(
                "state_changed",
                "The reservation changed while the upload was running.",
                reservation_id=reservation.id,
            )
