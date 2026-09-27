import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    MonitoringOutcome,
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionMonitor,
    is_queue_update_stale,
)
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def make_session(
    session_id: str,
    *,
    next_check_at: datetime | None = NOW,
) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=f"queue-{session_id}",
        transfer_url=f"https://queue.test/journey?q=queue-{session_id}",
        mode=SessionMode.TRANSFER_ONLY,
        status=QueueStatus.PARKED,
        state_path=Path(f".browser-state/{session_id}.json"),
        next_check_at=next_check_at,
    )


@dataclass
class ReparkingHandler:
    repository: SQLiteSessionRepository
    delay_seconds: float = 30

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        session.next_check_at = NOW + timedelta(seconds=self.delay_seconds)
        await self.repository.update(session)
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=session.next_check_at,
            queue_update_stale=False,
            progress_changed=False,
        )


class BlockingHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active_ids: set[str] = set()
        self.checked_ids: list[str] = []
        self.duplicate_check = False

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.checked_ids.append(session.session_id)
        if session.session_id in self.active_ids:
            self.duplicate_check = True
        self.active_ids.add(session.session_id)
        self.started.set()
        await self.release.wait()
        self.active_ids.remove(session.session_id)
        next_check = NOW + timedelta(seconds=30)
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=next_check,
            queue_update_stale=False,
            progress_changed=False,
        )


class SaturatedHandler:
    def __init__(self, expected_active: int) -> None:
        self.expected_active = expected_active
        self.started_count = 0
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.started_count += 1
        if self.started_count >= self.expected_active:
            self.all_started.set()
        await self.release.wait()
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=NOW + timedelta(seconds=30),
            queue_update_stale=False,
            progress_changed=False,
        )


class RecordingCycleHandler:
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self.repository = repository
        self.session_ids: list[str] = []

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.session_ids.append(session.session_id)
        session.next_check_at = NOW + timedelta(minutes=5)
        await self.repository.update(session)
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=session.next_check_at,
            queue_update_stale=False,
            progress_changed=False,
        )


class RaisingHandler:
    async def check(self, _: QueueSession) -> MonitoringOutcome:
        raise RuntimeError("synthetic monitoring failure")


class StaticRestorer:
    def __init__(self, progress: QueueProgress) -> None:
        self.progress = progress

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=self.progress,
        )


class RetryThenSuccessRestorer:
    def __init__(self) -> None:
        self.calls = 0

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        self.calls += 1
        if self.calls == 1:
            return SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=False,
                expected_queue_id=session.queue_id,
                failure=RestoreFailure.NAVIGATION_FAILED,
            )
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(session_id=session.session_id, active_queue=True),
        )


async def no_wait(_: float) -> None:
    return None


async def test_due_sessions_are_claimed_and_future_sessions_are_not(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("due-1"))
    await repository.create(make_session("due-2", next_check_at=NOW - timedelta(seconds=1)))
    await repository.create(make_session("future", next_check_at=NOW + timedelta(minutes=1)))
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=ReparkingHandler(repository),
        worker_count=1,
        queue_capacity=5,
        claim_batch_size=5,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    claimed = await scheduler.schedule_due()

    assert claimed == 2
    assert scheduler.queue_size == 2
    future = await repository.get("future")
    assert future is not None
    assert future.worker_id is None
    await repository.close()


