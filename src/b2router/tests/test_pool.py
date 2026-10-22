"""Placement, reservation and accounting invariants."""

from __future__ import annotations

import pytest

from b2router.services.account import FileVersion
from b2router.services.pool import (
    AccountPool,
    Err,
    Ok,
    PoolError,
    PoolStartupError,
    RoutingFailureReason,
)
from b2router.tests.conftest import FakeStorage


def _version(name: str, size: int, account: str = "x", suffix: str = "1") -> FileVersion:
    return FileVersion(
        file_name=name,
        file_id=f"{account}-{suffix}",
        size=size,
        content_type="application/octet-stream",
        upload_timestamp=1_700_000_000_000,
        action="upload",
    )


def test_add_account_builds_cache_and_stats() -> None:
    fake = FakeStorage("a")
    fake.seed("one.txt", b"x" * 100)
    fake.seed("two.txt", b"y" * 50)
    pool = AccountPool(max_files_per_account=5)

    state = pool.add_account(fake, capacity_bytes=1000)

    assert state.file_count == 2
    assert state.used_bytes == 150
    assert pool.get_file("one.txt") is not None
    assert pool.stats()["files"] == 2
    assert pool.stats()["capacity_bytes"] == 1000
    assert pool.stats()["free_bytes"] == 850


def test_duplicate_file_across_accounts_fails_startup() -> None:
    first = FakeStorage("a")
    first.seed("same.txt", b"1")
    second = FakeStorage("b")
    second.seed("same.txt", b"2")
    pool = AccountPool()

    with pytest.raises(PoolStartupError) as excinfo:
        pool.load_accounts([(first, None), (second, None)])
    assert "same.txt" in str(excinfo.value)


def test_duplicate_file_in_same_account_fails_startup() -> None:
    fake = FakeStorage("a")
    fake.seed("same.txt", b"old")
    fake.seed("same.txt", b"new")
    pool = AccountPool()

    with pytest.raises(PoolStartupError) as excinfo:
        pool.load_accounts([(fake, None)])

    message = str(excinfo.value)
    assert "same.txt" in message
    assert "2 upload versions" in message
    assert "b2router-reconcile --apply" in message
    assert pool.names() == []


def test_routing_prefers_best_fit() -> None:
    tight = FakeStorage("tight")
    tight.seed("big.bin", b"x" * 900)
    roomy = FakeStorage("roomy")
    pool = AccountPool()
    pool.add_account(tight, 1000)
    pool.add_account(roomy, 1000)

    plan = pool.plan([("small.bin", 50)])

    decision = plan.jobs[0].decision
    assert isinstance(decision, Ok)
    assert decision.value.account_name == "tight"


def test_routing_rejects_duplicate_filename() -> None:
    fake = FakeStorage("a")
    fake.seed("exists.txt", b"1")
    pool = AccountPool()
    pool.add_account(fake, 1000)

    plan = pool.plan([("exists.txt", 10)])
    failure = plan.jobs[0].decision
    assert isinstance(failure, Err)
    assert failure.error.reason is RoutingFailureReason.DUPLICATE_FILENAME

    upsert_plan = pool.plan([("exists.txt", 10)], allow_existing=["exists.txt"])
    assert isinstance(upsert_plan.jobs[0].decision, Ok)


def test_routing_rejects_duplicate_in_batch() -> None:
    pool = AccountPool()
    pool.add_account(FakeStorage("a"), None)
    plan = pool.plan([("x", 1), ("x", 1)])
    assert isinstance(plan.jobs[1].decision, Err)
    assert plan.jobs[1].decision.error.reason is RoutingFailureReason.DUPLICATE_IN_BATCH


def test_routing_respects_reservations() -> None:
    pool = AccountPool()
    pool.add_account(FakeStorage("a"), 100)
    reservation = pool.try_reserve("a", "one.bin", 60)

    plan = pool.plan([("two.bin", 50)])
    failure = plan.jobs[0].decision
    assert isinstance(failure, Err)
    assert failure.error.reason is RoutingFailureReason.NO_ACCOUNT_CAN_FIT

    pool.release(reservation.id)
    assert isinstance(pool.plan([("two.bin", 50)]).jobs[0].decision, Ok)


def test_file_slot_limit_is_enforced() -> None:
    fake = FakeStorage("a")
    fake.seed("one", b"1")
    fake.seed("two", b"2")
    pool = AccountPool(max_files_per_account=2)
    pool.add_account(fake, None)

    plan = pool.plan([("three", 1)])
    assert isinstance(plan.jobs[0].decision, Err)
    assert plan.jobs[0].decision.error.reason is RoutingFailureReason.NO_ACCOUNT_CAN_FIT


def test_same_account_upsert_can_use_its_own_full_slot() -> None:
    fake = FakeStorage("a")
    fake.seed("only", b"1")
    pool = AccountPool(max_files_per_account=1)
    pool.add_account(fake, None)

    assert isinstance(pool.plan([("other", 1)]).jobs[0].decision, Err)
    plan = pool.plan([("only", 1)], allow_existing=["only"])
    assert isinstance(plan.jobs[0].decision, Ok)
    assert plan.jobs[0].decision.value.account_name == "a"


def test_same_account_upsert_can_use_its_own_bytes() -> None:
    fake = FakeStorage("a")
    fake.seed("only", b"x" * 900)
    pool = AccountPool()
    pool.add_account(fake, capacity_bytes=1000)

    assert isinstance(pool.plan([("other", 950)]).jobs[0].decision, Err)
    plan = pool.plan([("only", 950)], allow_existing=["only"])
    decision = plan.jobs[0].decision
    assert isinstance(decision, Ok)
    assert decision.value.remaining_bytes == 50


