import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import PollingPolicy, QueueSessionMonitor
from queue_load_test.transfer import (
    OpenedSessionRestore,
    QueueSessionRestorer,
    RestoreAttempt,
    RestoreFailure,
    RestoreMethod,
    SessionRestoreResult,
)
from queue_load_test.web.manual import ManualChromeSessionManager, ManualOpenError


class EventSource:
    def __init__(self) -> None:
        self.callbacks: dict[str, list[Any]] = {}

    def on(self, event: str, callback: Any) -> None:
        self.callbacks.setdefault(event, []).append(callback)

    def emit(self, event: str) -> None:
        for callback in self.callbacks.get(event, []):
            callback(self)


class FakePage(EventSource):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def crash_close(self) -> None:
        self.closed = True
        self.emit("close")


class FakeContext(EventSource):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False


class FakeOwnedContext:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.closed = False
        self.browser_id = 0

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.context.closed = True
        self.context.emit("close")


class FakeHeadedManager:
    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0

    async def start(self) -> None:
        self.started = True

    async def capacity(self, *, repair: bool = True) -> object:
        return SimpleNamespace(processes=[SimpleNamespace(index=0, connected=True)])

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.started = False


class FakeOpenRestorer:
    def __init__(self, *, failure: RestoreFailure | None = None) -> None:
        self.failure = failure
        self.opened: list[FakeOwnedContext] = []
        self.pages: list[FakePage] = []
        self.inspections = 0
        self.adoptable_queue_id: str | None = None
        self.adoptions = 0
        self.discarded: list[str] = []

    async def restore_open(self, session: QueueSession) -> OpenedSessionRestore:
        if self.failure is not None:
            attempt = RestoreAttempt(
                method=RestoreMethod.TRANSFER,
                success=False,
                observed_queue_id="unexpected" if self.failure is RestoreFailure.IDENTITY_MISMATCH else None,
                identity_match=False if self.failure is RestoreFailure.IDENTITY_MISMATCH else None,
                failure=self.failure,
            )
            return OpenedSessionRestore(
                SessionRestoreResult(
                    method=attempt.method,
                    success=False,
                    expected_queue_id=session.queue_id,
                    observed_queue_id=attempt.observed_queue_id,
                    identity_match=attempt.identity_match,
                    failure=attempt.failure,
                    attempts=(attempt,),
                )
            )
        owned = FakeOwnedContext()
        page = FakePage()
        self.opened.append(owned)
        self.pages.append(page)
        result = SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
        )
        return OpenedSessionRestore(
            result,
            cast(Any, owned),
            cast(Any, page),
            identity_pending=session.queue_id is None,
        )

    async def adopt_open(self, session: QueueSession, **_: object) -> SessionRestoreResult | None:
        self.adoptions += 1
        if self.adoptable_queue_id is None:
            return None
        session.queue_id = self.adoptable_queue_id
        session.transfer_url = f"https://queue.test/journey?q={self.adoptable_queue_id}"
        session.last_error = None
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            state_refreshed=True,
            progress=QueueProgress(
                session_id=session.session_id,
                queue_number="42",
                active_queue=True,
            ),
        )

    async def discard_state(self, session: QueueSession) -> None:
        self.discarded.append(session.session_id)

    async def inspect_open(self, session: QueueSession, **_: object) -> SessionRestoreResult:
        self.inspections += 1
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(
                session_id=session.session_id,
                progress_percentage=91.0,
                serviced_soon=True,
                active_queue=True,
            ),
        )


class UnusedRestorer:
    async def restore(self, _: QueueSession) -> SessionRestoreResult:
        raise AssertionError("manual finalization supplies its existing observation")


def make_session(session_id: str) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=f"queue-{session_id}",
        transfer_url=f"https://queue.test/journey?q=queue-{session_id}",
        mode=SessionMode.HYBRID,
        status=QueueStatus.ACTIVE_QUEUE,
        state_path=Path(f"state/{session_id}.json"),
    )


async def make_manager(
    tmp_path: Path,
    *,
    session_count: int = 1,
    capacity: int = 2,
    failure: RestoreFailure | None = None,
    lease_seconds: float = 30,
) -> tuple[
    ManualChromeSessionManager,
    SQLiteSessionRepository,
    FakeOpenRestorer,
    FakeHeadedManager,
]:
    repository = SQLiteSessionRepository(tmp_path / "manual.sqlite3")
    for index in range(session_count):
        await repository.create(make_session(f"session-{index}"))
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=UnusedRestorer(),
        polling_policy=PollingPolicy(jitter_seconds=0),
    )
    restorer = FakeOpenRestorer(failure=failure)
    headed = FakeHeadedManager()
    manager = ManualChromeSessionManager(
        repository=repository,
        browser_manager=cast(BrowserManager, headed),
        restorer=cast(QueueSessionRestorer, restorer),
        monitor=monitor,
        capacity=capacity,
        lease_seconds=lease_seconds,
    )
    return manager, repository, restorer, headed


async def test_repeated_open_is_safe_and_close_persists_latest_progress(
    tmp_path: Path,
) -> None:
    manager, repository, restorer, _ = await make_manager(tmp_path)

    first = await manager.open("session-0")
    repeated = await manager.open("session-0")
    opened = await repository.get("session-0")

    assert first.status.value == "OPENED"
    assert repeated.status.value == "ALREADY_OPEN"
    assert len(restorer.opened) == 1
    assert opened is not None and opened.manual_owner_id is not None

    assert await manager.close_session("session-0")
    persisted = await repository.get("session-0")
    progress = await repository.get_progress("session-0")
    assert persisted is not None
    assert persisted.manual_owner_id is None
    assert persisted.status is QueueStatus.SERVICED_SOON
    assert persisted.next_check_at is not None
    assert progress is not None and progress.progress_percentage == 91.0
    assert restorer.inspections == 1
    assert restorer.opened[0].closed
    await manager.close()
    await repository.close()