async def test_bounded_queue_applies_backpressure_before_claiming_more(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    for index in range(5):
        await repository.create(make_session(f"session-{index}"))
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=ReparkingHandler(repository),
        worker_count=1,
        queue_capacity=2,
        claim_batch_size=2,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    assert await scheduler.schedule_due() == 2
    assert scheduler.queue_size == scheduler.queue_capacity == 2
    assert await scheduler.schedule_due() == 0
    sessions = await repository.list()
    assert sum(item.worker_id == "scheduler-1" for item in sessions) == 2
    assert scheduler.metrics.maximum_queue_depth == 2
    await repository.close()


async def test_one_hundred_persisted_sessions_claim_only_bounded_queue_capacity(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    for index in range(100):
        next_check_at = NOW if index < 80 else NOW + timedelta(minutes=5)
        await repository.create(
            make_session(f"session-{index:03d}", next_check_at=next_check_at)
        )
    observability = PrometheusMetrics()
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=ReparkingHandler(repository),
        worker_count=5,
        queue_capacity=25,
        claim_batch_size=25,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
        observability=observability,
    )

    claimed = await scheduler.schedule_due()
    sessions = await repository.list()

    assert claimed == 25
    assert scheduler.queue_size == scheduler.queue_capacity == 25
    assert await scheduler.schedule_due() == 0
    assert sum(item.worker_id == "scheduler-1" for item in sessions) == 25
    assert sum(item.worker_id is None for item in sessions) == 75
    assert scheduler.metrics.due_backlog == 55
    assert await repository.count_due_sessions(now=NOW) == 55
    assert observability.registry.get_sample_value("monitoring_queue_depth") == 25
    assert observability.registry.get_sample_value("monitoring_due_backlog") == 55
    assert observability.registry.get_sample_value("monitoring_sessions_claimed_total") == 25
    await scheduler.shutdown()
    await repository.close()


async def test_slow_workers_apply_backpressure_with_one_hundred_sessions(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "slow-workers.sqlite3")
    for index in range(100):
        await repository.create(make_session(f"session-{index:03d}"))
    handler = SaturatedHandler(expected_active=2)
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=2,
        queue_capacity=2,
        claim_batch_size=2,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    await scheduler.start()
    assert await scheduler.schedule_due() == 2
    await asyncio.wait_for(handler.all_started.wait(), timeout=1)
    assert await scheduler.schedule_due() == 2
    assert scheduler.queue_size == 2
    assert await scheduler.schedule_due() == 0

    sessions = await repository.list()
    assert scheduler.worker_task_count == 2
    assert scheduler.metrics.currently_checking == 2
    assert scheduler.metrics.maximum_concurrent_checks == 2
    assert scheduler.metrics.maximum_queue_depth == 2
    assert scheduler.metrics.due_backlog == 96
    assert sum(session.worker_id == "scheduler-1" for session in sessions) == 4

    scheduler.stop_scheduling()
    handler.release.set()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()
    assert scheduler.metrics.currently_checking == 0
    assert scheduler.queue_size == 0
    await repository.close()


async def test_one_thousand_sessions_keep_fixed_workers_and_bounded_queue(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "one-thousand.sqlite3")
    for index in range(1000):
        await repository.create(make_session(f"session-{index:04d}"))
    handler = SaturatedHandler(expected_active=5)
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=5,
        queue_capacity=50,
        claim_batch_size=50,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    await scheduler.start()
    assert scheduler.worker_task_count == 5
    assert await scheduler.schedule_due() == 50
    await asyncio.wait_for(handler.all_started.wait(), timeout=1)
    assert await scheduler.schedule_due() == 5

    sessions = await repository.list()
    assert scheduler.worker_task_count == 5
    assert scheduler.metrics.currently_checking == 5
    assert scheduler.queue_size == scheduler.queue_capacity == 50
    assert sum(session.worker_id == "scheduler-1" for session in sessions) == 55
    assert scheduler.metrics.due_backlog == 945

    scheduler.stop_scheduling()
    handler.release.set()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()
    await repository.close()


async def test_one_thousand_sessions_have_stable_repeated_bounded_cycles(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "stable-cycles.sqlite3")
    for index in range(1000):
        await repository.create(make_session(f"session-{index:04d}"))
    handler = RecordingCycleHandler(repository)
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=5,
        queue_capacity=25,
        claim_batch_size=25,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-cycles",
    )

    await scheduler.start()
    claimed_per_cycle: list[int] = []
    for _ in range(4):
        claimed_per_cycle.append(await scheduler.schedule_due())
        await scheduler.wait_until_idle()

    assert claimed_per_cycle == [25, 25, 25, 25]
    assert len(handler.session_ids) == len(set(handler.session_ids)) == 100
    assert scheduler.worker_task_count == 5
    assert scheduler.metrics.maximum_concurrent_checks <= 5
    assert scheduler.metrics.maximum_queue_depth == 25
    assert await repository.count_due_sessions(now=NOW) == 900

    await scheduler.shutdown()
    await repository.close()


async def test_fixed_worker_reparks_and_releases_lease(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("due"))
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=ReparkingHandler(repository),
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    await scheduler.start()
    assert scheduler.worker_task_count == 1
    assert await scheduler.schedule_due() == 1
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    persisted = await repository.get("due")
    assert persisted is not None
    assert persisted.next_check_at == NOW + timedelta(seconds=30)
    assert persisted.worker_id is None
    assert persisted.lease_until is None
    assert scheduler.metrics.completed == 1
    await repository.close()


