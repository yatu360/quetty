import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.runtime import ApplicationRuntime
from queue_load_test.scheduler import MonitoringOutcome, ParkedSessionScheduler

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


class TrackingRepository(SQLiteSessionRepository):
    def __init__(self, database: Path, order: list[str]) -> None:
        super().__init__(database)
        self.order = order
        self.list_calls = 0

    async def list(self, status: QueueStatus | None = None) -> list[QueueSession]:
        self.list_calls += 1
        return await super().list(status)

    async def close(self) -> None:
        self.order.append("database")
        await super().close()


class FakeBrowserManager:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.stopped = True
        self.order.append("browser")


class ActiveHandler:
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self.repository = repository
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.started.set()
        await self.finish.wait()
        session.last_checked_at = NOW
        session.next_check_at = NOW + timedelta(seconds=30)
        await self.repository.update(session)
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=session.next_check_at,
            queue_update_stale=False,
            progress_changed=False,
        )


class StoppableCreation:
    def __init__(self) -> None:
        self.stopped = False

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        assert stop_event is not None
        await stop_event.wait()
        self.stopped = True


async def test_shutdown_finishes_active_work_and_preserves_restart_state(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    database = tmp_path / "sessions.sqlite3"
    repository = TrackingRepository(database, order)
    session = QueueSession(
        session_id="session-1",
        queue_id="queue-expected",
        transfer_url="https://queue.test/journey?q=queue-expected",
        mode=SessionMode.HYBRID,
        status=QueueStatus.PARKED,
        state_path=tmp_path / "state" / "session-1.json",
        next_check_at=NOW,
    )
    await repository.create(session)
    handler = ActiveHandler(repository)
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=30,
        scheduler_tick_seconds=0.01,
        shutdown_timeout_seconds=1,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )
    browser = FakeBrowserManager(order)
    creation = StoppableCreation()
    runtime = ApplicationRuntime(
        browser_manager=cast(BrowserManager, browser),
        repository=repository,
        monitoring_scheduler=scheduler,
        creation_runner=creation,
        shutdown_timeout_seconds=1,
    )

    runtime_task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    runtime.request_shutdown()
    handler.finish.set()
    await asyncio.wait_for(runtime_task, timeout=2)

    assert browser.started
    assert browser.stopped
    assert creation.stopped
    assert repository.list_calls == 0
    assert runtime.startup_recovery_summary is not None
    assert runtime.startup_recovery_summary.total_persisted_sessions == 1
    assert runtime.startup_recovery_summary.valid_queue_ids == 1
    assert not runtime.startup_recovery_summary.state_scan_performed
    assert order[-2:] == ["browser", "database"]

    restarted = SQLiteSessionRepository(database)
    recovered = await restarted.get(session.session_id)
    assert recovered is not None
    assert recovered.queue_id == "queue-expected"
    assert recovered.transfer_url == session.transfer_url
    assert recovered.last_checked_at == NOW
    assert recovered.worker_id is None
    assert recovered.lease_until is None
    await restarted.close()