def test_reservation_and_release_invalidate_revision() -> None:
    pool = AccountPool()
    pool.add_account(FakeStorage("a"), 100)
    before = pool.revision

    reservation = pool.try_reserve("a", "one.bin", 10)
    reserved = pool.revision
    assert reserved > before
    assert pool.stats()["reserved_bytes"] == 10

    pool.release(reservation.id)
    assert pool.revision > reserved
    assert pool.stats()["reserved_bytes"] == 0


def test_cross_account_upsert_requires_a_free_slot() -> None:
    fake_a = FakeStorage("a")
    fake_a.seed("only", b"1")
    fake_b = FakeStorage("b")
    fake_b.seed("other", b"2")
    pool = AccountPool(max_files_per_account=1)
    pool.add_account(fake_a, 1000)
    pool.add_account(fake_b, 1000)

    # Only account a can accept the replacement: b is full and a can reuse the
    # slot its old version will free.
    plan = pool.plan([("only", 1)], allow_existing=["only"])
    assert isinstance(plan.jobs[0].decision, Ok)
    assert plan.jobs[0].decision.value.account_name == "a"


def test_commit_upload_updates_cache_and_accounting() -> None:
    pool = AccountPool()
    pool.add_account(FakeStorage("a"), 1000)
    reservation = pool.try_reserve("a", "new.txt", 20)

    entry = pool.commit_upload(reservation, _version("new.txt", 20, "a"))

    assert entry.accounts == ("a",)
    assert pool.get_file("new.txt") == entry
    assert pool.get_account_state("a").used_bytes == 20
    assert pool.reservations() == []


def test_commit_replace_same_account_adjusts_size() -> None:
    fake = FakeStorage("a")
    fake.seed("file.txt", b"x" * 100)
    pool = AccountPool()
    pool.add_account(fake, 1000)
    old_entry = pool.get_file("file.txt")
    assert old_entry is not None
    reservation = pool.try_reserve("a", "file.txt", 40)

    new_entry = pool.commit_replace(
        reservation, old_entry, _version("file.txt", 40, "a", "2"), old_deleted=True
    )

    state = pool.get_account_state("a")
    assert state.used_bytes == 40
    assert state.file_count == 1
    assert new_entry.file_id == "a-2"
    assert pool.get_file("file.txt").file_id == "a-2"  # type: ignore[union-attr]


def test_commit_replace_cross_account_moves_accounting() -> None:
    fake_a = FakeStorage("a")
    fake_a.seed("file.txt", b"x" * 100)
    pool = AccountPool()
    pool.add_account(fake_a, 1000)
    pool.add_account(FakeStorage("b"), 1000)
    old_entry = pool.get_file("file.txt")
    assert old_entry is not None
    reservation = pool.try_reserve("b", "file.txt", 70)

    entry = pool.commit_replace(
        reservation, old_entry, _version("file.txt", 70, "b", "2"), old_deleted=True
    )

    assert entry.accounts == ("b",)
    assert pool.get_account_state("a").used_bytes == 0
    assert pool.get_account_state("a").file_count == 0
    assert pool.get_account_state("b").used_bytes == 70
    assert pool.get_account_state("b").file_count == 1


def test_partial_replace_keeps_old_accounting() -> None:
    fake_a = FakeStorage("a")
    fake_a.seed("file.txt", b"x" * 100)
    pool = AccountPool()
    pool.add_account(fake_a, 1000)
    pool.add_account(FakeStorage("b"), 1000)
    old_entry = pool.get_file("file.txt")
    assert old_entry is not None
    reservation = pool.try_reserve("b", "file.txt", 70)

    pool.commit_replace(
        reservation, old_entry, _version("file.txt", 70, "b", "2"), old_deleted=False
    )

    # The old version still exists in account a, so its bytes and slot stay counted.
    assert pool.get_account_state("a").used_bytes == 100
    assert pool.get_account_state("a").file_count == 1
    assert pool.get_account_state("b").used_bytes == 70
    assert pool.get_file("file.txt").accounts == ("b",)  # type: ignore[union-attr]


def test_commit_delete_checks_etag() -> None:
    fake = FakeStorage("a")
    fake.seed("file.txt", b"data")
    pool = AccountPool()
    pool.add_account(fake, None)
    entry = pool.get_file("file.txt")
    assert entry is not None

    with pytest.raises(PoolError) as excinfo:
        pool.commit_delete("file.txt", expected_file_id="wrong")
    assert excinfo.value.code == "file_changed"

    deleted = pool.commit_delete("file.txt", expected_file_id=entry.file_id)
    assert deleted.file_id == entry.file_id
    assert pool.get_file("file.txt") is None
    assert pool.get_account_state("a").file_count == 0


def test_route_one_returns_decision() -> None:
    pool = AccountPool()
    pool.add_account(FakeStorage("a"), 100)
    decision = pool.route_one("x", 10)
    assert decision.account_name == "a"
    assert decision.remaining_bytes == 90


def test_route_one_raises_pool_error() -> None:
    pool = AccountPool()
    pool.add_account(FakeStorage("a"), 5)
    with pytest.raises(PoolError) as excinfo:
        pool.route_one("x", 10)
    assert excinfo.value.code == "no_account_can_fit"
