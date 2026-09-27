import asyncio
import hashlib
import io
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from queue_load_test.browser import BrowserCapacity, BrowserManager
from queue_load_test.config import Settings
from queue_load_test.metrics import JsonLogFormatter, PrometheusMetrics, log_event
from queue_load_test.metrics.prometheus import FORBIDDEN_LABEL_NAMES
from queue_load_test.metrics.status import StatusSummaryProvider
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import (
    PROGRESS_BUCKETS,
    ClaimedSessions,
    DueSessionSummary,
    LeaseOwnershipError,
    SQLiteSessionRepository,
)
from queue_load_test.runtime import ApplicationRuntime
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
    MonitoringOutcome,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionMonitor,
    SessionCreationController,
)
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def make_session(
    index: int,
    *,
    status: QueueStatus = QueueStatus.PARKED,
    queue_id: str | None = "default",
    worker_id: str | None = None,
    lease_until: datetime | None = None,
    next_check_at: datetime | None = NOW - timedelta(seconds=5),
) -> QueueSession:
    return QueueSession(
        session_id=f"session-{index:05d}",
        queue_id=f"queue-{index:05d}" if queue_id == "default" else queue_id,
        transfer_url=f"https://queue.test/journey?q=queue-{index:05d}",
        mode=SessionMode.HYBRID,
        status=status,
        state_path=Path(f".browser-state/session-{index:05d}.json"),
        created_at=NOW - timedelta(hours=1, microseconds=index),
        next_check_at=next_check_at,
        worker_id=worker_id,
        lease_until=lease_until,
    )


async def identity_digest(repository: SQLiteSessionRepository) -> str:
    digest = hashlib.sha256()
    for session in await repository.list():
        digest.update(
            f"{session.session_id}\0{session.queue_id}\0{session.transfer_url}\n".encode()
        )
    return digest.hexdigest()


class RecordingHandler:
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self.repository = repository
        self.checked: list[str] = []

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.checked.append(session.session_id)
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


def scheduler_for(
    repository: SQLiteSessionRepository,
    handler: Any,
    *,
    scheduler_id: str = "recovery-scheduler",
    observability: PrometheusMetrics | None = None,
    clock: Any = None,
) -> ParkedSessionScheduler:
    return ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=4,
        queue_capacity=20,
        claim_batch_size=20,
        lease_seconds=60,
        failure_delay_seconds=30,
        scheduler_tick_seconds=0.01,
        shutdown_timeout_seconds=1,
        clock=clock or (lambda: NOW),
        scheduler_id=scheduler_id,
        observability=observability,
    )


# --- recovery accounting -------------------------------------------------------------


