import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from queue_load_test.harness.phase3_repository import seed_synthetic_sessions
from queue_load_test.models import (
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.repository import (
    LeaseOwnershipError,
    ManualSessionBusyError,
    ManualSessionCapacityError,
    QueueIdConflictError,
    SessionRepository,
    SQLiteSessionRepository,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def make_session(
    session_id: str,
    *,
    queue_id: str | None = None,
    status: QueueStatus = QueueStatus.NEW,
    next_check_at: datetime | None = None,
) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=queue_id,
        transfer_url=f"https://staging.example.test/transfer?session={session_id}",
        mode=SessionMode.HYBRID,
        status=status,
        state_path=Path(f".browser-state/{session_id}.json"),
        created_at=NOW,
        next_check_at=next_check_at,
    )


def accepts_repository_contract(repository: SessionRepository) -> SessionRepository:
    return repository


def test_sqlite_repository_satisfies_backend_contract(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "contract.sqlite3")

    assert accepts_repository_contract(repository) is repository


async def test_create_and_get_session(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    session = make_session("session-1", queue_id="queue-1")

    await repository.create(session)
    loaded = await repository.get(session.session_id)

    assert loaded == session
    assert "transfer?session" not in repr(loaded)
    assert ".browser-state" not in repr(loaded)
    await repository.close()


async def test_update_lifecycle_and_scheduling_fields(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    session = make_session("session-1")
    await repository.create(session)

    session.status = QueueStatus.CREATING
    session.queue_id = "queue-1"
    session.last_checked_at = NOW
    session.last_queue_update = NOW - timedelta(seconds=2)
    session.last_progress_change_at = NOW - timedelta(seconds=5)
    session.next_check_at = NOW + timedelta(seconds=10)
    session.attempt_count = 1
    await repository.update(session)

    loaded = await repository.get(session.session_id)
    assert loaded is not None
    assert loaded.status is QueueStatus.CREATING
    assert loaded.queue_id == "queue-1"
    assert loaded.last_checked_at == NOW
    assert loaded.last_queue_update == NOW - timedelta(seconds=2)
    assert loaded.last_progress_change_at == NOW - timedelta(seconds=5)
    assert loaded.next_check_at == NOW + timedelta(seconds=10)
    assert loaded.attempt_count == 1
    await repository.close()


async def test_non_null_queue_id_is_unique_but_null_is_not(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("session-1", queue_id="queue-1"))
    await repository.create(make_session("session-2"))
    await repository.create(make_session("session-3"))

    with pytest.raises(QueueIdConflictError):
        await repository.create(make_session("session-4", queue_id="queue-1"))

    assert len(await repository.list()) == 3
    await repository.close()


async def test_queue_id_uniqueness_is_enforced_on_update(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("session-1", queue_id="queue-1"))
    second = make_session("session-2")
    await repository.create(second)
    second.status = QueueStatus.CREATING
    second.queue_id = "queue-1"

    with pytest.raises(QueueIdConflictError):
        await repository.update(second)

    persisted = await repository.get("session-2")
    assert persisted is not None
    assert persisted.queue_id is None
    assert persisted.status is QueueStatus.NEW
    await repository.close()


async def test_failed_attempts_do_not_count_as_successful_queue_ids(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(
        make_session("failed", queue_id="failed-queue", status=QueueStatus.FAILED)
    )
    await repository.create(
        make_session("active", queue_id="active-queue", status=QueueStatus.ACTIVE_QUEUE)
    )
    await repository.create(make_session("no-identity", status=QueueStatus.PRE_QUEUE))

    assert await repository.count_successful_queue_ids() == 1
    await repository.close()


async def test_claim_selects_only_due_non_terminal_sessions(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("due", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW))
    await repository.create(
        make_session(
            "future",
            status=QueueStatus.ACTIVE_QUEUE,
            next_check_at=NOW + timedelta(minutes=1),
        )
    )
    await repository.create(
        make_session("admitted", status=QueueStatus.ADMITTED, next_check_at=NOW)
    )

    claimed = await repository.claim_due_sessions(
        worker_id="worker-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=10,
    )

    assert [session.session_id for session in claimed] == ["due"]
    assert claimed[0].worker_id == "worker-1"
    assert claimed[0].lease_until == NOW + timedelta(seconds=30)
    await repository.close()


async def test_persisted_monitoring_pause_atomically_blocks_claims(tmp_path: Path) -> None:
    database = tmp_path / "paused-claims.sqlite3"
    repository = SQLiteSessionRepository(database)
    original = make_session("due", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW)
    await repository.create(original)

    assert await repository.set_monitoring_paused(True)
    assert await repository.claim_due_sessions(
        worker_id="worker-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    ) == []
    paused = await repository.get("due")
    assert paused is not None
    assert paused.status is QueueStatus.ACTIVE_QUEUE
    assert paused.queue_id == original.queue_id
    assert paused.worker_id is None
    assert paused.lease_until is None
    assert (await repository.due_session_summary(now=NOW)).count == 1
    await repository.close()

    reopened = SQLiteSessionRepository(database)
    assert await reopened.is_monitoring_paused()
    assert not await reopened.set_monitoring_paused(False)
    resumed_claim = await reopened.claim_due_sessions(
        worker_id="worker-2",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    )
    assert [session.session_id for session in resumed_claim] == ["due"]
    await reopened.release_lease("due", worker_id="worker-2")
    await reopened.close()


@pytest.mark.parametrize(
    "status",
    [
        QueueStatus.ADMITTED,
        QueueStatus.EXPIRED,
        QueueStatus.FAILED,
        QueueStatus.NEW,
        QueueStatus.CREATING,
    ],
)
async def test_claim_excludes_non_monitorable_statuses(
    tmp_path: Path,
    status: QueueStatus,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / f"{status.value}.sqlite3")
    await repository.create(make_session("excluded", status=status, next_check_at=NOW))

    assert await repository.count_due_sessions(now=NOW) == 0
    assert (
        await repository.claim_due_sessions(
            worker_id="worker-1",
            now=NOW,
            lease_until=NOW + timedelta(seconds=30),
            limit=1,
        )
        == []
    )
    await repository.close()


async def test_active_lease_prevents_claim_until_expiry(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(
        make_session("session-1", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW)
    )
    first = await repository.claim_due_sessions(
        worker_id="worker-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    )
    assert await repository.count_due_sessions(now=NOW + timedelta(seconds=10)) == 0
    blocked = await repository.claim_due_sessions(
        worker_id="worker-2",
        now=NOW + timedelta(seconds=10),
        lease_until=NOW + timedelta(seconds=40),
        limit=1,
    )
    reclaimed = await repository.claim_due_sessions(
        worker_id="worker-2",
        now=NOW + timedelta(seconds=31),
        lease_until=NOW + timedelta(seconds=61),
        limit=1,
    )

    assert len(first) == 1
    assert blocked == []
    assert [session.worker_id for session in reclaimed] == ["worker-2"]
    assert await repository.count_due_sessions(now=NOW + timedelta(seconds=60)) == 0
    assert await repository.count_due_sessions(now=NOW + timedelta(seconds=62)) == 1
    await repository.close()


async def test_manual_ownership_blocks_scheduler_and_releases_back_to_due_work(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "manual-owner.sqlite3")
    await repository.create(
        make_session("manual", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW)
    )

    owned = await repository.acquire_manual_ownership(
        "manual",
        owner_id="operator-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        capacity=1,
    )

    assert owned.manual_owner_id == "operator-1"
    assert await repository.count_due_sessions(now=NOW) == 0
    assert await repository.claim_due_sessions(
        worker_id="automatic",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    ) == []
    assert await repository.release_manual_ownership("manual", owner_id="operator-1")
    assert await repository.count_due_sessions(now=NOW) == 1
    assert [
        item.session_id
        for item in await repository.claim_due_sessions(
            worker_id="automatic",
            now=NOW,
            lease_until=NOW + timedelta(seconds=30),
            limit=1,
        )
    ] == ["manual"]
    await repository.close()


async def test_manual_open_is_busy_during_automatic_check(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "manual-busy.sqlite3")
    await repository.create(
        make_session("busy", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW)
    )
    await repository.claim_due_sessions(
        worker_id="automatic",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    )

    with pytest.raises(ManualSessionBusyError, match="currently being checked"):
        await repository.acquire_manual_ownership(
            "busy",
            owner_id="operator",
            now=NOW,
            lease_until=NOW + timedelta(seconds=30),
            capacity=1,
        )
    await repository.close()


async def test_manual_capacity_and_stale_restart_recovery(tmp_path: Path) -> None:
    database = tmp_path / "manual-capacity.sqlite3"
    repository = SQLiteSessionRepository(database)
    for session_id in ("first", "second"):
        await repository.create(
            make_session(
                session_id,
                queue_id=f"queue-{session_id}",
                status=QueueStatus.ACTIVE_QUEUE,
                next_check_at=NOW,
            )
        )
    await repository.acquire_manual_ownership(
        "first",
        owner_id="old-process",
        now=NOW,
        lease_until=NOW + timedelta(seconds=5),
        capacity=1,
    )
    with pytest.raises(ManualSessionCapacityError, match="Browser capacity"):
        await repository.acquire_manual_ownership(
            "second",
            owner_id="operator",
            now=NOW,
            lease_until=NOW + timedelta(seconds=5),
            capacity=1,
        )
    await repository.close()

    reopened = SQLiteSessionRepository(database)
    assert await reopened.recover_stale_manual_ownership(
        now=NOW + timedelta(seconds=6)
    ) == 1
    recovered = await reopened.get("first")
    assert recovered is not None
    assert recovered.manual_owner_id is None
    assert recovered.queue_id == "queue-first"
    await reopened.close()


async def test_release_lease_checks_owner_when_provided(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("session-1", status=QueueStatus.PARKED, next_check_at=NOW))
    await repository.claim_due_sessions(
        worker_id="worker-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    )

    assert not await repository.release_lease("session-1", worker_id="worker-2")
    assert await repository.release_lease("session-1", worker_id="worker-1")
    loaded = await repository.get("session-1")
    assert loaded is not None
    assert loaded.worker_id is None
    assert loaded.lease_until is None
    await repository.close()


async def test_reclaimed_lease_fences_stale_worker_update(tmp_path: Path) -> None:
    database = tmp_path / "lease-fencing.sqlite3"
    first_repository = SQLiteSessionRepository(database)
    await first_repository.create(
        make_session("session-1", status=QueueStatus.PARKED, next_check_at=NOW)
    )
    stale_claim = await first_repository.claim_due_sessions(
        worker_id="crashed-worker",
        now=NOW,
        lease_until=NOW + timedelta(seconds=1),
        limit=1,
    )
    second_repository = SQLiteSessionRepository(database)
    await second_repository.initialize()
    replacement_claim = await second_repository.claim_due_sessions(
        worker_id="replacement-worker",
        now=NOW + timedelta(seconds=2),
        lease_until=NOW + timedelta(seconds=32),
        limit=1,
    )

    assert len(stale_claim) == len(replacement_claim) == 1
    stale_claim[0].last_error = "stale-result"
    with pytest.raises(LeaseOwnershipError):
        await first_repository.update(stale_claim[0])

    persisted = await second_repository.get("session-1")
    assert persisted is not None
    assert persisted.worker_id == "replacement-worker"
    assert persisted.lease_until == NOW + timedelta(seconds=32)
    assert persisted.last_error is None

    await second_repository.close()
    await first_repository.close()


async def test_unleased_snapshot_cannot_clear_another_workers_lease(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "unleased-fencing.sqlite3")
    await repository.create(make_session("session-1", status=QueueStatus.PARKED, next_check_at=NOW))
    stale_snapshot = await repository.get("session-1")
    assert stale_snapshot is not None
    claimed = await repository.claim_due_sessions(
        worker_id="worker-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        limit=1,
    )

    assert len(claimed) == 1
    stale_snapshot.last_error = "stale-unleased-result"
    with pytest.raises(LeaseOwnershipError):
        await repository.update(stale_snapshot)

    persisted = await repository.get("session-1")
    assert persisted is not None
    assert persisted.worker_id == "worker-1"
    assert persisted.last_error is None
    await repository.close()


async def test_one_thousand_mixed_sessions_have_bounded_disjoint_concurrent_claims(
    tmp_path: Path,
) -> None:
    database = tmp_path / "one-thousand.sqlite3"
    first_repository = SQLiteSessionRepository(database)
    expected_due = await seed_synthetic_sessions(first_repository, now=NOW)
    second_repository = SQLiteSessionRepository(database)
    await second_repository.initialize()

    first_claim, second_claim = await asyncio.gather(
        first_repository.claim_due_sessions(
            worker_id="worker-1",
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
            limit=50,
        ),
        second_repository.claim_due_sessions(
            worker_id="worker-2",
            now=NOW,
            lease_until=NOW + timedelta(minutes=2),
            limit=50,
        ),
    )

    first_ids = {session.session_id for session in first_claim}
    second_ids = {session.session_id for session in second_claim}
    assert expected_due == 600
    assert len(first_claim) == len(second_claim) == 50
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {
        f"synthetic-{index:04d}" for index in range(800, 900)
    }
    assert {session.worker_id for session in first_claim} == {"worker-1"}
    assert {session.worker_id for session in second_claim} == {"worker-2"}
    assert await first_repository.count_due_sessions(now=NOW) == 500

    await second_repository.close()
    await first_repository.close()


async def test_due_query_uses_ordered_partial_index_without_temporary_sort(
    tmp_path: Path,
) -> None:
    database = tmp_path / "query-plan.sqlite3"
    repository = SQLiteSessionRepository(database)
    await seed_synthetic_sessions(repository, now=NOW)

    plan = await repository.explain_due_session_query(now=NOW, limit=50)

    assert any("idx_queue_sessions_due" in detail for detail in plan)
    assert all("TEMP B-TREE" not in detail for detail in plan)
    await repository.close()


async def test_existing_phase_two_due_index_is_migrated(tmp_path: Path) -> None:
    database = tmp_path / "migrated-index.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    await repository.close()
    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX idx_queue_sessions_due")
    connection.execute(
        "CREATE INDEX idx_queue_sessions_due "
        "ON queue_sessions (next_check_at, lease_until, status)"
    )
    connection.commit()
    connection.close()

    restarted = SQLiteSessionRepository(database)
    await restarted.initialize()
    plan = await restarted.explain_due_session_query(now=NOW, limit=50)

    assert any("idx_queue_sessions_due" in detail for detail in plan)
    assert all("TEMP B-TREE" not in detail for detail in plan)
    await restarted.close()


async def test_sessions_survive_repository_restart(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    first_repository = SQLiteSessionRepository(database)
    expected = make_session("session-1", queue_id="queue-1")
    await first_repository.create(expected)
    await first_repository.close()

    restarted_repository = SQLiteSessionRepository(database)
    loaded = await restarted_repository.get("session-1")

    assert loaded == expected
    await restarted_repository.close()


async def test_recovery_summary_uses_aggregate_counts_for_mixed_sessions(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "recovery-summary.sqlite3")
    due = make_session(
        "due",
        queue_id="queue-due",
        status=QueueStatus.CONNECTION_LOST,
        next_check_at=NOW,
    )
    due.last_error = "restore:NAVIGATION_FAILED"
    expired_lease = make_session(
        "expired-lease",
        queue_id="queue-expired-lease",
        status=QueueStatus.PARKED,
        next_check_at=NOW,
    )
    expired_lease.worker_id = "old-worker"
    expired_lease.lease_until = NOW - timedelta(seconds=1)
    active_lease = make_session(
        "active-lease",
        queue_id="queue-active-lease",
        status=QueueStatus.PARKED,
        next_check_at=NOW,
    )
    active_lease.worker_id = "live-worker"
    active_lease.lease_until = NOW + timedelta(seconds=30)
    terminal = make_session(
        "terminal",
        queue_id="queue-terminal",
        status=QueueStatus.ADMITTED,
        next_check_at=None,
    )
    failed = make_session("failed", status=QueueStatus.FAILED, next_check_at=None)
    for session in (due, expired_lease, active_lease, terminal, failed):
        await repository.create(session)

    summary = await repository.recovery_summary(now=NOW)

    assert summary.total_persisted_sessions == 5
    assert summary.valid_queue_ids == 4
    assert summary.leased_sessions == 1
    assert summary.expired_leases == 1
    assert summary.sessions_due == 2
    assert summary.sessions_requiring_retry == 1
    assert summary.terminal_sessions == 2
    assert summary.status_counts[QueueStatus.PARKED] == 2
    assert summary.status_counts[QueueStatus.CONNECTION_LOST] == 1
    assert not summary.state_scan_performed
    await repository.close()


async def test_progress_survives_repository_restart(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    first_repository = SQLiteSessionRepository(database)
    expected_session = make_session("session-1", queue_id="queue-1")
    expected_progress = QueueProgress(
        session_id="session-1",
        queue_number="123",
        users_ahead=12,
        progress_percentage=42,
        queue_paused=False,
        active_queue=True,
    )
    await first_repository.create(expected_session, expected_progress)
    await first_repository.close()

    restarted_repository = SQLiteSessionRepository(database)
    loaded = await restarted_repository.get_progress("session-1")

    assert loaded == expected_progress
    await restarted_repository.close()


async def test_update_never_rewrites_created_at(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "created-at.sqlite3")
    session = make_session("immutable", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW)
    await repository.create(session)
    original = (await repository.get("immutable")).created_at  # type: ignore[union-attr]

    session.created_at = NOW + timedelta(days=30)
    session.last_error = "changed"
    await repository.update(session)

    persisted = await repository.get("immutable")
    assert persisted is not None
    assert persisted.created_at == original
    assert persisted.last_error == "changed"
    await repository.close()


async def test_reset_all_removes_run_sessions_progress_and_controls(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "reset.sqlite3")
    run = RunConfig(
        run_id="old-run",
        target_url="https://staging.example.test/queue",
        requested_sessions=2,
        created_at=NOW,
    )
    await repository.create_run(run)
    await repository.create(
        make_session("one", queue_id="queue-one", status=QueueStatus.PARKED),
        QueueProgress(session_id="one", progress_percentage=10.0),
    )
    await repository.create(make_session("two"))
    await repository.set_monitoring_paused(True)
    await repository.adjust_operator_population(3)

    await repository.reset_all()

    assert await repository.get_active_run() is None
    assert await repository.get("one") is None
    assert await repository.get_progress("one") is None
    assert await repository.list() == []
    assert not await repository.is_monitoring_paused()
    assert await repository.get_operator_population_adjustment() == 0
    # Setup's empty-database guard passes again, and a queue ID can be reused.
    await repository.create_run(
        RunConfig(
            run_id="new-run",
            target_url=run.target_url,
            requested_sessions=1,
            created_at=NOW,
        )
    )
    await repository.create(make_session("three", queue_id="queue-one"))
    await repository.close()
