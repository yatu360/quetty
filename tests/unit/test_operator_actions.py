import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import CreationOutcome, CreationOutcomeKind, CreationWorkItem
from queue_load_test.state import FileSystemStateStore
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)


class Target:
    def __init__(self) -> None:
        self.adjustments: list[int] = []

    def adjust_target(self, delta: int) -> None:
        self.adjustments.append(delta)


class Creator:
    def __init__(self, repository: SQLiteSessionRepository, *, fail: bool = False) -> None:
        self.repository = repository
        self.fail = fail
        self.calls = 0

    async def create(self, item: CreationWorkItem) -> CreationOutcome:
        self.calls += 1
        session_id = item.session_id
        if self.fail:
            await self.repository.create(
                QueueSession(
                    session_id=session_id,
                    status=QueueStatus.FAILED,
                    state_path=Path(f"{session_id}.json"),
                )
            )
            return CreationOutcome(
                kind=CreationOutcomeKind.TEMPORARY_FAILURE,
                attempts=3,
                temporary_failures=3,
                duration_seconds=0,
            )
        session = QueueSession(
            session_id=session_id,
            queue_id=f"queue-{self.calls}",
            transfer_url="https://example.invalid/transfer",
            mode=SessionMode.HYBRID,
            status=QueueStatus.PARKED,
            state_path=Path(f"{session_id}.json"),
        )
        await self.repository.create(session)
        return CreationOutcome(
            kind=CreationOutcomeKind.SUCCESS,
            attempts=1,
            temporary_failures=0,
            duration_seconds=0,
            session=session,
        )


class BlockingCreator(Creator):
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        super().__init__(repository)
        self.gate = asyncio.Event()
        self.two_started = asyncio.Event()
        self.running = 0
        self.maximum_running = 0

    async def create(self, item: CreationWorkItem) -> CreationOutcome:
        self.running += 1
        self.maximum_running = max(self.maximum_running, self.running)
        if self.running == 2:
            self.two_started.set()
        try:
            await self.gate.wait()
            return await super().create(item)
        finally:
            self.running -= 1


class Monitor:
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self.repository = repository
        self.calls = 0

    async def check(self, session: QueueSession) -> object:
        self.calls += 1
        session.status = QueueStatus.ACTIVE_QUEUE
        session.last_error = None
        progress = QueueProgress(
            session_id=session.session_id,
            progress_percentage=55,
            active_queue=True,
        )
        await self.repository.update(session, progress)
        return SimpleNamespace(success=True)


def session(session_id: str = "old", queue_id: str = "expected") -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=queue_id,
        transfer_url="https://example.invalid/transfer",
        mode=SessionMode.HYBRID,
        status=QueueStatus.PARKED,
        state_path=Path(f"{session_id}.json"),
    )


async def wait_for_action(
    manager: OperatorActionManager, session_id: str | None
) -> OperatorActionStatus:
    for _ in range(100):
        action = manager.latest_add() if session_id is None else manager.for_session(session_id)
        if action is not None and action.status in {
            OperatorActionStatus.SUCCESS,
            OperatorActionStatus.FAILED,
        }:
            return action.status
        await asyncio.sleep(0.01)
    raise AssertionError("operator action did not finish")


async def manager_for(
    tmp_path: Path,
    repository: SQLiteSessionRepository,
    *,
    creator: Creator | None = None,
    workers: int = 1,
    capacity: int = 2,
) -> tuple[OperatorActionManager, Target, Monitor, FileSystemStateStore]:
    target = Target()
    monitor = Monitor(repository)
    state_store = FileSystemStateStore(tmp_path / "state")
    manager = OperatorActionManager(
        repository=repository,
        creator=creator or Creator(repository),
        monitor=monitor,  # type: ignore[arg-type]
        state_store=state_store,
        target_adjustment=target,
        worker_count=workers,
        queue_capacity=capacity,
        lease_seconds=30,
    )
    await manager.start()
    return manager, target, monitor, state_store


@pytest.mark.asyncio
async def test_refresh_runs_while_paused_and_persists_progress(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "actions.sqlite3")
    await repository.initialize()
    await repository.create(session())
    await repository.set_monitoring_paused(True)
    manager, _, monitor, _ = await manager_for(tmp_path, repository)

    requested = await manager.request(OperatorActionKind.REFRESH, "old")
    assert requested.status is OperatorActionStatus.REQUESTED
    assert await wait_for_action(manager, "old") is OperatorActionStatus.SUCCESS
    persisted = await repository.get("old")
    assert persisted is not None
    assert persisted.queue_id == "expected"
    assert persisted.status is QueueStatus.ACTIVE_QUEUE
    assert persisted.worker_id is None
    assert (await repository.get_progress("old")).progress_percentage == 55  # type: ignore[union-attr]
    assert monitor.calls == 1
    await manager.close()
    await repository.close()