async def test_two_manual_sessions_open_and_one_above_limit_is_rejected(
    tmp_path: Path,
) -> None:
    manager, repository, restorer, _ = await make_manager(
        tmp_path,
        session_count=3,
        capacity=2,
    )

    await asyncio.gather(manager.open("session-0"), manager.open("session-1"))
    with pytest.raises(ManualOpenError, match="Browser capacity currently unavailable"):
        await manager.open("session-2")

    assert len(restorer.opened) == 2
    await manager.close()
    for index in range(3):
        persisted = await repository.get(f"session-{index}")
        assert persisted is not None and persisted.manual_owner_id is None
    await repository.close()


async def test_identity_mismatch_preserves_expected_id_and_cleans_up(tmp_path: Path) -> None:
    manager, repository, _, _ = await make_manager(
        tmp_path,
        failure=RestoreFailure.IDENTITY_MISMATCH,
    )

    with pytest.raises(ManualOpenError, match="Identity Mismatch"):
        await manager.open("session-0")

    persisted = await repository.get("session-0")
    assert persisted is not None
    assert persisted.queue_id == "queue-session-0"
    assert persisted.manual_owner_id is None
    await manager.close()
    await repository.close()


async def test_page_close_and_application_shutdown_release_all_ownership(
    tmp_path: Path,
) -> None:
    manager, repository, restorer, headed = await make_manager(tmp_path, session_count=2)
    await manager.open("session-0")
    await manager.open("session-1")

    restorer.pages[0].crash_close()
    for _ in range(20):
        persisted = await repository.get("session-0")
        if persisted is not None and persisted.manual_owner_id is None:
            break
        await asyncio.sleep(0)
    assert persisted is not None and persisted.manual_owner_id is None
    # The page was already gone, so failure to inspect preserves the last status.
    assert persisted.status is QueueStatus.ACTIVE_QUEUE

    await manager.close()
    second = await repository.get("session-1")
    assert second is not None and second.manual_owner_id is None
    assert headed.shutdown_calls == 1
    assert all(context.closed for context in restorer.opened)
    await repository.close()



async def create_unidentified(repository: SQLiteSessionRepository, session_id: str) -> None:
    await repository.create(
        QueueSession(
            session_id=session_id,
            queue_id=None,
            transfer_url="",
            mode=SessionMode.HYBRID,
            status=QueueStatus.FAILED,
            state_path=Path(f"state/{session_id}.json"),
            last_error="queue_identity_missing",
        )
    )


async def test_session_without_queue_id_adopts_identity_on_heartbeat(tmp_path: Path) -> None:
    manager, repository, restorer, _ = await make_manager(
        tmp_path, session_count=0, lease_seconds=3
    )
    await create_unidentified(repository, "no-id")

    opened = await manager.open("no-id")
    assert opened.status.value == "OPENED"
    assert "no Queue ID yet" in opened.message

    restorer.adoptable_queue_id = "queue-adopted"
    for _ in range(40):
        persisted = await repository.get("no-id")
        if persisted is not None and persisted.queue_id is not None:
            break
        await asyncio.sleep(0.1)
    assert persisted is not None
    assert persisted.queue_id == "queue-adopted"
    assert persisted.transfer_url == "https://queue.test/journey?q=queue-adopted"
    assert persisted.status is QueueStatus.PARKED
    assert persisted.last_error is None
    assert persisted.next_check_at is not None
    assert persisted.manual_owner_id is not None
    progress = await repository.get_progress("no-id")
    assert progress is not None and progress.queue_number == "42"

    # Once adopted, closing uses the normal verified inspection.
    assert await manager.close_session("no-id")
    assert restorer.inspections == 1
    closed = await repository.get("no-id")
    assert closed is not None and closed.manual_owner_id is None
    assert closed.status is QueueStatus.SERVICED_SOON
    await manager.close()
    await repository.close()


async def test_closing_before_queue_id_appears_leaves_row_unchanged(tmp_path: Path) -> None:
    manager, repository, restorer, _ = await make_manager(tmp_path, session_count=0)
    await create_unidentified(repository, "no-id")

    await manager.open("no-id")
    assert await manager.close_session("no-id")

    persisted = await repository.get("no-id")
    assert persisted is not None
    assert persisted.queue_id is None
    assert persisted.status is QueueStatus.FAILED
    assert persisted.last_error == "queue_identity_missing"
    assert persisted.manual_owner_id is None
    assert restorer.adoptions == 1
    assert restorer.inspections == 0
    assert restorer.opened[0].closed
    await manager.close()
    await repository.close()


async def test_adopting_duplicate_queue_id_is_rejected_and_state_discarded(
    tmp_path: Path,
) -> None:
    manager, repository, restorer, _ = await make_manager(tmp_path, session_count=1)
    await create_unidentified(repository, "no-id")

    await manager.open("no-id")
    restorer.adoptable_queue_id = "queue-session-0"
    assert await manager.close_session("no-id")

    persisted = await repository.get("no-id")
    assert persisted is not None
    assert persisted.queue_id is None
    assert persisted.status is QueueStatus.FAILED
    assert persisted.manual_owner_id is None
    assert restorer.discarded == ["no-id"]
    original = await repository.get("session-0")
    assert original is not None and original.queue_id == "queue-session-0"
    await manager.close()
    await repository.close()
