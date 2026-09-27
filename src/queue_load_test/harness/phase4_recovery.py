"""Controlled Phase 4 resilience and recovery scenarios at a 10,000-session population.

Evidence produced here is **local and controlled**. It uses real SQLite, real
atomic state files, real ``SIGKILL``-ed worker processes, and installed Google
Chrome driven against :class:`LocalQueueSimulator`. It never contacts Queue-it or a
staging environment, so it cannot establish real Queue-it recovery behavior.

The report is aggregate: it contains no Queue IDs, session IDs, transfer URLs, or
browser state. Synthetic identities exist only in the temporary work directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.harness.local_queue_simulator import (
    STAGE_ACTIVE,
    STAGE_PRE,
    STAGE_SERVICED,
    LocalQueueSimulator,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import JsonLogFormatter, PrometheusMetrics
from queue_load_test.metrics.prometheus import FORBIDDEN_LABEL_NAMES
from queue_load_test.metrics.status import ObservabilityHttpServer, StatusSummaryProvider
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationRetryPolicy,
    MonitoringOutcome,
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionCreator,
    QueueSessionMonitor,
    SessionCreationController,
)
from queue_load_test.state import (
    BrowserState,
    FileSystemStateStore,
    StateConsistencyChecker,
    StateUnreadableError,
)
from queue_load_test.transfer import QueueSessionRestorer

try:  # optional benchmark dependency
    psutil: Any = importlib.import_module("psutil")
except ImportError:  # pragma: no cover - exercised only without the extra
    psutil = None

_T = TypeVar("_T")

LIVE_OWNER = "live-monitor-owner"
CRASHED_OWNER = "crashed-monitor-owner"
CHILD_OWNER = "child-worker"
_TERMINAL = frozenset({QueueStatus.ADMITTED, QueueStatus.EXPIRED, QueueStatus.FAILED})


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ScenarioResult:
    """One scenario: executed checks and aggregate measurements only."""

    key: str
    title: str
    evidence: str
    outcome: Outcome = Outcome.UNKNOWN
    checks: dict[str, bool] = field(default_factory=dict)
    measurements: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def check(self, name: str, passed: bool) -> bool:
        self.checks[name] = bool(passed)
        return bool(passed)

    def finish(self) -> ScenarioResult:
        if self.checks:
            self.outcome = Outcome.PASS if all(self.checks.values()) else Outcome.FAIL
        return self


@dataclass(frozen=True, slots=True)
class PopulationLayout:
    """Index ranges of the synthetic population; sizes scale with ``population``."""

    population: int
    group_size: int

    def __post_init__(self) -> None:
        if self.population < 500:
            raise ValueError("population must be at least 500")
        if self.group_size < 1:
            raise ValueError("group_size must be at least 1")
        if self.fault_end > self.due_end:
            raise ValueError("fault groups do not fit inside the due population")

    @property
    def two_percent(self) -> int:
        return max(1, self.population // 50)

    @property
    def one_percent(self) -> int:
        return max(1, self.population // 100)

    @property
    def expired_leases(self) -> range:
        return range(self.two_percent)

    @property
    def live_leases(self) -> range:
        return range(self.expired_leases.stop, self.expired_leases.stop + self.two_percent)

    @property
    def monitorable(self) -> range:
        return range(self.live_leases.stop, self.admitted.start)

    @property
    def due_end(self) -> int:
        start = self.monitorable.start
        return start + (self.monitorable.stop - start) * 54 // 100

    @property
    def admitted(self) -> range:
        end = self.population - 2 * self.one_percent
        return range(end - self.two_percent, end)

    @property
    def expired_status(self) -> range:
        return range(self.admitted.stop, self.admitted.stop + self.one_percent)

    @property
    def creation_failed(self) -> range:
        return range(self.expired_status.stop, self.population)

    def _group(self, offset: int, multiplier: int = 1) -> range:
        start = self.monitorable.start + offset
        return range(start, start + self.group_size * multiplier)

    @property
    def navigation_failure(self) -> range:
        return self._group(0, 2)

    @property
    def transfer_fallback(self) -> range:
        return range(
            self.navigation_failure.stop, self.navigation_failure.stop + 2 * self.group_size
        )

    @property
    def storage_corrupt(self) -> range:
        return range(self.transfer_fallback.stop, self.transfer_fallback.stop + self.group_size)

    @property
    def storage_unavailable(self) -> range:
        return range(self.storage_corrupt.stop, self.storage_corrupt.stop + self.group_size)

    @property
    def context_failure(self) -> range:
        return range(self.storage_unavailable.stop, self.storage_unavailable.stop + self.group_size)

    @property
    def identity_mismatch(self) -> range:
        return range(self.context_failure.stop, self.context_failure.stop + self.group_size)

    @property
    def navigation_timeout(self) -> range:
        size = max(1, self.group_size // 2)
        return range(self.identity_mismatch.stop, self.identity_mismatch.stop + size)

    @property
    def fault_end(self) -> int:
        return self.navigation_timeout.stop

    @property
    def healthy_due(self) -> range:
        return range(self.fault_end, self.due_end)

    @property
    def future(self) -> range:
        return range(self.due_end, self.monitorable.stop)

    @property
    def expected_permanent_failures(self) -> set[int]:
        return set(self.storage_corrupt) | set(self.identity_mismatch)

    @property
    def valid_queue_ids(self) -> int:
        return self.population - len(self.creation_failed)

    @property
    def initially_due(self) -> int:
        return len(self.expired_leases) + (self.due_end - self.fault_end)


def session_id_for(index: int) -> str:
    return f"sim-session-{index:05d}"


def queue_id_for(index: int) -> str:
    return f"sim-q-{index:05d}"


def _index(session_id: str) -> int:
    return int(session_id.rsplit("-", 1)[1])


def _stage_for(index: int) -> tuple[QueueStatus, str]:
    cycle = index % 4
    if cycle == 0:
        return QueueStatus.PRE_QUEUE, STAGE_PRE
    if cycle == 1:
        return QueueStatus.ACTIVE_QUEUE, STAGE_ACTIVE
    if cycle == 2:
        return QueueStatus.PARKED, STAGE_ACTIVE
    return QueueStatus.SERVICED_SOON, STAGE_SERVICED


def _cookie_state(queue_id: str) -> BrowserState:
    return {
        "cookies": [
            {
                "name": "queue_id",
                "value": queue_id,
                "domain": "127.0.0.1",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


class FaultInjectingRepository(SQLiteSessionRepository):
    """SQLite repository with a controllable outage and connection severing."""

    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.outage = False
        self.injected_failures = 0

    async def _run(self, operation: Callable[[], _T]) -> _T:
        if self.outage:
            self.injected_failures += 1
            raise sqlite3.OperationalError("disk I/O error (injected outage)")
        return await super()._run(operation)

    async def sever_connection(self) -> None:
        """Close the live handle underneath the repository, as a lost file handle would."""

        def operation() -> None:
            if self._connection is not None:
                self._connection.close()

        await super()._run(operation)


class FaultInjectingStateStore(FileSystemStateStore):
    """Filesystem state store with an interruption window and unreadable IDs."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.unreadable_ids: set[str] = set()
        self.save_outage = False
        self.load_outage = False
        self.injected_load_failures = 0
        self.injected_save_failures = 0

    async def load(self, session_id: str) -> BrowserState | None:
        if self.load_outage or session_id in self.unreadable_ids:
            self.injected_load_failures += 1
            raise StateUnreadableError(f"Could not read browser state for {session_id!r}")
        return await super().load(session_id)

    async def save(self, session_id: str, state: BrowserState) -> Path:
        if self.save_outage:
            self.injected_save_failures += 1
            raise OSError("state volume unavailable (injected outage)")
        return await super().save(session_id, state)


# --- population ----------------------------------------------------------------------