@pytest.mark.asyncio
async def test_refresh_refuses_automatic_and_manual_ownership(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "busy.sqlite3")
    await repository.initialize()
    await repository.create(session())
    claimed = await repository.claim_due_sessions(
        worker_id="automatic",
        now=session().created_at,
        lease_until=session().created_at.replace(year=session().created_at.year + 1),
        limit=1,
    )
    assert claimed
    manager, _, _, _ = await manager_for(tmp_path, repository)
    busy = await manager.request(OperatorActionKind.REFRESH, "old")
    assert busy.status is OperatorActionStatus.FAILED
    assert "checked" in busy.message
    await repository.release_lease("old", worker_id="automatic")
    now = session().created_at
    await repository.acquire_manual_ownership(
        "old",
        owner_id="headed",
        now=now,
        lease_until=now.replace(year=now.year + 1),
        capacity=1,
    )
    open_busy = await manager.request(OperatorActionKind.REFRESH, "old")
    assert open_busy.status is OperatorActionStatus.FAILED
    assert "Close" in open_busy.message
    await manager.close()
    await repository.close()


@pytest.mark.asyncio
async def test_delete_removes_row_progress_state_and_prevents_restart_refill(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "delete.sqlite3")
    await repository.initialize()
    await repository.create(session(), QueueProgress(session_id="old", progress_percentage=10))
    manager, target, _, store = await manager_for(tmp_path, repository)
    await store.save("old", {"cookies": [], "origins": []})

    await manager.request(OperatorActionKind.DELETE, "old")
    assert await wait_for_action(manager, "old") is OperatorActionStatus.SUCCESS
    assert await repository.get("old") is None
    assert await repository.get_progress("old") is None
    assert await store.load("old") is None
    assert await repository.get_operator_population_adjustment() == -1
    assert target.adjustments == [-1]
    repeated = await manager.request(OperatorActionKind.DELETE, "old")
    assert repeated.status is OperatorActionStatus.SUCCESS
    await manager.close()
    await repository.close()


@pytest.mark.asyncio
async def test_replace_keeps_old_on_failure_and_swaps_after_success(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "replace.sqlite3")
    await repository.initialize()
    await repository.create(session())
    failed_manager, _, _, _ = await manager_for(
        tmp_path, repository, creator=Creator(repository, fail=True)
    )
    await failed_manager.request(OperatorActionKind.REPLACE, "old")
    assert await wait_for_action(failed_manager, "old") is OperatorActionStatus.FAILED
    assert (await repository.get("old")).queue_id == "expected"  # type: ignore[union-attr]
    assert await repository.count_successful_queue_ids() == 1
    await failed_manager.close()

    manager, target, _, _ = await manager_for(tmp_path, repository)
    await manager.request(OperatorActionKind.REPLACE, "old")
    assert await wait_for_action(manager, "old") is OperatorActionStatus.SUCCESS
    assert await repository.get("old") is None
    assert await repository.count_successful_queue_ids() == 1
    assert target.adjustments == []
    await manager.close()
    await repository.close()


@pytest.mark.asyncio
async def test_add_creates_exactly_one_without_changing_requested_target(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "add.sqlite3")
    await repository.initialize()
    manager, target, _, _ = await manager_for(tmp_path, repository)

    await manager.request(OperatorActionKind.ADD)
    assert await wait_for_action(manager, None) is OperatorActionStatus.SUCCESS
    assert await repository.count_successful_queue_ids() == 1
    assert await repository.get_operator_population_adjustment() == 1
    assert target.adjustments == [1]
    await manager.close()
    await repository.close()


@pytest.mark.asyncio
async def test_concurrent_adds_use_fixed_workers_and_bounded_queue(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "bounded-add.sqlite3")
    await repository.initialize()
    creator = BlockingCreator(repository)
    manager, _, _, _ = await manager_for(
        tmp_path,
        repository,
        creator=creator,
        workers=2,
        capacity=1,
    )

    first = await manager.request(OperatorActionKind.ADD)
    while creator.running < 1:
        await asyncio.sleep(0)
    second = await manager.request(OperatorActionKind.ADD)
    await asyncio.wait_for(creator.two_started.wait(), timeout=1)
    third = await manager.request(OperatorActionKind.ADD)
    rejected = await manager.request(OperatorActionKind.ADD)
    assert first.status is OperatorActionStatus.REQUESTED
    assert second.status is OperatorActionStatus.REQUESTED
    assert third.status is OperatorActionStatus.REQUESTED
    assert rejected.status is OperatorActionStatus.FAILED
    assert rejected.message == "Operator work capacity unavailable"
    creator.gate.set()
    await asyncio.wait_for(manager._queue.join(), timeout=1)
    assert creator.maximum_running == 2
    assert await repository.count_successful_queue_ids() == 3
    await manager.close()
    await repository.close()
