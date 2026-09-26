from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import (
    QueueIdConflictError,
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
    session.next_check_at = NOW + timedelta(seconds=10)
    session.attempt_count = 1
    await repository.update(session)

    loaded = await repository.get(session.session_id)
    assert loaded is not None
    assert loaded.status is QueueStatus.CREATING
    assert loaded.queue_id == "queue-1"
    assert loaded.last_checked_at == NOW
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
    await repository.create(
        make_session("due", status=QueueStatus.ACTIVE_QUEUE, next_check_at=NOW)
    )
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
    await repository.close()


async def test_release_lease_checks_owner_when_provided(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("session-1", next_check_at=NOW))
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