async def seed_population(
    database: Path,
    state_store: FileSystemStateStore,
    simulator: LocalQueueSimulator,
    layout: PopulationLayout,
    *,
    now: datetime,
) -> None:
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    rows: list[tuple[QueueSession, QueueProgress | None]] = []
    for index in range(layout.population):
        session_id = session_id_for(index)
        queue_id: str | None = queue_id_for(index)
        status, stage = _stage_for(index)
        next_check_at: datetime | None = now - timedelta(seconds=30 + index % 300)
        worker_id: str | None = None
        lease_until: datetime | None = None
        last_error: str | None = None
        if index in layout.expired_leases:
            worker_id, lease_until = CRASHED_OWNER, now - timedelta(seconds=1)
            next_check_at = now - timedelta(minutes=10)
        elif index in layout.live_leases:
            worker_id, lease_until = LIVE_OWNER, now + timedelta(hours=2)
        elif index in layout.future or index < layout.fault_end:
            # Fault groups stay parked until the resume sweep activates them, so the
            # earlier shutdown scenario cannot consume them.
            next_check_at = now + timedelta(hours=1, seconds=index % 3600)
        elif index in layout.admitted:
            status, next_check_at = QueueStatus.ADMITTED, None
        elif index in layout.expired_status:
            status, next_check_at = QueueStatus.EXPIRED, None
        elif index in layout.creation_failed:
            status, queue_id, next_check_at = QueueStatus.FAILED, None, None
            last_error = "creation:permanent_http_response"
        session = QueueSession(
            session_id=session_id,
            queue_id=queue_id,
            transfer_url=simulator.transfer_url(queue_id) if queue_id else "",
            mode=SessionMode.HYBRID,
            status=status,
            state_path=state_store.path_for(session_id),
            created_at=now - timedelta(hours=2, microseconds=index),
            next_check_at=next_check_at,
            last_error=last_error,
            worker_id=worker_id,
            lease_until=lease_until,
        )
        percentage = index % 101
        progress = (
            QueueProgress(session_id=session_id, progress_percentage=float(percentage))
            if queue_id is not None and status is not QueueStatus.PRE_QUEUE
            else None
        )
        rows.append((session, progress))
        if queue_id is not None:
            simulator.stages[queue_id] = stage
            simulator.progress[queue_id] = percentage
    await repository.create_many(rows)
    await repository.close()

    semaphore = asyncio.Semaphore(20)

    async def save(index: int) -> None:
        async with semaphore:
            await state_store.save(session_id_for(index), _cookie_state(queue_id_for(index)))

    await asyncio.gather(
        *(save(index) for index in range(layout.population) if index not in layout.creation_failed)
    )


def apply_fault_groups(
    layout: PopulationLayout,
    simulator: LocalQueueSimulator,
    state_store: FaultInjectingStateStore,
) -> None:
    ids = queue_id_for
    simulator.empty_response_ids.update(ids(i) for i in layout.navigation_failure)
    simulator.slow_ids.update(ids(i) for i in layout.navigation_timeout)
    simulator.mismatch_ids.update(ids(i) for i in layout.identity_mismatch)
    for group in (
        layout.transfer_fallback,
        layout.storage_corrupt,
        layout.storage_unavailable,
        layout.context_failure,
    ):
        simulator.transfer_down_ids.update(ids(i) for i in group)
    state_store.unreadable_ids.update(session_id_for(i) for i in layout.storage_unavailable)
    for index in layout.storage_corrupt:
        state_store.path_for(session_id_for(index)).write_text("{corrupt", encoding="utf-8")


def activate_fault_groups(database: Path, layout: PopulationLayout) -> None:
    """Make every fault-group session due before the rest of the backlog."""

    due = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            "UPDATE queue_sessions SET next_check_at = ? WHERE session_id = ?",
            [
                (due, session_id_for(index))
                for index in range(layout.monitorable.start, layout.fault_end)
            ],
        )
        connection.commit()
    finally:
        connection.close()


async def write_invalid_context_states(
    layout: PopulationLayout,
    state_store: FileSystemStateStore,
) -> None:
    # A valid, digest-verified envelope whose cookie Chrome rejects at
    # BrowserContext creation: a real context-creation failure.
    for index in layout.context_failure:
        await state_store.save(session_id_for(index), {"cookies": [{"name": "q"}], "origins": []})


# --- inspection helpers --------------------------------------------------------------


async def identity_digest(repository: SQLiteSessionRepository) -> tuple[str, int]:
    digest = hashlib.sha256()
    sessions = await repository.list()
    for session in sorted(sessions, key=lambda item: item.session_id):
        digest.update(
            f"{session.session_id}\0{session.queue_id or ''}\0{session.transfer_url}\n".encode()
        )
    return digest.hexdigest(), len(sessions)


async def original_identity_digest(
    repository: SQLiteSessionRepository,
    population: int,
) -> str:
    digest = hashlib.sha256()
    for session in sorted(await repository.list(), key=lambda item: item.session_id):
        if (
            session.session_id.startswith("sim-session-")
            and _index(session.session_id) < population
        ):
            digest.update(
                f"{session.session_id}\0{session.queue_id or ''}\0{session.transfer_url}\n".encode()
            )
    return digest.hexdigest()


async def terminal_snapshot(repository: SQLiteSessionRepository) -> dict[str, str]:
    return {
        session.session_id: session.status.value
        for session in await repository.list()
        if session.status in _TERMINAL
    }


def chrome_main_processes(*, descendants_of: int | None = None) -> list[Any]:
    """Return Playwright-launched Chrome browser (not renderer/helper) processes."""

    if psutil is None:
        return []
    if descendants_of is not None:
        try:
            candidates = psutil.Process(descendants_of).children(recursive=True)
        except psutil.Error:
            return []
    else:
        candidates = list(psutil.process_iter())
    found = []
    for process in candidates:
        try:
            arguments = process.cmdline()
        except psutil.Error:
            continue
        if "--remote-debugging-pipe" in arguments and not any(
            argument.startswith("--type=") for argument in arguments
        ):
            found.append(process)
    return found


def _stats(values: Iterable[float]) -> dict[str, float | None]:
    samples = list(values)
    if not samples:
        return {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(samples),
        "p50": round(percentile(samples, 50) or 0.0, 4),
        "p95": round(percentile(samples, 95) or 0.0, 4),
        "max": round(max(samples), 4),
        "mean": round(statistics.fmean(samples), 4),
    }


def _metric(metrics: PrometheusMetrics, name: str, labels: dict[str, str] | None = None) -> float:
    return float(metrics.registry.get_sample_value(name, labels or {}) or 0.0)


async def _wait_for(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float,
    interval: float = 0.1,
) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return await predicate()


# --- runtime assembly ----------------------------------------------------------------


def harness_polling_policy() -> PollingPolicy:
    """Park every observed session for an hour so one sweep has a defined end."""

    hour = 3600.0
    return PollingPolicy(
        default_seconds=hour,
        jitter_seconds=0,
        pre_queue_min_seconds=hour,
        pre_queue_max_seconds=hour,
        active_early_min_seconds=hour,
        active_early_max_seconds=hour,
        active_mid_min_seconds=hour,
        active_mid_max_seconds=hour,
        serviced_soon_min_seconds=hour,
        serviced_soon_max_seconds=hour,
        turn_started_seconds=hour,
    )


@dataclass(slots=True)
class MonitoringStack:
    manager: BrowserManager
    scheduler: ParkedSessionScheduler
    metrics: PrometheusMetrics


def build_monitoring_stack(
    *,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    simulator: LocalQueueSimulator,
    metrics: PrometheusMetrics,
    scheduler_id: str,
    chrome_processes: int = 2,
    contexts_per_browser: int = 25,
    workers: int = 20,
    queue_capacity: int = 50,
    lease_seconds: float = 15.0,
) -> MonitoringStack:
    manager = BrowserManager(
        chrome_process_count=chrome_processes,
        max_contexts_per_browser=contexts_per_browser,
        max_active_contexts=chrome_processes * contexts_per_browser,
        observability=metrics,
    )
    restorer = QueueSessionRestorer(
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
        expected_journey_url=simulator.queue_url,
        storage_navigation_url=simulator.queue_url,
        admission_detector=AdmissionDetector.from_urls(simulator.protected_url),
        navigation_timeout_ms=1_500,
        admission_wait_timeout_ms=0,
        observation_timeout_seconds=2.0,
        observation_interval_seconds=0.1,
        observability=metrics,
    )
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=harness_polling_policy(),
        retry_policy=MonitoringRetryPolicy(
            max_attempts=3,
            initial_backoff_seconds=0.2,
            maximum_backoff_seconds=1.0,
            jitter_seconds=0.1,
        ),
        observability=metrics,
    )
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=monitor,
        worker_count=workers,
        queue_capacity=queue_capacity,
        claim_batch_size=queue_capacity,
        lease_seconds=lease_seconds,
        failure_delay_seconds=3600,
        scheduler_tick_seconds=0.05,
        shutdown_timeout_seconds=5,
        scheduler_id=scheduler_id,
        observability=metrics,
    )
    return MonitoringStack(manager=manager, scheduler=scheduler, metrics=metrics)