async def test_recovery_summary_accounts_lost_identities_and_leases(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    for index in range(10):
        await repository.create(make_session(index))
    await repository.create(make_session(10, status=QueueStatus.FAILED))
    await repository.create(make_session(11, status=QueueStatus.FAILED, queue_id=None))
    await repository.create(
        make_session(12, worker_id="dead-worker", lease_until=NOW - timedelta(seconds=1))
    )
    await repository.create(
        make_session(13, worker_id="live-worker", lease_until=NOW + timedelta(seconds=30))
    )
    await repository.create(make_session(14, status=QueueStatus.ADMITTED, next_check_at=None))

    summary = await repository.recovery_summary(now=NOW)

    assert summary.total_persisted_sessions == 15
    assert summary.valid_queue_ids == 13
    assert summary.lost_queue_ids == 1
    assert await repository.count_lost_queue_ids() == 1
    assert summary.expired_leases == 1
    assert summary.leased_sessions == 1
    assert summary.terminal_sessions == 3
    await repository.close()


async def test_claim_counts_only_expired_leases_as_recovered(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    for index in range(5):
        await repository.create(make_session(index))
    for index in range(5, 8):
        await repository.create(
            make_session(
                index,
                worker_id="crashed-worker",
                lease_until=NOW - timedelta(seconds=1),
            )
        )
    await repository.create(
        make_session(8, worker_id="live-worker", lease_until=NOW + timedelta(seconds=30))
    )

    claimed = await repository.claim_due_sessions(
        worker_id="new-owner",
        now=NOW,
        lease_until=NOW + timedelta(seconds=60),
        limit=50,
    )

    assert isinstance(claimed, ClaimedSessions)
    assert len(claimed) == 8
    assert claimed.recovered_expired_leases == 3
    assert all(session.worker_id == "new-owner" for session in claimed)
    live = await repository.get("session-00008")
    assert live is not None and live.worker_id == "live-worker"
    await repository.close()


async def test_scheduler_reports_lease_recoveries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    for index in range(6):
        await repository.create(
            make_session(
                index,
                worker_id="crashed-worker",
                lease_until=NOW - timedelta(seconds=1),
            )
        )
    metrics = PrometheusMetrics()
    handler = RecordingHandler(repository)
    scheduler = scheduler_for(repository, handler, observability=metrics)
    await scheduler.start()

    with caplog.at_level(logging.WARNING):
        assert await scheduler.schedule_due() == 6
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    assert scheduler.metrics.expired_leases_recovered == 6
    assert metrics.registry.get_sample_value("monitoring_lease_recoveries_total") == 6
    recovered = [record for record in caplog.records if record.msg == "expired_leases_recovered"]
    assert recovered
    assert recovered[0].observability_context["recovered_leases"] == 6  # type: ignore[attr-defined]
    summary = await repository.recovery_summary(now=NOW)
    assert summary.leased_sessions == 0 and summary.expired_leases == 0
    await repository.close()


# --- lease recovery after a hard worker stop -----------------------------------------


async def test_hard_stopped_worker_leases_recover_only_after_expiry(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(database)
    for index in range(30):
        await repository.create(make_session(index))
    crashed = await repository.claim_due_sessions(
        worker_id="crashed-worker",
        now=NOW,
        lease_until=NOW + timedelta(seconds=60),
        limit=10,
    )
    stale_snapshot = crashed[0]
    # Simulate a hard kill: the connection disappears without any release.
    await repository.close()

    restarted = SQLiteSessionRepository(database)
    before_expiry = await restarted.recovery_summary(now=NOW + timedelta(seconds=30))
    assert before_expiry.leased_sessions == 10
    early = await restarted.claim_due_sessions(
        worker_id="replacement",
        now=NOW + timedelta(seconds=30),
        lease_until=NOW + timedelta(seconds=90),
        limit=50,
    )
    assert {session.session_id for session in early}.isdisjoint(
        {session.session_id for session in crashed}
    )

    after_expiry = NOW + timedelta(seconds=61)
    recovered = await restarted.claim_due_sessions(
        worker_id="replacement",
        now=after_expiry,
        lease_until=after_expiry + timedelta(seconds=60),
        limit=50,
    )
    assert isinstance(recovered, ClaimedSessions)
    assert recovered.recovered_expired_leases == 10
    assert {session.session_id for session in crashed} <= {
        session.session_id for session in recovered
    }
    with pytest.raises(LeaseOwnershipError):
        await restarted.update(stale_snapshot)
    for session in recovered:
        assert session.queue_id == f"queue-{session.session_id.removeprefix('session-')}"
    await restarted.close()


# --- restart idempotency -------------------------------------------------------------


async def test_repeated_restart_is_idempotent_for_identities_and_terminal_states(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(database)
    statuses = (QueueStatus.PARKED, QueueStatus.ADMITTED, QueueStatus.EXPIRED, QueueStatus.FAILED)
    for index in range(2_000):
        status = statuses[index % len(statuses)]
        await repository.create(
            make_session(
                index,
                status=status,
                next_check_at=None if status is not QueueStatus.PARKED else NOW,
            )
        )
    baseline_digest = await identity_digest(repository)
    baseline = await repository.recovery_summary(now=NOW)
    await repository.close()

    for _ in range(3):
        reopened = SQLiteSessionRepository(database)
        await reopened.initialize()
        summary = await reopened.recovery_summary(now=NOW)
        assert summary.status_counts == baseline.status_counts
        assert summary.valid_queue_ids == baseline.valid_queue_ids
        assert await identity_digest(reopened) == baseline_digest
        await reopened.close()


class CountingCreationHandler:
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self.repository = repository
        self.calls = 0

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        self.calls += 1
        await self.repository.create(
            QueueSession(
                session_id=work_item.session_id,
                queue_id=f"new-{work_item.session_id}",
                transfer_url=f"https://queue.test/journey?q=new-{work_item.session_id}",
                mode=SessionMode.HYBRID,
                status=QueueStatus.PARKED,
                state_path=Path(f".browser-state/{work_item.session_id}.json"),
            )
        )
        return CreationOutcome(
            kind=CreationOutcomeKind.SUCCESS,
            attempts=1,
            temporary_failures=0,
            duration_seconds=0.0,
        )


async def test_restarted_acquisition_with_satisfied_target_creates_nothing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(database)
    for index in range(50):
        await repository.create(make_session(index))
    await repository.close()

    for _ in range(3):
        reopened = SQLiteSessionRepository(database)
        handler = CountingCreationHandler(reopened)
        controller = SessionCreationController(
            repository=reopened,
            handler=handler,
            target_queue_ids=50,
            worker_count=5,
            identity_replacement_limit=0,
        )
        metrics = await controller.run()
        assert handler.calls == 0
        assert metrics.successful_unique_ids == 50
        await reopened.close()


# --- mass replacement guard ----------------------------------------------------------


async def seed_with_lost_identities(repository: SQLiteSessionRepository) -> None:
    for index in range(10):
        await repository.create(make_session(index))
    for index in range(10, 13):
        # Acquired, then failed during monitoring (for example IDENTITY_MISMATCH).
        await repository.create(make_session(index, status=QueueStatus.FAILED))


@pytest.mark.parametrize(("limit", "expected_new"), [(0, 0), (2, 2), (None, 3)])
async def test_identity_replacement_limit_caps_replacements(
    tmp_path: Path,
    limit: int | None,
    expected_new: int,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await seed_with_lost_identities(repository)
    handler = CountingCreationHandler(repository)
    metrics = PrometheusMetrics()
    controller = SessionCreationController(
        repository=repository,
        handler=handler,
        target_queue_ids=13,
        worker_count=3,
        identity_replacement_limit=limit,
        observability=metrics,
    )

    result = await controller.run()

    assert handler.calls == expected_new
    assert await repository.count_successful_queue_ids() == 10 + expected_new
    assert result.replacement_blocked is (limit is not None and limit < 3)
    blocked_gauge = metrics.registry.get_sample_value("queue_identity_replacement_blocked")
    if limit is not None:
        assert blocked_gauge == (1 if limit < 3 else 0)
    # Lost identities keep their original Queue IDs; they are never overwritten.
    lost = await repository.get("session-00010")
    assert lost is not None and lost.queue_id == "queue-00010"
    await repository.close()


def test_identity_replacement_limit_setting_defaults_to_zero() -> None:
    settings = Settings(STAGING_URL="https://staging.example.test")
    assert settings.identity_replacement_limit == 0


# --- monitor classification of storage faults ----------------------------------------


class ScriptedRestorer:
    def __init__(self, results: list[SessionRestoreResult]) -> None:
        self.results = results
        self.calls = 0

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


async def no_sleep(_: float) -> None:
    return None


async def test_state_store_outage_never_fails_or_replaces_identity(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session(1))
    before = await repository.count_successful_queue_ids()
    restorer = ScriptedRestorer(
        [
            SessionRestoreResult(
                method=RestoreMethod.STORAGE_STATE,
                success=False,
                expected_queue_id="queue-00001",
                failure=RestoreFailure.STATE_UNAVAILABLE,
            )
        ]
    )
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        clock=lambda: NOW,
        sleep=no_sleep,
    )
    session = await repository.get("session-00001")
    assert session is not None

    outcome = await monitor.check(session)

    assert outcome.observed_status is QueueStatus.CONNECTION_LOST
    assert restorer.calls == 3  # bounded transient retries
    persisted = await repository.get("session-00001")
    assert persisted is not None
    assert persisted.status is QueueStatus.CONNECTION_LOST
    assert persisted.queue_id == "queue-00001"
    assert persisted.next_check_at is not None
    assert await repository.count_successful_queue_ids() == before
    await repository.close()


async def test_state_refresh_failure_keeps_verified_observation_without_retry(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session(1))
    progress = QueueProgress(
        session_id="session-00001",
        progress_percentage=40,
        users_ahead=100,
        active_queue=True,
    )
    restorer = ScriptedRestorer(
        [
            SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=False,
                expected_queue_id="queue-00001",
                observed_queue_id="queue-00001",
                identity_match=True,
                progress=progress,
                failure=RestoreFailure.STATE_REFRESH_FAILED,
            )
        ]
    )
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        clock=lambda: NOW,
        sleep=no_sleep,
    )
    session = await repository.get("session-00001")
    assert session is not None

    outcome = await monitor.check(session)

    assert restorer.calls == 1
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    persisted = await repository.get("session-00001")
    assert persisted is not None and persisted.queue_id == "queue-00001"
    await repository.close()


# --- database interruption -----------------------------------------------------------


class InterruptedRepository(SQLiteSessionRepository):
    def __init__(self, database: Path, *, failures: int) -> None:
        super().__init__(database)
        self.remaining_failures = failures

    async def due_session_summary(self, *, now: datetime) -> DueSessionSummary:
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return await super().due_session_summary(now=now)


async def test_scheduler_keeps_running_through_database_interruption(tmp_path: Path) -> None:
    repository = InterruptedRepository(tmp_path / "sessions.sqlite3", failures=3)
    for index in range(25):
        await repository.create(make_session(index))
    metrics = PrometheusMetrics()
    handler = RecordingHandler(repository)
    scheduler = scheduler_for(repository, handler, observability=metrics)
    scheduler._scheduler_tick_seconds = 0.001
    stop = asyncio.Event()

    task = asyncio.create_task(scheduler.run(stop))
    for _ in range(500):
        if len(handler.checked) == 25:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert sorted(handler.checked) == [f"session-{index:05d}" for index in range(25)]
    assert scheduler.metrics.schedule_failures == 3
    assert (
        metrics.registry.get_sample_value(
            "repository_errors_total", {"operation": "schedule"}
        )
        == 3
    )
    summary = await repository.recovery_summary(now=NOW)
    assert summary.leased_sessions == 0
    assert summary.valid_queue_ids == 25
    await repository.close()


async def test_sqlite_repository_reconnects_after_connection_loss(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session(1))
    assert repository._connection is not None
    repository._connection.close()  # the handle is lost underneath the repository

    with pytest.raises(sqlite3.ProgrammingError):
        await repository.get("session-00001")
    recovered = await repository.get("session-00001")

    assert recovered is not None and recovered.queue_id == "queue-00001"
    assert repository.reconnects == 1
    await repository.close()


async def test_sqlite_lock_contention_keeps_connection(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create(make_session(1))
    blocker = sqlite3.connect(database, timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    assert repository._connection is not None
    repository._connection.execute("PRAGMA busy_timeout = 0")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await repository.update(make_session(1))
    finally:
        blocker.rollback()
        blocker.close()

    assert repository.reconnects == 0
    await repository.close()


class ReleaseFailingRepository(SQLiteSessionRepository):
    async def release_lease(self, session_id: str, *, worker_id: str | None = None) -> bool:
        raise sqlite3.OperationalError("unable to open database file")


class BlockingHandler:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        await self.release.wait()
        raise AssertionError("unreachable")


async def test_shutdown_completes_when_database_is_unreachable(tmp_path: Path) -> None:
    repository = ReleaseFailingRepository(tmp_path / "sessions.sqlite3")
    for index in range(30):
        await repository.create(make_session(index))
    scheduler = scheduler_for(repository, BlockingHandler())
    await scheduler.start()
    await scheduler.schedule_due()

    await asyncio.wait_for(scheduler.shutdown(timeout_seconds=0.05), timeout=5)

    assert scheduler.worker_task_count == 0
    assert scheduler.metrics.lease_release_failures == 20
    await repository.close()


class FailingCloseRepository(SQLiteSessionRepository):
    async def close(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")


class TrackingBrowserManager:
    def report_navigation(self, context: object, *, responsive: bool) -> None:
        """Navigation health reports are irrelevant to this fake."""

    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        self.stopped = True


class FailingShutdownScheduler(ParkedSessionScheduler):
    async def shutdown(self, *, timeout_seconds: float | None = None) -> None:
        raise sqlite3.OperationalError("disk I/O error")


async def test_runtime_closes_browser_even_when_database_steps_fail(tmp_path: Path) -> None:
    repository = FailingCloseRepository(tmp_path / "sessions.sqlite3")
    await repository.create(make_session(1))
    browser = TrackingBrowserManager()
    scheduler = FailingShutdownScheduler(
        repository=repository,
        handler=RecordingHandler(repository),
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=60,
        failure_delay_seconds=30,
        scheduler_tick_seconds=0.01,
        clock=lambda: NOW + timedelta(days=1),
    )
    metrics = PrometheusMetrics()
    runtime = ApplicationRuntime(
        browser_manager=cast(BrowserManager, browser),
        repository=repository,
        monitoring_scheduler=scheduler,
        shutdown_timeout_seconds=0.5,
        observability=metrics,
        target_queue_ids=10,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(0.05)
    runtime.request_shutdown()
    await asyncio.wait_for(task, timeout=5)

    assert browser.stopped
    assert runtime.startup_recovery_seconds is not None
    assert metrics.registry.get_sample_value("queue_ids_valid") == 1
    assert metrics.registry.get_sample_value("queue_ids_remaining") == 9
    assert metrics.registry.get_sample_value("startup_recovery_duration_seconds") is not None
    await SQLiteSessionRepository.close(repository)


# --- metrics cardinality and redaction -----------------------------------------------


class FakeBrowserManager:
    def report_navigation(self, context: object, *, responsive: bool) -> None:
        """Navigation health reports are irrelevant to this fake."""

    async def capacity(self) -> BrowserCapacity:
        return BrowserCapacity(
            chrome_processes=2,
            connected_processes=2,
            active_contexts=3,
            available_contexts=47,
            maximum_active_contexts=50,
            processes=(),
        )


async def exposition_for_population(tmp_path: Path, population: int) -> tuple[int, str]:
    repository = SQLiteSessionRepository(tmp_path / f"population-{population}.sqlite3")
    statuses = tuple(QueueStatus)
    for index in range(population):
        status = statuses[index % len(statuses)]
        session = make_session(index, status=status)
        await repository.create(
            session,
            QueueProgress(session_id=session.session_id, progress_percentage=index % 101),
        )
    metrics = PrometheusMetrics()
    settings = Settings(STAGING_URL="https://staging.example.test", TARGET_QUEUE_IDS=10_000)
    provider = StatusSummaryProvider(
        settings=settings,
        repository=repository,
        browser_manager=cast(BrowserManager, FakeBrowserManager()),
        metrics=metrics,
    )
    for index in range(min(population, 200)):
        metrics.record_check(
            0.2,
            QueueProgress(
                session_id=f"session-{index:05d}",
                progress_percentage=index % 101,
                users_ahead=index,
            ),
        )
        metrics.record_repository_error("an-unexpected-operation-name")
    summary = await provider.snapshot()
    assert summary.persisted_sessions == population
    series = sum(len(family.samples) for family in metrics.registry.collect())
    for family in metrics.registry.collect():
        for sample in family.samples:
            assert not FORBIDDEN_LABEL_NAMES & set(sample.labels)
    exposition = metrics.render().decode()
    await repository.close()
    return series, exposition


async def test_metric_series_count_does_not_grow_with_population(tmp_path: Path) -> None:
    small_series, _ = await exposition_for_population(tmp_path, 5)
    large_series, exposition = await exposition_for_population(tmp_path, 3_000)

    assert small_series == large_series
    assert "queue-0" not in exposition
    assert "session-0" not in exposition
    assert "https://queue.test" not in exposition
    for bucket in PROGRESS_BUCKETS:
        assert f'queue_sessions_progress_bucket{{bucket="{bucket}"}}' in exposition
    assert 'repository_errors_total{operation="other"} 200.0' in exposition


async def test_progress_distribution_uses_fixed_buckets(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    values = (None, 0, 9.9, 10, 24, 25, 60, 75, 89.9, 90, 100)
    for index, value in enumerate(values):
        session = make_session(index)
        await repository.create(
            session,
            QueueProgress(session_id=session.session_id, progress_percentage=value),
        )
    await repository.create(make_session(50, status=QueueStatus.ADMITTED, next_check_at=None))

    distribution = await repository.progress_distribution()

    assert tuple(distribution) == PROGRESS_BUCKETS
    assert distribution == {
        "unknown": 1,
        "0-10": 2,
        "10-25": 2,
        "25-50": 1,
        "50-75": 1,
        "75-90": 2,
        "90-100": 2,
    }
    await repository.close()


def test_structured_logs_redact_urls_and_keep_correlation_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter(run_id="run-123"))
    logger = logging.getLogger("queue_load_test.test_redaction")
    third_party = logging.getLogger("playwright.test_redaction")
    for target in (logger, third_party):
        target.handlers = [handler]
        target.propagate = False
        target.setLevel(logging.INFO)

    log_event(
        logger,
        logging.WARNING,
        "restore failed at https://queue.test/journey?q=secret-queue&t=token",
        session_id="session-1",
        queue_id="queue-1",
        worker_id="monitor-1",
        browser_id=0,
        error_type="Navigation to https://queue.test/journey?q=secret-queue failed",
        transfer_url="https://queue.test/journey?q=secret-queue",
        state_path="/tmp/state/session-1.json",
    )
    third_party.error("page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:9/q?q=abc")

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    text = stream.getvalue()
    assert "secret-queue" not in text
    assert "token" not in text
    assert "127.0.0.1:9" not in text
    assert "transfer_url" not in lines[0]
    assert "state_path" not in lines[0]
    assert lines[0]["run_id"] == "run-123"
    assert lines[0]["session_id"] == "session-1"
    assert lines[0]["worker_id"] == "monitor-1"
    assert lines[0]["message"] == "restore failed at <redacted-url>"
    assert lines[1]["message"] == "page.goto: net::ERR_CONNECTION_REFUSED at <redacted-url>"


async def test_cancelled_operation_keeps_connection_serialized(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.initialize()
    timeline: list[str] = []

    def slow_operation() -> None:
        timeline.append("slow-start")
        import time

        time.sleep(0.2)
        timeline.append("slow-end")

    def next_operation() -> None:
        timeline.append("next")

    slow = asyncio.create_task(repository._run(slow_operation))
    await asyncio.sleep(0.05)
    slow.cancel()
    following = asyncio.create_task(repository._run(next_operation))
    with pytest.raises(asyncio.CancelledError):
        await slow
    await following

    assert timeline == ["slow-start", "slow-end", "next"]
    await repository.close()


async def test_bulk_create_many_matches_individual_creates(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    rows = [
        (make_session(index), QueueProgress(session_id=f"session-{index:05d}", users_ahead=index))
        for index in range(100)
    ]

    assert await repository.create_many(rows) == 100

    assert await repository.count_successful_queue_ids() == 100
    progress = await repository.get_progress("session-00042")
    assert progress is not None and progress.users_ahead == 42
    await repository.close()


async def test_renewing_own_expired_lease_is_not_a_recovery(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(
        make_session(1, worker_id="same-owner", lease_until=NOW - timedelta(seconds=1))
    )
    await repository.create(
        make_session(2, worker_id="other-owner", lease_until=NOW - timedelta(seconds=1))
    )

    claimed = await repository.claim_due_sessions(
        worker_id="same-owner",
        now=NOW,
        lease_until=NOW + timedelta(seconds=60),
        limit=10,
    )

    assert isinstance(claimed, ClaimedSessions)
    assert len(claimed) == 2
    assert claimed.recovered_expired_leases == 1
    await repository.close()


async def test_scheduler_counts_reclaimed_own_expired_leases(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    for index in range(3):
        await repository.create(
            make_session(index, worker_id="recovery-scheduler", lease_until=NOW - timedelta(seconds=1))
        )
    scheduler = scheduler_for(repository, RecordingHandler(repository))
    await scheduler.start()
    await scheduler.schedule_due()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    assert scheduler.metrics.own_expired_leases_reclaimed == 3
    assert scheduler.metrics.expired_leases_recovered == 0
    await repository.close()