async def test_active_lease_prevents_simultaneous_check_of_same_session(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("due"))
    handler = BlockingHandler()
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=2,
        queue_capacity=2,
        claim_batch_size=2,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await handler.started.wait()
    assert await scheduler.schedule_due() == 0
    handler.release.set()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    assert not handler.duplicate_check
    assert scheduler.metrics.completed == 1
    await repository.close()


async def test_expired_local_lease_is_renewed_without_duplicate_check(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "expired-local-lease.sqlite3")
    await repository.create(make_session("due"))
    handler = BlockingHandler()
    current_time = [NOW]
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=2,
        queue_capacity=2,
        claim_batch_size=2,
        lease_seconds=1,
        failure_delay_seconds=30,
        clock=lambda: current_time[0],
        scheduler_id="scheduler-1",
    )

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await handler.started.wait()
    current_time[0] = NOW + timedelta(seconds=2)

    assert await scheduler.schedule_due() == 0
    renewed = await repository.get("due")
    assert renewed is not None
    assert renewed.worker_id == "scheduler-1"
    assert renewed.lease_until == NOW + timedelta(seconds=3)
    assert scheduler.metrics.claimed == 1
    assert not handler.duplicate_check

    scheduler.stop_scheduling()
    handler.release.set()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()
    await repository.close()


async def test_shutdown_timeout_cancels_active_work_and_releases_lease(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session("due"))
    handler = BlockingHandler()
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )
    await scheduler.start()
    await scheduler.schedule_due()
    await handler.started.wait()

    await scheduler.shutdown(timeout_seconds=0.01)

    persisted = await repository.get("due")
    assert persisted is not None
    assert persisted.queue_id == "queue-due"
    assert persisted.worker_id is None
    assert persisted.lease_until is None
    assert scheduler.worker_task_count == 0
    await repository.close()


async def test_worker_exception_reparks_and_releases_lease(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "worker-failure.sqlite3")
    await repository.create(make_session("due"))
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=RaisingHandler(),
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=45,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    persisted = await repository.get("due")
    assert persisted is not None
    assert persisted.next_check_at == NOW + timedelta(seconds=45)
    assert persisted.last_error == "monitor:worker_failure"
    assert persisted.worker_id is None
    assert persisted.lease_until is None
    assert scheduler.metrics.failed == 1
    assert scheduler.metrics.checked == 1
    assert scheduler.metrics.lease_conflicts == 0
    await repository.close()


async def test_idle_scheduler_loop_waits_for_configured_tick(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "idle.sqlite3")
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=ReparkingHandler(repository),
        worker_count=2,
        queue_capacity=2,
        claim_batch_size=2,
        lease_seconds=60,
        failure_delay_seconds=30,
        scheduler_tick_seconds=0.02,
        shutdown_timeout_seconds=1,
        clock=lambda: NOW,
        scheduler_id="scheduler-1",
    )
    stop_event = asyncio.Event()

    run_task = asyncio.create_task(scheduler.run(stop_event))
    await asyncio.sleep(0.075)
    stop_event.set()
    await asyncio.wait_for(run_task, timeout=1)

    assert 2 <= scheduler.metrics.scheduler_iterations <= 5
    assert scheduler.metrics.idle_iterations == scheduler.metrics.scheduler_iterations
    assert scheduler.metrics.claimed == 0
    assert scheduler.worker_task_count == 0
    await repository.close()


async def test_pause_finishes_in_flight_check_and_releases_queued_claims(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "pause-drain.sqlite3")
    for index in range(3):
        await repository.create(make_session(f"due-{index}"))
    handler = BlockingHandler()
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=1,
        queue_capacity=3,
        claim_batch_size=3,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="pause-scheduler",
    )

    await scheduler.start()
    assert await scheduler.schedule_due() == 3
    await handler.started.wait()
    await scheduler.pause_monitoring()
    assert await scheduler.monitoring_paused()

    handler.release.set()
    await scheduler.wait_until_idle()

    assert len(handler.checked_ids) == 1
    assert scheduler.metrics.completed == 1
    assert scheduler.metrics.skipped_while_paused == 2
    for index in range(3):
        persisted = await repository.get(f"due-{index}")
        assert persisted is not None
        assert persisted.status is QueueStatus.PARKED
        assert persisted.queue_id == f"queue-due-{index}"
        assert persisted.transfer_url.endswith(f"queue-due-{index}")
        assert persisted.state_path == Path(f".browser-state/due-{index}.json")
        assert persisted.next_check_at == NOW
        assert persisted.worker_id is None
        assert persisted.lease_until is None
    assert (await repository.due_session_summary(now=NOW)).count == 3

    await scheduler.resume_monitoring()
    assert not await scheduler.monitoring_paused()
    assert await scheduler.schedule_due() == 3
    await scheduler.wait_until_idle()
    assert len(handler.checked_ids) == 4
    await scheduler.shutdown()
    await repository.close()