# --- scenarios -----------------------------------------------------------------------


async def scenario_restart(
    database: Path,
    state_store: FileSystemStateStore,
    layout: PopulationLayout,
    *,
    repetitions: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key="restart_with_persisted_population",
        title=f"Restart with {layout.population:,} persisted sessions",
        evidence="local SQLite + local state files (synthetic identities)",
    )
    baseline_repository = SQLiteSessionRepository(database)
    baseline_digest, baseline_rows = await identity_digest(baseline_repository)
    baseline_terminal = await terminal_snapshot(baseline_repository)
    await baseline_repository.close()

    startup: list[float] = []
    first_claim: list[float] = []
    summaries = []
    for cycle in range(repetitions):
        started = time.perf_counter()
        repository = SQLiteSessionRepository(database)
        await repository.initialize()
        summary = await repository.recovery_summary(now=datetime.now(UTC))
        startup.append(time.perf_counter() - started)
        summaries.append(summary)
        digest, rows = await identity_digest(repository)
        result.check(f"cycle_{cycle + 1}_identity_digest_unchanged", digest == baseline_digest)
        result.check(f"cycle_{cycle + 1}_row_count_unchanged", rows == baseline_rows)
        result.check(
            f"cycle_{cycle + 1}_terminal_states_unchanged",
            await terminal_snapshot(repository) == baseline_terminal,
        )
        await repository.close()

    # Time-to-first-work on a copy so the claims do not change the main population.
    copy = database.with_name("restart-claim-copy.sqlite3")
    for _ in range(repetitions):
        shutil.copyfile(database, copy)
        started = time.perf_counter()
        repository = SQLiteSessionRepository(copy)
        await repository.initialize()
        now = datetime.now(UTC)
        await repository.recovery_summary(now=now)
        claimed = await repository.claim_due_sessions(
            worker_id="restart-probe",
            now=now,
            lease_until=now + timedelta(seconds=60),
            limit=50,
        )
        first_claim.append(time.perf_counter() - started)
        await repository.close()
        result.check("first_claim_returns_bounded_batch", len(claimed) == 50)
    copy.unlink(missing_ok=True)

    audit_started = time.perf_counter()
    repository = SQLiteSessionRepository(database)
    audit = await StateConsistencyChecker(repository, state_store).check()
    audit_seconds = time.perf_counter() - audit_started
    await repository.close()

    first = summaries[0]
    result.check("valid_queue_ids_match_layout", first.valid_queue_ids == layout.valid_queue_ids)
    result.check(
        "summary_stable_across_restarts",
        all(summary.status_counts == first.status_counts for summary in summaries),
    )
    result.check("state_audit_clean", audit.is_consistent)
    result.measurements.update(
        {
            "persisted_sessions": baseline_rows,
            "valid_queue_ids": first.valid_queue_ids,
            "restart_repetitions": repetitions,
            "startup_initialize_plus_summary_seconds": _stats(startup),
            "startup_to_first_claim_seconds": _stats(first_claim),
            "due_backlog_at_restart": first.sessions_due,
            "expired_leases_at_restart": first.expired_leases,
            "active_leases_at_restart": first.leased_sessions,
            "terminal_sessions": first.terminal_sessions,
            "full_state_audit_seconds": round(audit_seconds, 4),
            "state_files_audited": audit.state_files,
        }
    )
    return result.finish()


class _SyntheticHandler:
    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self._repository = repository
        self.checked: list[str] = []

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.checked.append(session.session_id)
        session.next_check_at = datetime.now(UTC) + timedelta(hours=1)
        session.last_checked_at = datetime.now(UTC)
        await self._repository.update(session)
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=session.next_check_at,
            queue_update_stale=False,
            progress_changed=False,
        )


async def scenario_expired_leases(database: Path, layout: PopulationLayout) -> ScenarioResult:
    result = ScenarioResult(
        key="expired_leases",
        title="Expired leases become available for recovery",
        evidence="local SQLite, bounded scheduler, synthetic check handler",
    )
    copy = database.with_name("expired-lease-copy.sqlite3")
    shutil.copyfile(database, copy)
    repository = SQLiteSessionRepository(copy)
    metrics = PrometheusMetrics()
    handler = _SyntheticHandler(repository)
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=20,
        queue_capacity=50,
        claim_batch_size=50,
        lease_seconds=60,
        failure_delay_seconds=3600,
        scheduler_id="lease-recovery-scheduler",
        observability=metrics,
    )
    expected = {session_id_for(i) for i in layout.expired_leases}
    started = time.perf_counter()
    await scheduler.start()
    while not expected <= set(handler.checked):
        if await scheduler.schedule_due() == 0 and scheduler.queue_size == 0:
            await scheduler.wait_until_idle()
            if await scheduler.schedule_due() == 0:
                break
        await scheduler.wait_until_idle()
    recovery_seconds = time.perf_counter() - started
    await scheduler.shutdown()
    live = [
        session
        for session in await repository.list()
        if _index(session.session_id) in layout.live_leases
    ]
    summary = await repository.recovery_summary(now=datetime.now(UTC))
    await repository.close()
    copy.unlink(missing_ok=True)

    recovered = scheduler.metrics.expired_leases_recovered
    result.check("all_expired_leases_rechecked", expected <= set(handler.checked))
    result.check("recovered_count_matches_expired", recovered == len(expected))
    result.check(
        "metric_matches", _metric(metrics, "monitoring_lease_recoveries_total") == recovered
    )
    result.check("live_leases_untouched", all(s.worker_id == LIVE_OWNER for s in live))
    result.check("no_scheduler_leases_left", summary.leased_sessions == len(layout.live_leases))
    result.measurements.update(
        {
            "expired_leases_before": len(expected),
            "expired_leases_recovered": recovered,
            "live_leases_left_untouched": len(live),
            "time_to_recover_all_seconds": round(recovery_seconds, 4),
            "checks_to_recover_all": len(handler.checked),
        }
    )
    return result.finish()


async def scenario_shutdown_during_monitoring(
    *,
    database: Path,
    state_store: FaultInjectingStateStore,
    simulator: LocalQueueSimulator,
    layout: PopulationLayout,
    stop_after_checks: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key="shutdown_during_monitoring",
        title="Application shutdown during browser-backed monitoring",
        evidence="installed Chrome + local simulator + local SQLite/state",
    )
    repository = SQLiteSessionRepository(database)
    before_digest = await original_identity_digest(repository, layout.population)
    metrics = PrometheusMetrics()
    stack = build_monitoring_stack(
        repository=repository,
        state_store=state_store,
        simulator=simulator,
        metrics=metrics,
        scheduler_id="shutdown-scenario-scheduler",
    )
    await stack.manager.start()
    stop = asyncio.Event()
    task = asyncio.create_task(stack.scheduler.run(stop))
    while stack.scheduler.metrics.checked < stop_after_checks and not task.done():
        await asyncio.sleep(0.02)
    in_flight = stack.scheduler.metrics.currently_checking
    queued = stack.scheduler.queue_size
    shutdown_started = time.perf_counter()
    # Forced: in-flight checks get only 0.5 s before cancellation. The run loop is
    # stopped afterwards so only one shutdown drains the fixed workers.
    await stack.scheduler.shutdown(timeout_seconds=0.5)
    stop.set()
    await task
    scheduler_shutdown_seconds = time.perf_counter() - shutdown_started
    capacity = await stack.manager.capacity()
    contexts_after_scheduler = capacity.active_contexts
    browser_started = time.perf_counter()
    await stack.manager.shutdown()
    browser_shutdown_seconds = time.perf_counter() - browser_started
    await asyncio.sleep(0.5)
    leftover_chrome = len(chrome_main_processes(descendants_of=os.getpid()))
    summary = await repository.recovery_summary(now=datetime.now(UTC))
    after_digest = await original_identity_digest(repository, layout.population)
    scheduler_leases = sum(
        1
        for session in await repository.list()
        if session.worker_id == "shutdown-scenario-scheduler"
    )
    await repository.close()

    result.check("queue_ids_unchanged", after_digest == before_digest)
    result.check("no_contexts_after_scheduler_shutdown", contexts_after_scheduler == 0)
    result.check("no_chrome_processes_after_browser_shutdown", leftover_chrome == 0)
    result.check("no_leases_left_by_stopped_scheduler", scheduler_leases == 0)
    result.check("no_rows_created", summary.total_persisted_sessions == layout.population)
    result.measurements.update(
        {
            "checks_before_shutdown": stack.scheduler.metrics.checked,
            "in_flight_at_shutdown": in_flight,
            "queued_at_shutdown": queued,
            "forced_cancel_timeout_seconds": 0.5,
            "scheduler_shutdown_seconds": round(scheduler_shutdown_seconds, 4),
            "browser_shutdown_seconds": round(browser_shutdown_seconds, 4),
            "contexts_after_scheduler_shutdown": contexts_after_scheduler,
            "chrome_processes_after_shutdown": leftover_chrome,
            "leases_left_by_scheduler": scheduler_leases,
            "expired_leases_recovered_during_run": stack.scheduler.metrics.expired_leases_recovered,
            "due_backlog_after_shutdown": summary.sessions_due,
        }
    )
    return result.finish()


@dataclass(slots=True)
class SweepFaultPlan:
    chrome_kills: int = 5
    state_outage_seconds: float = 3.0
    database_outage_seconds: float = 3.0


@dataclass(slots=True)
class _SweepObservations:
    kill_times: list[float] = field(default_factory=list)
    kill_pids: int = 0
    state_outage: tuple[float, float] | None = None
    database_outage: tuple[float, float] | None = None
    database_first_success_after_outage: float | None = None
    peak_contexts: int = 0
    samples: list[dict[str, float]] = field(default_factory=list)