async def test_scheduler_stays_idle_while_persisted_pause_is_active_then_wakes(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "paused-loop.sqlite3")
    await repository.create(make_session("due"))
    await repository.set_monitoring_paused(True)
    handler = RecordingCycleHandler(repository)
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=30,
        scheduler_tick_seconds=0.5,
        clock=lambda: NOW,
        scheduler_id="paused-loop",
    )
    stop_event = asyncio.Event()
    run_task = asyncio.create_task(scheduler.run(stop_event))

    await asyncio.sleep(0.05)
    assert scheduler.metrics.scheduler_iterations == 0
    assert handler.session_ids == []
    assert (await repository.due_session_summary(now=NOW)).count == 1

    await scheduler.resume_monitoring()
    await asyncio.wait_for(_wait_for_checks(handler, 1), timeout=1)
    stop_event.set()
    await asyncio.wait_for(run_task, timeout=1)

    assert handler.session_ids == ["due"]
    assert scheduler.worker_task_count == 0
    await repository.close()


async def _wait_for_checks(handler: RecordingCycleHandler, expected: int) -> None:
    while len(handler.session_ids) < expected:
        await asyncio.sleep(0)


class RecordingControlRepository(SQLiteSessionRepository):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.control_writes: list[bool] = []

    async def set_monitoring_paused(self, paused: bool) -> bool:
        result = await super().set_monitoring_paused(paused)
        self.control_writes.append(result)
        return result


async def test_pause_resume_are_idempotent_and_concurrent_races_are_linearized(
    tmp_path: Path,
) -> None:
    repository = RecordingControlRepository(tmp_path / "control-race.sqlite3")
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=RecordingCycleHandler(repository),
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=30,
    )

    await asyncio.gather(*(scheduler.pause_monitoring() for _ in range(8)))
    assert await repository.is_monitoring_paused()
    await asyncio.gather(*(scheduler.resume_monitoring() for _ in range(8)))
    assert not await repository.is_monitoring_paused()

    await asyncio.gather(
        scheduler.pause_monitoring(),
        scheduler.resume_monitoring(),
        scheduler.pause_monitoring(),
        scheduler.resume_monitoring(),
    )
    assert await repository.is_monitoring_paused() is repository.control_writes[-1]
    assert len(repository.control_writes) == 20
    await scheduler.shutdown()
    await repository.close()


def test_adaptive_intervals_follow_lifecycle_ranges() -> None:
    policy = PollingPolicy(jitter_seconds=5)
    early = QueueProgress(session_id="s", active_queue=True, progress_percentage=10)
    mid = QueueProgress(session_id="s", active_queue=True, progress_percentage=70)

    assert 60 <= policy.interval_seconds(QueueStatus.PRE_QUEUE, None, jitter=lambda *_: 0) <= 300
    assert (
        60 <= policy.interval_seconds(QueueStatus.ACTIVE_QUEUE, early, jitter=lambda *_: 0) <= 120
    )
    assert 30 <= policy.interval_seconds(QueueStatus.ACTIVE_QUEUE, mid, jitter=lambda *_: 0) <= 60
    assert 10 <= policy.interval_seconds(QueueStatus.SERVICED_SOON, None, jitter=lambda *_: 0) <= 30
    assert policy.interval_seconds(QueueStatus.TURN_STARTED, None) == 0


def test_poll_jitter_spreads_sessions_without_leaving_configured_range() -> None:
    policy = PollingPolicy(jitter_seconds=5)

    low = policy.interval_seconds(QueueStatus.SERVICED_SOON, None, jitter=lambda *_: -5)
    high = policy.interval_seconds(QueueStatus.SERVICED_SOON, None, jitter=lambda *_: 5)

    assert low == 15
    assert high == 25