async def scenario_resume_sweep(
    *,
    database: Path,
    state_store: FaultInjectingStateStore,
    simulator: LocalQueueSimulator,
    layout: PopulationLayout,
    plan: SweepFaultPlan,
    timeout_seconds: float,
    log_path: Path,
) -> tuple[list[ScenarioResult], dict[str, object]]:
    """Restart, resume every due session with real Chrome, and inject faults mid-sweep."""

    repository = FaultInjectingRepository(database)
    baseline_digest = await original_identity_digest(repository, layout.population)
    baseline_terminal = {
        key: value
        for key, value in (await terminal_snapshot(repository)).items()
        if _index(key) not in layout.expected_permanent_failures
    }
    metrics = PrometheusMetrics()
    metrics.set_target(layout.population)
    restart_started = time.perf_counter()
    await repository.initialize()
    startup_summary = await repository.recovery_summary(now=datetime.now(UTC))
    startup_seconds = time.perf_counter() - restart_started
    metrics.record_startup_recovery(startup_seconds, startup_summary, target=layout.population)
    stack = build_monitoring_stack(
        repository=repository,
        state_store=state_store,
        simulator=simulator,
        metrics=metrics,
        scheduler_id="resume-sweep-scheduler",
    )
    browser_started = time.perf_counter()
    await stack.manager.start()
    browser_start_seconds = time.perf_counter() - browser_started
    settings = Settings.model_validate(
        {
            "STAGING_URL": simulator.queue_url,
            "TARGET_QUEUE_IDS": layout.population,
            "MONITOR_WORKERS": 20,
            "MONITOR_QUEUE_CAPACITY": 50,
            "MONITOR_CLAIM_BATCH_SIZE": 50,
            "CREATION_WORKERS": 10,
            "CREATION_QUEUE_CAPACITY": 10,
        }
    )
    status_server = ObservabilityHttpServer(
        metrics=metrics,
        status_provider=StatusSummaryProvider(
            settings=settings,
            repository=repository,
            browser_manager=stack.manager,
            metrics=metrics,
        ),
        port=0,
    )
    await status_server.start()

    stop = asyncio.Event()
    sweep_started = time.perf_counter()
    sweep_started_at = datetime.now(UTC)
    task = asyncio.create_task(stack.scheduler.run(stop))
    observed = _SweepObservations()
    target_checks = startup_summary.sessions_due
    kill_points = [
        int(target_checks * fraction / (plan.chrome_kills + 1))
        for fraction in range(1, plan.chrome_kills + 1)
    ]
    state_point = int(target_checks * 0.55)
    database_point = int(target_checks * 0.68)

    async def remaining_work() -> bool:
        if repository.outage:
            return False
        summary = await repository.due_session_summary(now=datetime.now(UTC))
        return (
            summary.count == 0
            and stack.scheduler.queue_size == 0
            and stack.scheduler.metrics.currently_checking == 0
        )

    completed = False
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline and not task.done():
        checked = stack.scheduler.metrics.checked
        elapsed = time.perf_counter() - sweep_started
        capacity_contexts = sum(len(slot.contexts) for slot in stack.manager._slots)
        observed.peak_contexts = max(observed.peak_contexts, capacity_contexts)
        if kill_points and checked >= kill_points[0]:
            kill_points.pop(0)
            victims = chrome_main_processes(descendants_of=os.getpid())
            if victims:
                victims[0].send_signal(signal.SIGKILL)
                observed.kill_pids += 1
                observed.kill_times.append(round(elapsed, 3))
        if observed.state_outage is None and checked >= state_point:
            state_store.save_outage = True
            state_store.load_outage = True
            await asyncio.sleep(plan.state_outage_seconds)
            state_store.save_outage = False
            state_store.load_outage = False
            observed.state_outage = (
                round(elapsed, 3),
                round(time.perf_counter() - sweep_started, 3),
            )
            continue
        if observed.database_outage is None and checked >= database_point:
            await repository.sever_connection()
            repository.outage = True
            outage_started = time.perf_counter()
            await asyncio.sleep(plan.database_outage_seconds)
            repository.outage = False
            outage_ended = time.perf_counter()
            observed.database_outage = (
                round(outage_started - sweep_started, 3),
                round(outage_ended - sweep_started, 3),
            )
            failures_at_end = stack.scheduler.metrics.schedule_failures
            iterations_at_end = stack.scheduler.metrics.scheduler_iterations
            while (
                stack.scheduler.metrics.scheduler_iterations <= iterations_at_end
                or stack.scheduler.metrics.schedule_failures > failures_at_end + 5
            ) and time.perf_counter() < deadline:
                if stack.scheduler.metrics.scheduler_iterations > iterations_at_end:
                    break
                await asyncio.sleep(0.01)
            observed.database_first_success_after_outage = round(
                time.perf_counter() - outage_ended, 4
            )
            continue
        if not kill_points and observed.database_outage is not None and await remaining_work():
            # Leases orphaned by the database outage expire and are reclaimed before
            # the due count can reach zero, so this also proves their recovery.
            completed = True
            break
        if int(elapsed) != int(elapsed - 0.25):
            observed.samples.append(
                {
                    "t": round(elapsed, 2),
                    "checked": float(checked),
                    "contexts": float(capacity_contexts),
                    "backlog": float(stack.scheduler.metrics.due_backlog),
                }
            )
        await asyncio.sleep(0.25)
    sweep_seconds = time.perf_counter() - sweep_started

    metrics_text = await _http_get(status_server.port, "/metrics")
    status_text = await _http_get(status_server.port, "/status")
    await stack.scheduler.shutdown(timeout_seconds=5)
    stop.set()
    await task
    contexts_after = (await stack.manager.capacity()).active_contexts
    restart_durations = list(stack.manager.restart_durations)
    await stack.manager.shutdown()
    await status_server.close()
    await asyncio.sleep(0.5)
    chrome_after = len(chrome_main_processes(descendants_of=os.getpid()))

    sessions = {session.session_id: session for session in await repository.list()}
    final_summary = await repository.recovery_summary(now=datetime.now(UTC))
    final_digest = await original_identity_digest(repository, layout.population)
    final_terminal = {
        key: value
        for key, value in (await terminal_snapshot(repository)).items()
        if _index(key) not in layout.expected_permanent_failures
    }
    reconnects = repository.reconnects
    injected_db_failures = repository.injected_failures
    await repository.close()

    def group(indices: Iterable[int]) -> list[QueueSession]:
        return [sessions[session_id_for(index)] for index in indices]

    def checked_since(items: list[QueueSession]) -> bool:
        return all(
            item.last_checked_at is not None and item.last_checked_at >= sweep_started_at
            for item in items
        )

    def checked_and_parked(items: list[QueueSession]) -> bool:
        return all(
            item.last_checked_at is not None
            and item.next_check_at is not None
            and item.next_check_at > sweep_started_at
            for item in items
        )

    def ids_unchanged(items: list[QueueSession]) -> bool:
        return all(item.queue_id == queue_id_for(_index(item.session_id)) for item in items)

    def errors(items: list[QueueSession]) -> dict[str, int]:
        return dict(Counter(item.last_error or "none" for item in items))

    def statuses(items: list[QueueSession]) -> dict[str, int]:
        return dict(Counter(item.status.value for item in items))

    failed_now = {
        _index(session_id)
        for session_id, session in sessions.items()
        if session.status is QueueStatus.FAILED and session.queue_id is not None
    }
    results: list[ScenarioResult] = []
    evidence = "installed Chrome + local simulator + local SQLite/state"

    restart = ScenarioResult(
        key="restart_resume_monitoring",
        title=f"Restart and resume monitoring of {layout.population:,} persisted sessions",
        evidence=evidence,
    )
    resumed = group(range(layout.healthy_due.start, layout.healthy_due.stop))
    restart.check("identity_digest_unchanged", final_digest == baseline_digest)
    restart.check("sweep_completed_before_timeout", completed)
    restart.check("all_resumed_sessions_checked", checked_and_parked(resumed))
    restart.check(
        "no_unexpected_permanent_failures", failed_now == layout.expected_permanent_failures
    )
    restart.check("pre_existing_terminal_states_preserved", final_terminal == baseline_terminal)
    restart.check("no_rows_created", final_summary.total_persisted_sessions == layout.population)
    restart.check("no_contexts_after_shutdown", contexts_after == 0)
    restart.check("no_chrome_processes_after_shutdown", chrome_after == 0)
    restart.check(
        "only_untouched_live_leases_remain",
        final_summary.leased_sessions == len(layout.live_leases),
    )
    checks = stack.scheduler.metrics.checked
    restart.measurements.update(
        {
            "startup_recovery_seconds": round(startup_seconds, 4),
            "browser_start_seconds": round(browser_start_seconds, 4),
            "due_backlog_after_restart": startup_summary.sessions_due,
            "expired_leases_at_restart": startup_summary.expired_leases,
            "sessions_resumed_checked": checks,
            "sweep_seconds": round(sweep_seconds, 3),
            "checks_per_second": round(checks / sweep_seconds, 2) if sweep_seconds else None,
            "check_duration_seconds_average": round(
                _metric(metrics, "queue_check_duration_seconds_sum")
                / max(1.0, _metric(metrics, "queue_check_duration_seconds_count")),
                4,
            ),
            "restore_duration_average_seconds": round(
                _metric(metrics, "session_restore_duration_seconds_sum")
                / max(1.0, _metric(metrics, "session_restore_duration_seconds_count")),
                4,
            ),
            "peak_active_contexts": observed.peak_contexts,
            "maximum_queue_depth": stack.scheduler.metrics.maximum_queue_depth,
            "maximum_concurrent_checks": stack.scheduler.metrics.maximum_concurrent_checks,
            "final_status_counts": {
                status.value: count
                for status, count in final_summary.status_counts.items()
                if count
            },
            "valid_queue_ids_after": final_summary.valid_queue_ids,
            "lost_queue_ids_after": final_summary.lost_queue_ids,
            "progress_samples": observed.samples[:: max(1, len(observed.samples) // 40)],
        }
    )
    results.append(restart.finish())

    crash = ScenarioResult(
        key="chrome_process_crashes",
        title="Chrome process crash, repeated over time",
        evidence=evidence + " (SIGKILL of a Chrome browser process)",
    )
    crash.check("all_kills_delivered", observed.kill_pids == plan.chrome_kills)
    crash.check("every_crash_detected", len(restart_durations) >= observed.kill_pids)
    crash.check(
        "every_restart_succeeded",
        _metric(metrics, "browser_restart_failures_total") == 0,
    )
    crash.check("monitoring_continued", completed and checked_and_parked(resumed))
    crash.check("identities_unchanged", final_digest == baseline_digest)
    crash.check("no_contexts_leaked", contexts_after == 0)
    crash.measurements.update(
        {
            "chrome_kills": observed.kill_pids,
            "kill_times_seconds_into_sweep": observed.kill_times,
            "browser_crashes_detected": _metric(metrics, "browser_crashes_total"),
            "browser_restart_seconds": _stats(restart_durations),
            "contexts_lost": _metric(metrics, "browser_contexts_lost_total"),
            "navigation_failures_total": _metric(metrics, "navigation_failures_total"),
        }
    )
    results.append(crash.finish())

    def fault_result(
        key: str,
        title: str,
        items: list[QueueSession],
        *,
        expected_status: set[QueueStatus],
        expected_error: str | set[str] | None,
        extra: dict[str, object] | None = None,
    ) -> ScenarioResult:
        item = ScenarioResult(key=key, title=title, evidence=evidence)
        item.check("queue_ids_unchanged", ids_unchanged(items))
        item.check("all_checked", checked_since(items))
        item.check("expected_status", all(entry.status in expected_status for entry in items))
        if expected_error is not None:
            allowed = {expected_error} if isinstance(expected_error, str) else expected_error
            item.check(
                "failure_observable_in_last_error",
                all(entry.last_error in allowed for entry in items),
            )
        item.check(
            "no_replacement_rows", final_summary.total_persisted_sessions == layout.population
        )
        item.measurements.update(
            {
                "sessions": len(items),
                "statuses": statuses(items),
                "last_errors": errors(items),
                **(extra or {}),
            }
        )
        return item.finish()

    results.append(
        fault_result(
            "context_creation_failure",
            "Context creation failure (Chrome rejects restored storage_state)",
            group(layout.context_failure),
            expected_status={QueueStatus.CONNECTION_LOST},
            expected_error="restore:STATE_CONTEXT_FAILED",
            extra={
                "context_creation_failures_total": _metric(
                    metrics, "browser_context_creation_failures_total"
                )
            },
        )
    )
    results.append(
        fault_result(
            "navigation_failure",
            "Navigation failure (empty response) and navigation timeout",
            group(layout.navigation_failure) + group(layout.navigation_timeout),
            expected_status={QueueStatus.CONNECTION_LOST},
            # HYBRID falls back to storage_state after a failed transfer navigation;
            # a failed fallback navigation is reported as STATE_CONTEXT_FAILED.
            expected_error={"restore:NAVIGATION_FAILED", "restore:STATE_CONTEXT_FAILED"},
            extra={
                "navigation_timeouts_total": _metric(metrics, "navigation_timeouts_total"),
                "timeout_sessions": len(layout.navigation_timeout),
            },
        )
    )
    results.append(
        fault_result(
            "transfer_restore_failure",
            "Transfer restore failure with storage_state fallback",
            group(layout.transfer_fallback),
            expected_status={
                QueueStatus.PRE_QUEUE,
                QueueStatus.ACTIVE_QUEUE,
                QueueStatus.SERVICED_SOON,
            },
            expected_error=None,
            extra={
                "transfer_restore_failures_total": _metric(
                    metrics, "transfer_restore_failures_total"
                )
            },
        )
    )
    storage = fault_result(
        "storage_state_restore_failure",
        "storage_state restore failure (corrupt file permanent, unreadable file transient)",
        group(layout.storage_corrupt) + group(layout.storage_unavailable),
        expected_status={QueueStatus.FAILED, QueueStatus.CONNECTION_LOST},
        expected_error=None,
        extra={"state_restore_failures_total": _metric(metrics, "state_restore_failures_total")},
    )
    storage.check(
        "corrupt_state_fails_permanently",
        all(
            entry.status is QueueStatus.FAILED and entry.last_error == "restore:STATE_CORRUPT"
            for entry in group(layout.storage_corrupt)
        ),
    )
    storage.check(
        "unreadable_state_stays_retryable",
        all(
            entry.status is QueueStatus.CONNECTION_LOST
            and entry.last_error == "restore:STATE_UNAVAILABLE"
            for entry in group(layout.storage_unavailable)
        ),
    )
    results.append(storage.finish())
    mismatch = fault_result(
        "identity_mismatch",
        "Identity mismatch",
        group(layout.identity_mismatch),
        expected_status={QueueStatus.FAILED},
        expected_error="restore:IDENTITY_MISMATCH",
        extra={"identity_mismatches_total": _metric(metrics, "identity_mismatches_total")},
    )
    mismatch.check(
        "mismatch_metric_counted",
        _metric(metrics, "identity_mismatches_total") >= len(layout.identity_mismatch),
    )
    results.append(mismatch.finish())

    database_result = ScenarioResult(
        key="database_interruption",
        title="Database connection interruption",
        evidence=evidence + " (severed handle + injected OperationalError window)",
    )
    database_result.check("outage_injected", injected_db_failures > 0)
    database_result.check("connection_reopened", reconnects >= 1)
    database_result.check("scheduler_survived", completed)
    database_result.check("identities_unchanged", final_digest == baseline_digest)
    database_result.check(
        "orphaned_leases_recovered",
        final_summary.leased_sessions == len(layout.live_leases)
        and stack.scheduler.metrics.own_expired_leases_reclaimed
        >= stack.scheduler.metrics.lease_release_failures,
    )
    database_result.measurements.update(
        {
            "outage_window_seconds_into_sweep": observed.database_outage,
            "injected_operation_failures": injected_db_failures,
            "scheduler_schedule_failures": stack.scheduler.metrics.schedule_failures,
            "lease_release_failures": stack.scheduler.metrics.lease_release_failures,
            "repository_reconnects": reconnects,
            "first_successful_schedule_after_outage_seconds": (
                observed.database_first_success_after_outage
            ),
            "lease_recoveries_total": _metric(metrics, "monitoring_lease_recoveries_total"),
            "own_expired_leases_reclaimed_after_outage": (
                stack.scheduler.metrics.own_expired_leases_reclaimed
            ),
            "repository_errors": {
                operation: _metric(metrics, "repository_errors_total", {"operation": operation})
                for operation in ("schedule", "release", "repark", "status")
            },
        }
    )
    results.append(database_result.finish())

    state_result = ScenarioResult(
        key="state_storage_interruption",
        title="State-storage interruption (save and load outage window)",
        evidence=evidence + " (injected OSError/StateUnreadableError window)",
    )
    state_result.check(
        "outage_injected",
        state_store.injected_save_failures + state_store.injected_load_failures > 0,
    )
    state_result.check(
        "no_session_failed_by_outage",
        failed_now == layout.expected_permanent_failures,
    )
    state_result.check("identities_unchanged", final_digest == baseline_digest)
    state_result.measurements.update(
        {
            "outage_window_seconds_into_sweep": observed.state_outage,
            "injected_save_failures": state_store.injected_save_failures,
            "injected_load_failures": state_store.injected_load_failures,
            "state_refresh_failures_total": _metric(metrics, "state_refresh_failures_total"),
        }
    )
    results.append(state_result.finish())

    observability = _audit_exposition(metrics_text, status_text, simulator)
    observability["log"] = _audit_log(log_path, simulator)
    return results, observability


async def _http_get(port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    body = await reader.read()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return body.decode("utf-8", errors="replace").split("\r\n\r\n", 1)[-1]


def _audit_exposition(
    metrics_text: str,
    status_text: str,
    simulator: LocalQueueSimulator,
) -> dict[str, object]:
    series = [line for line in metrics_text.splitlines() if line and not line.startswith("#")]
    label_names: set[str] = set()
    for line in series:
        if "{" in line:
            labels = line.split("{", 1)[1].split("}", 1)[0]
            for pair in labels.split(","):
                if "=" in pair:
                    label_names.add(pair.split("=", 1)[0])
    status_rows = {
        line.split("  ", 1)[0].strip(): line.split("  ", 1)[-1].strip()
        for line in status_text.splitlines()
        if "  " in line
    }
    return {
        "metrics_series": len(series),
        "label_names": sorted(label_names),
        "forbidden_labels_present": sorted(label_names & FORBIDDEN_LABEL_NAMES),
        "queue_ids_in_exposition": "sim-q-" in metrics_text,
        "session_ids_in_exposition": "sim-session-" in metrics_text,
        "transfer_urls_in_exposition": simulator.queue_url in metrics_text,
        "status_rows": status_rows,
    }


def _audit_log(log_path: Path, simulator: LocalQueueSimulator) -> dict[str, object]:
    if not log_path.exists():
        return {"lines": 0}
    text = log_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    return {
        "lines": len(lines),
        "bytes": len(text.encode()),
        "transfer_urls_present": simulator.queue_url in text or "127.0.0.1:" in text,
        "lines_with_session_id": sum('"session_id"' in line for line in lines),
        "lines_with_run_id": sum('"run_id"' in line for line in lines),
        "events": dict(
            Counter(json.loads(line).get("message", "") for line in lines).most_common(12)
        ),
    }


async def scenario_worker_crash(
    *,
    database: Path,
    state_directory: Path,
    simulator: LocalQueueSimulator,
    layout: PopulationLayout,
    reactivate: int,
    kill_after_checks: int,
    lease_seconds: float,
) -> list[ScenarioResult]:
    """SIGKILL a separate monitoring worker process, then restart and recover."""

    crash = ScenarioResult(
        key="worker_process_crash",
        title="Worker process crash (single machine, SIGKILL)",
        evidence="separate OS process with installed Chrome + local simulator + SQLite",
    )
    restart = ScenarioResult(
        key="worker_process_restart",
        title="Worker process restart and lease recovery",
        evidence="separate OS process killed; new process recovers leases",
    )
    targets = [session_id_for(i) for i in list(layout.future)[:reactivate]]
    now = datetime.now(UTC)
    connection = sqlite3.connect(database)
    connection.executemany(
        "UPDATE queue_sessions SET next_check_at = ? WHERE session_id = ?",
        [(now.isoformat(), session_id) for session_id in targets],
    )
    connection.commit()
    connection.close()
    repository = SQLiteSessionRepository(database)
    baseline_digest = await original_identity_digest(repository, layout.population)
    await repository.close()

    chrome_before = {process.pid for process in chrome_main_processes()}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "queue_load_test.harness.phase4_recovery",
        "--child-worker",
        "--database",
        str(database),
        "--state-directory",
        str(state_directory),
        "--simulator-port",
        str(simulator.port),
        "--lease-seconds",
        str(lease_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdout is not None
    child_checked = 0
    deadline = time.perf_counter() + 120
    while time.perf_counter() < deadline:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
        if not line:
            break
        text = line.decode().strip()
        if text.startswith("CHECKED "):
            child_checked = int(text.split()[1])
            if child_checked >= kill_after_checks:
                break
    child_chrome = {p.pid for p in chrome_main_processes(descendants_of=process.pid)}
    killed_at = time.perf_counter()
    process.send_signal(signal.SIGKILL)
    await process.wait()
    await asyncio.sleep(5)
    orphans = [
        p.pid
        for p in chrome_main_processes()
        if p.pid not in chrome_before and p.pid in child_chrome
    ]
    for orphan in chrome_main_processes():
        if orphan.pid in orphans:
            with contextlib.suppress(Exception):
                orphan.kill()

    check = sqlite3.connect(database)
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    child_leases = check.execute(
        "SELECT COUNT(*) FROM queue_sessions WHERE worker_id = ?",
        (CHILD_OWNER,),
    ).fetchone()[0]
    check.close()
    crash.check("child_made_progress", child_checked >= kill_after_checks)
    crash.check("database_integrity_ok", integrity == "ok")
    crash.check("child_leases_bounded_by_queue_and_workers", 0 <= child_leases <= 30)
    crash.check("no_orphan_chrome_processes", not orphans)
    repository = SQLiteSessionRepository(database)
    crash.check(
        "identities_unchanged",
        await original_identity_digest(repository, layout.population) == baseline_digest,
    )
    await repository.close()
    crash.measurements.update(
        {
            "child_checks_before_kill": child_checked,
            "leases_held_by_killed_worker": child_leases,
            "orphan_chrome_processes_after_5s": len(orphans),
            "child_chrome_processes": len(child_chrome),
            "sqlite_integrity_check": integrity,
        }
    )

    # Restarted worker: a fresh process-local scheduler in this process.
    repository = SQLiteSessionRepository(database)
    state_store = FileSystemStateStore(state_directory)
    metrics = PrometheusMetrics()
    stack = build_monitoring_stack(
        repository=repository,
        state_store=state_store,
        simulator=simulator,
        metrics=metrics,
        scheduler_id="restarted-worker",
        chrome_processes=1,
        contexts_per_browser=20,
        workers=10,
        queue_capacity=20,
        lease_seconds=lease_seconds,
    )
    await stack.manager.start()
    stop = asyncio.Event()
    task = asyncio.create_task(stack.scheduler.run(stop))
    restart_started = time.perf_counter()

    async def recovered_all() -> bool:
        remaining = sqlite3.connect(database)
        try:
            leased = remaining.execute(
                "SELECT COUNT(*) FROM queue_sessions WHERE worker_id = ?", (CHILD_OWNER,)
            ).fetchone()[0]
        finally:
            remaining.close()
        return int(leased) == 0 and stack.scheduler.metrics.expired_leases_recovered >= child_leases

    leases_recovered = await _wait_for(recovered_all, timeout=lease_seconds + 60, interval=0.1)
    time_to_recover = time.perf_counter() - restart_started
    since_kill = time.perf_counter() - killed_at

    async def drained() -> bool:
        summary = await repository.due_session_summary(now=datetime.now(UTC))
        return (
            summary.count == 0
            and stack.scheduler.queue_size == 0
            and stack.scheduler.metrics.currently_checking == 0
        )

    all_done = await _wait_for(drained, timeout=240, interval=0.25)
    await stack.scheduler.shutdown(timeout_seconds=5)
    stop.set()
    await task
    contexts_after = (await stack.manager.capacity()).active_contexts
    await stack.manager.shutdown()
    sessions = {s.session_id: s for s in await repository.list()}
    restart_digest = await original_identity_digest(repository, layout.population)
    await repository.close()
    reactivated = [sessions[session_id] for session_id in targets]
    restart.check("expired_leases_recovered", leases_recovered)
    restart.check("remaining_work_completed", all_done)
    restart.check(
        "reactivated_sessions_all_checked",
        all(s.next_check_at is not None and s.next_check_at > now for s in reactivated),
    )
    restart.check(
        "no_session_failed",
        all(s.status is not QueueStatus.FAILED for s in reactivated),
    )
    restart.check("identities_unchanged", restart_digest == baseline_digest)
    restart.check("no_contexts_after_shutdown", contexts_after == 0)
    restart.measurements.update(
        {
            "lease_seconds": lease_seconds,
            "expired_leases_recovered": stack.scheduler.metrics.expired_leases_recovered,
            "restart_to_all_leases_recovered_seconds": round(time_to_recover, 3),
            "kill_to_all_leases_recovered_seconds": round(since_kill, 3),
            "checks_by_restarted_worker": stack.scheduler.metrics.checked,
            "reactivated_sessions": len(targets),
        }
    )
    return [crash.finish(), restart.finish()]


async def scenario_shutdown_during_creation(
    *,
    database: Path,
    state_store: FileSystemStateStore,
    simulator: LocalQueueSimulator,
    layout: PopulationLayout,
    interrupt_after: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key="shutdown_during_creation",
        title="Application shutdown (forced cancellation) during creation, then resume",
        evidence="installed Chrome + local simulator + local SQLite/state",
    )
    repository = SQLiteSessionRepository(database)
    original = await original_identity_digest(repository, layout.population)
    before = await repository.recovery_summary(now=datetime.now(UTC))
    metrics = PrometheusMetrics()
    manager = BrowserManager(
        chrome_process_count=2,
        max_contexts_per_browser=25,
        max_active_contexts=50,
        observability=metrics,
    )

    def controller(
        manager: BrowserManager,
        repository: SQLiteSessionRepository,
    ) -> SessionCreationController:
        creator = QueueSessionCreator(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            staging_url=simulator.queue_url,
            state_directory=state_store.directory,
            mode=SessionMode.HYBRID,
            retry_policy=CreationRetryPolicy(max_attempts=3, initial_backoff_seconds=0.1),
            navigation_timeout_ms=5_000,
            live_page_timeout_seconds=5,
            observation_interval_seconds=0.05,
            observability=metrics,
        )
        return SessionCreationController(
            repository=repository,
            handler=creator,
            target_queue_ids=layout.population,
            worker_count=10,
            queue_capacity=10,
            observability=metrics,
            identity_replacement_limit=0,
        )

    await manager.start()
    first = controller(manager, repository)
    first_task = asyncio.create_task(first.run())
    while first.metrics.unique_ids_acquired < interrupt_after and not first_task.done():
        await asyncio.sleep(0.01)
    in_flight = first.metrics.currently_creating
    cancel_started = time.perf_counter()
    first_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first_task
    cancel_seconds = time.perf_counter() - cancel_started
    contexts_after_cancel = (await manager.capacity()).active_contexts
    interrupted = await repository.recovery_summary(now=datetime.now(UTC))
    await manager.shutdown()
    await repository.close()

    # Restart: a new repository, browser manager, and controller resume the deficit.
    repository = SQLiteSessionRepository(database)
    manager = BrowserManager(
        chrome_process_count=2,
        max_contexts_per_browser=25,
        max_active_contexts=50,
        observability=metrics,
    )
    await manager.start()
    resume_started = time.perf_counter()
    second = controller(manager, repository)
    second_metrics = await second.run()
    resume_seconds = time.perf_counter() - resume_started
    contexts_after_resume = (await manager.capacity()).active_contexts
    await manager.shutdown()
    after = await repository.recovery_summary(now=datetime.now(UTC))
    audit = await StateConsistencyChecker(repository, state_store).check()
    final_original = await original_identity_digest(repository, layout.population)
    await repository.close()

    lost = before.lost_queue_ids
    effective_target = layout.population - lost
    result.check("original_identities_unchanged", final_original == original)
    result.check("no_contexts_after_forced_cancel", contexts_after_cancel == 0)
    result.check("no_contexts_after_resume", contexts_after_resume == 0)
    result.check(
        "committed_ids_survive_interruption",
        interrupted.valid_queue_ids >= before.valid_queue_ids + interrupt_after,
    )
    result.check("resume_reaches_effective_target", after.valid_queue_ids == effective_target)
    result.check("no_overshoot", after.valid_queue_ids <= effective_target)
    result.check("lost_identities_not_replaced", second_metrics.replacement_blocked)
    result.check(
        "no_orphaned_or_missing_state",
        audit.count("orphaned_state_file") == 0 and audit.count("missing_state_file") == 0,
    )
    result.measurements.update(
        {
            "valid_before": before.valid_queue_ids,
            "lost_identities_before": lost,
            "effective_target_with_replacement_limit_0": effective_target,
            "ids_committed_before_interrupt": interrupted.valid_queue_ids - before.valid_queue_ids,
            "in_flight_at_cancel": in_flight,
            "forced_cancel_seconds": round(cancel_seconds, 4),
            "resume_seconds": round(resume_seconds, 3),
            "ids_created_by_resume": second_metrics.unique_ids_acquired,
            "valid_after": after.valid_queue_ids,
            "creation_attempts_total": _metric(metrics, "queue_creation_attempts_total"),
            "state_audit_findings": dict(Counter(f.kind for f in audit.findings)),
        }
    )
    return result.finish()


# --- report --------------------------------------------------------------------------


@dataclass(slots=True)
class Phase4RecoveryReport:
    generated_at: datetime
    population: int
    platform: str
    chrome_available: bool
    scenarios: list[ScenarioResult]
    not_applicable: dict[str, str]
    observability: dict[str, object]
    seed_seconds: float
    total_seconds: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["summary"] = dict(Counter(s.outcome.value for s in self.scenarios))
        return payload

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )

    def render_text(self) -> str:
        lines = [
            f"Phase 4 controlled recovery scenarios ({self.population:,} persisted sessions)",
            "Evidence: local only; no Queue-it or staging traffic.",
        ]
        for scenario in self.scenarios:
            failed = [name for name, ok in scenario.checks.items() if not ok]
            lines.append(
                f"  {scenario.outcome.value:<7} {scenario.title}"
                + (f"  [failed: {', '.join(failed)}]" if failed else "")
            )
        for key, reason in self.not_applicable.items():
            lines.append(f"  UNKNOWN {key}: {reason}")
        lines.append(f"Total {self.total_seconds:.1f} s")
        return "\n".join(lines) + "\n"


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Cannot serialize {type(value).__name__}")


async def run_phase4_recovery(
    work_directory: Path,
    *,
    population: int = 10_000,
    group_size: int = 20,
    restart_repetitions: int = 5,
    shutdown_after_checks: int = 300,
    creation_interrupt_after: int = 30,
    worker_crash_sessions: int = 1_000,
    worker_kill_after_checks: int = 150,
    worker_lease_seconds: float = 10.0,
    sweep_timeout_seconds: float = 900.0,
    fault_plan: SweepFaultPlan | None = None,
) -> Phase4RecoveryReport:
    if work_directory.exists() and any(work_directory.iterdir()):
        raise RuntimeError("Use an absent or empty work directory")
    work_directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    layout = PopulationLayout(population, group_size)
    database = work_directory / "sessions.sqlite3"
    state_directory = work_directory / "state"
    log_path = work_directory / "recovery.log.jsonl"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(JsonLogFormatter(run_id=f"phase4-recovery-{int(time.time())}"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    simulator = LocalQueueSimulator()
    await simulator.start()
    state_store = FaultInjectingStateStore(state_directory)
    scenarios: list[ScenarioResult] = []
    observability: dict[str, object] = {}
    chrome_available = shutil.which("google-chrome") is not None or sys.platform == "darwin"
    try:
        seed_started = time.perf_counter()
        await seed_population(database, state_store, simulator, layout, now=datetime.now(UTC))
        seed_seconds = time.perf_counter() - seed_started
        scenarios.append(
            await scenario_restart(database, state_store, layout, repetitions=restart_repetitions)
        )
        scenarios.append(await scenario_expired_leases(database, layout))
        apply_fault_groups(layout, simulator, state_store)
        await write_invalid_context_states(layout, state_store)
        scenarios.append(
            await scenario_shutdown_during_monitoring(
                database=database,
                state_store=state_store,
                simulator=simulator,
                layout=layout,
                stop_after_checks=shutdown_after_checks,
            )
        )
        activate_fault_groups(database, layout)
        sweep_results, observability = await scenario_resume_sweep(
            database=database,
            state_store=state_store,
            simulator=simulator,
            layout=layout,
            plan=fault_plan or SweepFaultPlan(),
            timeout_seconds=sweep_timeout_seconds,
            log_path=log_path,
        )
        scenarios.extend(sweep_results)
        scenarios.extend(
            await scenario_worker_crash(
                database=database,
                state_directory=state_directory,
                simulator=simulator,
                layout=layout,
                reactivate=worker_crash_sessions,
                kill_after_checks=worker_kill_after_checks,
                lease_seconds=worker_lease_seconds,
            )
        )
        scenarios.append(
            await scenario_shutdown_during_creation(
                database=database,
                state_store=state_store,
                simulator=simulator,
                layout=layout,
                interrupt_after=max(
                    1, min(creation_interrupt_after, len(layout.creation_failed) // 3)
                ),
            )
        )
    finally:
        await simulator.close()
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous_level)
    order = [
        "shutdown_during_creation",
        "shutdown_during_monitoring",
        "restart_with_persisted_population",
        "restart_resume_monitoring",
        "chrome_process_crashes",
        "context_creation_failure",
        "navigation_failure",
        "transfer_restore_failure",
        "storage_state_restore_failure",
        "identity_mismatch",
        "database_interruption",
        "state_storage_interruption",
        "expired_leases",
        "worker_process_crash",
        "worker_process_restart",
    ]
    scenarios.sort(key=lambda s: order.index(s.key) if s.key in order else len(order))
    return Phase4RecoveryReport(
        generated_at=datetime.now(UTC),
        population=population,
        platform=f"{sys.platform} / Python {sys.version.split()[0]}",
        chrome_available=chrome_available,
        scenarios=scenarios,
        not_applicable={
            "distributed_worker_crash": (
                "Phase 4 is single-machine (docs/phase4_distributed_worker_decision.md); "
                "no distributed worker exists, so node-level loss was not executed."
            ),
            "distributed_node_restart": (
                "No multi-node deployment exists; only single-machine process restart ran."
            ),
            "real_queue_it_recovery": (
                "No authorised staging configuration; every scenario used a local simulator."
            ),
        },
        observability=observability,
        seed_seconds=round(seed_seconds, 3),
        total_seconds=round(time.perf_counter() - started, 3),
    )


# --- child worker (for the SIGKILL scenario) -----------------------------------------


async def _child_worker(args: argparse.Namespace) -> None:
    simulator = LocalQueueSimulator()
    simulator.port = args.simulator_port
    repository = SQLiteSessionRepository(args.database)
    state_store = FileSystemStateStore(args.state_directory)
    stack = build_monitoring_stack(
        repository=repository,
        state_store=state_store,
        simulator=simulator,
        metrics=PrometheusMetrics(),
        scheduler_id=CHILD_OWNER,
        chrome_processes=1,
        contexts_per_browser=10,
        workers=10,
        queue_capacity=20,
        lease_seconds=args.lease_seconds,
    )
    await stack.manager.start()
    stop = asyncio.Event()
    task = asyncio.create_task(stack.scheduler.run(stop))
    while not task.done():
        print(f"CHECKED {stack.scheduler.metrics.checked}", flush=True)
        await asyncio.sleep(0.1)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run controlled Phase 4 recovery scenarios against local resources only"
    )
    parser.add_argument("--work-directory", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--population", type=int, default=10_000)
    parser.add_argument("--group-size", type=int, default=20)
    parser.add_argument("--restarts", type=int, default=5)
    parser.add_argument("--worker-crash-sessions", type=int, default=1_000)
    parser.add_argument("--sweep-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--child-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--state-directory", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--simulator-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--lease-seconds", type=float, default=10.0, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.child_worker:
        asyncio.run(_child_worker(args))
        return
    if psutil is None:
        raise SystemExit("Install the benchmark extra (psutil) to run Chrome crash scenarios")

    def run(directory: Path) -> Phase4RecoveryReport:
        return asyncio.run(
            run_phase4_recovery(
                directory,
                population=args.population,
                group_size=args.group_size,
                restart_repetitions=args.restarts,
                worker_crash_sessions=args.worker_crash_sessions,
                sweep_timeout_seconds=args.sweep_timeout_seconds,
            )
        )

    if args.work_directory is not None:
        report = run(args.work_directory)
    else:
        with tempfile.TemporaryDirectory(prefix="queue-load-test-phase4-recovery-") as directory:
            report = run(Path(directory) / "run")
    if args.report is not None:
        report.write_json(args.report)
    print(report.render_text())


if __name__ == "__main__":
    main()