def test_poll_jitter_distributes_one_thousand_next_check_intervals() -> None:
    policy = PollingPolicy(default_seconds=30, jitter_seconds=5)
    generator = random.Random(20260926)

    intervals = [
        policy.interval_seconds(
            QueueStatus.PARKED,
            None,
            jitter=generator.uniform,
        )
        for _ in range(1000)
    ]
    buckets: dict[int, int] = {}
    for interval in intervals:
        bucket = int(interval)
        buckets[bucket] = buckets.get(bucket, 0) + 1

    assert min(intervals) >= 25
    assert max(intervals) <= 35
    assert len(set(intervals)) > 900
    assert max(buckets.values()) < 150


async def test_monitor_retries_transient_restore_failure_then_succeeds(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "retry-monitor.sqlite3")
    parked = make_session("retry")
    await repository.create(parked)
    restorer = RetryThenSuccessRestorer()
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=MonitoringRetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0,
            maximum_backoff_seconds=0,
            jitter_seconds=0,
        ),
        clock=lambda: NOW,
        sleep=no_wait,
    )

    outcome = await monitor.check(parked)

    assert outcome.success
    assert restorer.calls == 2
    persisted = await repository.get(parked.session_id)
    assert persisted is not None
    assert persisted.status is QueueStatus.ACTIVE_QUEUE
    assert persisted.next_check_at == NOW + timedelta(seconds=90)
    await repository.close()


async def test_monitor_tracks_updates_changes_staleness_and_next_check(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    parked = make_session("session-1")
    parked.last_progress_change_at = NOW - timedelta(minutes=5)
    previous = QueueProgress(
        session_id=parked.session_id,
        queue_number="100",
        users_ahead=50,
        progress_percentage=25,
        last_updated_at=NOW - timedelta(minutes=4),
        active_queue=True,
    )
    await repository.create(parked, previous)
    current = QueueProgress(
        session_id=parked.session_id,
        queue_number="100",
        users_ahead=50,
        progress_percentage=25,
        last_updated_at=NOW - timedelta(seconds=10),
        active_queue=True,
    )
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=StaticRestorer(current),
        polling_policy=PollingPolicy(jitter_seconds=0, stale_update_seconds=60),
        clock=lambda: NOW,
    )

    outcome = await monitor.check(parked)

    assert outcome.success
    assert not outcome.progress_changed
    assert not outcome.queue_update_stale
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    persisted = await repository.get(parked.session_id)
    assert persisted is not None
    assert persisted.last_queue_update == NOW - timedelta(seconds=10)
    assert persisted.last_progress_change_at == NOW - timedelta(minutes=5)
    assert persisted.next_check_at == NOW + timedelta(seconds=90)
    await repository.close()


def test_stale_update_detection_does_not_treat_missing_timestamp_as_failure() -> None:
    assert not is_queue_update_stale(None, now=NOW, stale_after_seconds=60)
    assert not is_queue_update_stale(
        NOW - timedelta(seconds=30),
        now=NOW,
        stale_after_seconds=60,
    )
    assert is_queue_update_stale(
        NOW - timedelta(seconds=61),
        now=NOW,
        stale_after_seconds=60,
    )


async def test_shutdown_tolerates_workers_cancelled_from_outside(tmp_path: Path) -> None:
    """Regression: a runtime deadline cancelling run() cancels every worker; the
    following shutdown step must still finish and release the in-flight lease."""

    repository = SQLiteSessionRepository(tmp_path / "cancelled-workers.sqlite3")
    await repository.create(make_session("due-0"))
    handler = BlockingHandler()
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=2,
        queue_capacity=2,
        claim_batch_size=2,
        lease_seconds=60,
        failure_delay_seconds=30,
        clock=lambda: NOW,
        scheduler_id="cancelled-scheduler",
    )
    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await handler.started.wait()
    for worker in scheduler._workers:
        worker.cancel()
    await asyncio.sleep(0.05)

    await asyncio.wait_for(scheduler.shutdown(timeout_seconds=1), timeout=5)

    persisted = await repository.get("due-0")
    assert persisted is not None and persisted.worker_id is None
    assert persisted.queue_id == "queue-due-0"
    await repository.close()
