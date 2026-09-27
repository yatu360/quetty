"""Controlled Phase 4 monitoring benchmark for 10,000 parked sessions."""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import random
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from queue_load_test.harness.resource_benchmark import (
    ProcessResourceProbe,
    ProcessResourceSnapshot,
    PsutilProcessResourceProbe,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import DueSessionSummary, SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringOutcome, ParkedSessionScheduler, PollingPolicy

PHASE4_POPULATION = 10_000
_BASE_TIME = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class LatencySummary:
    count: int
    average_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    p99_seconds: float | None
    maximum_seconds: float | None


@dataclass(frozen=True, slots=True)
class AveragePeak:
    average: float | None
    peak: float | None


@dataclass(frozen=True, slots=True)
class ResourceSummary:
    application_cpu_percent: AveragePeak
    application_ram_bytes: AveragePeak
    chrome_cpu_percent: AveragePeak
    chrome_ram_bytes: AveragePeak


@dataclass(frozen=True, slots=True)
class SweepResult:
    sweep: int
    sessions_due: int
    sessions_checked: int
    successful_checks: int
    failed_checks: int
    checks_per_second: float
    full_sweep_duration_seconds: float
    throughput_derived_10000_sweep_seconds: float
    check_duration: LatencySummary
    scheduler_query_latency: LatencySummary
    claim_latency: LatencySummary
    update_latency: LatencySummary
    lease_release_latency: LatencySummary
    backlog_start: int
    backlog_peak: int
    backlog_end: int
    oldest_overdue_start_seconds: float
    oldest_overdue_peak_seconds: float
    oldest_overdue_end_seconds: float
    maximum_queue_depth: int
    maximum_active_workers: int
    lease_conflicts: int


@dataclass(frozen=True, slots=True)
class AdaptiveCheckpoint:
    offset_seconds: int
    due_before_processing: int
    checked: int
    backlog_after_processing: int
    oldest_overdue_before_seconds: float
    oldest_overdue_after_seconds: float


@dataclass(frozen=True, slots=True)
class AdaptiveResult:
    sessions_scheduled: int
    sessions_checked: int
    successful_checks: int
    failed_checks: int
    processing_duration_seconds: float
    checks_per_processing_second: float
    schedule_window_seconds: float
    completion_offset_seconds: float
    jitter_min_seconds: float
    jitter_max_seconds: float
    distinct_next_check_times: int
    maximum_one_second_bucket: int
    maximum_due_backlog: int
    maximum_oldest_overdue_seconds: float
    backlog_end: int
    oldest_overdue_end_seconds: float
    maximum_queue_depth: int
    maximum_active_workers: int
    lease_conflicts: int
    check_duration: LatencySummary
    scheduler_query_latency: LatencySummary
    claim_latency: LatencySummary
    checkpoints: tuple[AdaptiveCheckpoint, ...]


@dataclass(frozen=True, slots=True)
class Phase4MonitoringReport:
    generated_at: datetime
    environment: str
    staging_status: str
    persisted_queue_it_population_available: bool
    total_persisted_sessions: int
    seed_duration_seconds: float
    worker_count: int
    queue_capacity: int
    claim_batch_size: int
    lease_seconds: float
    synthetic_check_delay_seconds: float
    scenario_a: tuple[SweepResult, ...]
    scenario_b: AdaptiveResult
    resources: ResourceSummary
    resource_sample_count: int
    active_context_peak: int
    context_acquisition_wait: LatencySummary | None
    restore_attempts: int | None
    restore_successes: int | None
    restore_failures: int | None
    transfer_restore_successes: int | None
    storage_state_restore_successes: int | None
    identity_mismatches: int | None
    browser_crashes: int | None
    navigation_failures: int | None
    sustainable_queue_it_cadence: str
    scope_warning: str = (
        "Synthetic results measure SQLite, leases, fixed workers, bounded queues, "
        "persistence, adaptive due times, jitter, and process resources. They do not "
        "measure Queue-it restoration, navigation, Chrome stability, or page latency."
    )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def render_text(self) -> str:
        lines = [
            "Phase 4 10,000-session monitoring benchmark",
            "",
            f"Environment: {self.environment}",
            f"Queue-it staging: {self.staging_status}",
            f"Persisted synthetic sessions: {self.total_persisted_sessions}",
            (
                f"Bounds: {self.worker_count} workers, queue {self.queue_capacity}, "
                f"claim {self.claim_batch_size}"
            ),
            "",
            "Scenario A - deliberate all-due sweeps",
        ]
        for sweep in self.scenario_a:
            lines.append(
                f"  Sweep {sweep.sweep}: {sweep.sessions_checked}/"
                f"{sweep.sessions_due} checked, {sweep.failed_checks} failed, "
                f"{sweep.full_sweep_duration_seconds:.3f}s, "
                f"{sweep.checks_per_second:.2f} checks/s, "
                f"p95 check {_seconds(sweep.check_duration.p95_seconds)}, "
                f"backlog {sweep.backlog_start}->{sweep.backlog_end}"
            )
        lines.extend(
            (
                "",
                "Scenario B - adaptive schedules and jitter",
                (
                    f"  Checked: {self.scenario_b.sessions_checked}/"
                    f"{self.scenario_b.sessions_scheduled}"
                ),
                (
                    f"  Processing throughput: "
                    f"{self.scenario_b.checks_per_processing_second:.2f} checks/s"
                ),
                (
                f"  Simulated schedule window: "
                f"{self.scenario_b.schedule_window_seconds:.1f}s"
                ),
                (
                    f"  Maximum 1-second due bucket: "
                    f"{self.scenario_b.maximum_one_second_bucket}"
                ),
                f"  Maximum observed backlog: {self.scenario_b.maximum_due_backlog}",
                f"  Ending backlog: {self.scenario_b.backlog_end}",
                (
                    f"  Last due work completed at simulated +"
                    f"{self.scenario_b.completion_offset_seconds:.1f}s"
                ),
                "",
                f"Queue-it sustainable cadence: {self.sustainable_queue_it_cadence}",
                (
                    "Restore, browser, navigation, identity, and active-context results: "
                    "UNKNOWN (authorised 10,000-session staging population unavailable)"
                ),
                "",
                self.scope_warning,
            )
        )
        return "\n".join(lines) + "\n"


@dataclass(slots=True)
class _Timings:
    due_summary: list[float]
    claims: list[float]
    updates: list[float]
    releases: list[float]

    @classmethod
    def empty(cls) -> _Timings:
        return cls([], [], [], [])


class _TimedRepository:
    """Benchmark-only delegator that records aggregate database operation latency."""

    def __init__(self, repository: SQLiteSessionRepository) -> None:
        self.repository = repository
        self.timings = _Timings.empty()

    def reset_timings(self) -> None:
        self.timings = _Timings.empty()

    async def due_session_summary(self, *, now: datetime) -> DueSessionSummary:
        return await self._timed(
            self.timings.due_summary,
            self.repository.due_session_summary(now=now),
        )

    async def count_due_sessions(self, *, now: datetime) -> int:
        return (await self.due_session_summary(now=now)).count

    async def claim_due_sessions(self, **kwargs: Any) -> list[QueueSession]:
        return await self._timed(
            self.timings.claims,
            self.repository.claim_due_sessions(**kwargs),
        )

    async def update(
        self,
        session: QueueSession,
        progress: QueueProgress | None = None,
    ) -> QueueSession:
        return await self._timed(
            self.timings.updates,
            self.repository.update(session, progress),
        )

    async def release_lease(
        self,
        session_id: str,
        *,
        worker_id: str | None = None,
    ) -> bool:
        return await self._timed(
            self.timings.releases,
            self.repository.release_lease(session_id, worker_id=worker_id),
        )

    async def _timed(
        self,
        destination: list[float],
        operation: Awaitable[_T],
    ) -> _T:
        started = time.perf_counter()
        try:
            return await operation
        finally:
            destination.append(time.perf_counter() - started)


class _SyntheticHandler:
    def __init__(
        self,
        repository: _TimedRepository,
        *,
        clock: Callable[[], datetime],
        target_checks: int,
        next_delay_seconds: float,
        check_delay_seconds: float,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._target_checks = target_checks
        self._next_delay_seconds = next_delay_seconds
        self._check_delay_seconds = check_delay_seconds
        self.checked = 0
        self.successful = 0
        self.failed = 0
        self.latencies: list[float] = []
        self.complete = asyncio.Event()
        if target_checks == 0:
            self.complete.set()

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        started = time.perf_counter()
        success = False
        try:
            if self._check_delay_seconds:
                await asyncio.sleep(self._check_delay_seconds)
            session.last_checked_at = self._clock()
            session.next_check_at = self._clock() + timedelta(
                seconds=self._next_delay_seconds
            )
            await self._repository.update(session)
            success = True
            return MonitoringOutcome(
                session_id=session.session_id,
                success=True,
                observed_status=session.status,
                next_check_at=session.next_check_at,
                queue_update_stale=False,
                progress_changed=False,
            )
        finally:
            self.checked += 1
            self.successful += int(success)
            self.failed += int(not success)
            self.latencies.append(time.perf_counter() - started)
            if self.checked >= self._target_checks:
                self.complete.set()


async def run_phase4_monitoring_benchmark(
    database: Path,
    *,
    report_path: Path,
    population: int = PHASE4_POPULATION,
    worker_count: int = 20,
    queue_capacity: int = 50,
    claim_batch_size: int = 50,
    lease_seconds: float = 120.0,
    scheduler_tick_seconds: float = 0.001,
    check_delay_seconds: float = 0.001,
    repeated_sweeps: int = 2,
    sample_interval_seconds: float = 1.0,
    process_probe: ProcessResourceProbe | None = None,
) -> Phase4MonitoringReport:
    """Run distinct all-due and adaptive synthetic monitoring scenarios."""

    _validate_configuration(
        population=population,
        worker_count=worker_count,
        queue_capacity=queue_capacity,
        claim_batch_size=claim_batch_size,
        lease_seconds=lease_seconds,
        scheduler_tick_seconds=scheduler_tick_seconds,
        check_delay_seconds=check_delay_seconds,
        repeated_sweeps=repeated_sweeps,
        sample_interval_seconds=sample_interval_seconds,
    )
    repository = SQLiteSessionRepository(database)
    if await repository.list():
        await repository.close()
        raise ValueError("Phase 4 synthetic benchmark requires an empty database")

    seed_started = time.perf_counter()
    await _seed_sessions(repository, population)
    seed_duration = time.perf_counter() - seed_started
    timed = _TimedRepository(repository)
    metrics = PrometheusMetrics()
    samples: list[ProcessResourceSnapshot] = []
    sampling_stop = asyncio.Event()
    sampling_task = asyncio.create_task(
        _sample_resources(
            samples,
            process_probe or PsutilProcessResourceProbe(),
            sampling_stop,
            sample_interval_seconds,
        ),
        name="phase4-monitoring-resource-sampler",
    )
    now = [_BASE_TIME]
    sweeps: list[SweepResult] = []
    try:
        for sweep in range(1, repeated_sweeps + 1):
            if sweep > 1:
                now[0] += timedelta(seconds=61)
            sweeps.append(
                await _run_sweep(
                    timed,
                    metrics,
                    clock=lambda: now[0],
                    sweep=sweep,
                    worker_count=worker_count,
                    queue_capacity=queue_capacity,
                    claim_batch_size=claim_batch_size,
                    lease_seconds=lease_seconds,
                    scheduler_tick_seconds=scheduler_tick_seconds,
                    check_delay_seconds=check_delay_seconds,
                )
            )
        now[0] += timedelta(seconds=61)
        adaptive = await _run_adaptive_scenario(
            timed,
            metrics,
            current_time=now,
            worker_count=worker_count,
            queue_capacity=queue_capacity,
            claim_batch_size=claim_batch_size,
            lease_seconds=lease_seconds,
            check_delay_seconds=check_delay_seconds,
        )
    finally:
        sampling_stop.set()
        await sampling_task
        await repository.close()

    report = Phase4MonitoringReport(
        generated_at=datetime.now(UTC),
        environment="local synthetic scheduler/database benchmark",
        staging_status="NOT RUN",
        persisted_queue_it_population_available=False,
        total_persisted_sessions=population,
        seed_duration_seconds=seed_duration,
        worker_count=worker_count,
        queue_capacity=queue_capacity,
        claim_batch_size=claim_batch_size,
        lease_seconds=lease_seconds,
        synthetic_check_delay_seconds=check_delay_seconds,
        scenario_a=tuple(sweeps),
        scenario_b=adaptive,
        resources=_summarize_resources(samples),
        resource_sample_count=len(samples),
        active_context_peak=0,
        context_acquisition_wait=None,
        restore_attempts=None,
        restore_successes=None,
        restore_failures=None,
        transfer_restore_successes=None,
        storage_state_restore_successes=None,
        identity_mismatches=None,
        browser_crashes=None,
        navigation_failures=None,
        sustainable_queue_it_cadence=(
            "UNKNOWN: no authorised browser-backed 10,000-session population was "
            "available. Synthetic full-sweep throughput must not be treated as "
            "Queue-it check throughput."
        ),
    )
    report.write_json(report_path)
    return report


async def _seed_sessions(
    repository: SQLiteSessionRepository,
    population: int,
) -> None:
    for index in range(population):
        session_id = f"phase4-synthetic-{index:05d}"
        await repository.create(
            QueueSession(
                session_id=session_id,
                queue_id=f"phase4-synthetic-queue-{index:05d}",
                transfer_url="https://synthetic.invalid/supported-transfer",
                mode=SessionMode.TRANSFER_ONLY,
                status=QueueStatus.PARKED,
                state_path=Path(f".synthetic-state/{session_id}.json"),
                created_at=_BASE_TIME - timedelta(seconds=120),
                next_check_at=_BASE_TIME - timedelta(seconds=120),
            )
        )


async def _run_sweep(
    repository: _TimedRepository,
    metrics: PrometheusMetrics,
    *,
    clock: Callable[[], datetime],
    sweep: int,
    worker_count: int,
    queue_capacity: int,
    claim_batch_size: int,
    lease_seconds: float,
    scheduler_tick_seconds: float,
    check_delay_seconds: float,
) -> SweepResult:
    repository.reset_timings()
    starting = await repository.due_session_summary(now=clock())
    handler = _SyntheticHandler(
        repository,
        clock=clock,
        target_checks=starting.count,
        next_delay_seconds=60,
        check_delay_seconds=check_delay_seconds,
    )
    scheduler = _make_scheduler(
        repository,
        handler,
        metrics,
        clock,
        worker_count,
        queue_capacity,
        claim_batch_size,
        lease_seconds,
        scheduler_tick_seconds,
        f"phase4-sweep-{sweep}",
    )
    stop_event = asyncio.Event()
    started = time.perf_counter()
    run_task = asyncio.create_task(scheduler.run(stop_event), name=f"phase4-sweep-{sweep}")
    try:
        await handler.complete.wait()
    finally:
        stop_event.set()
        await run_task
    duration = time.perf_counter() - started
    ending = await repository.due_session_summary(now=clock())
    overdue_start = starting.oldest_overdue_seconds(now=clock())
    overdue_end = ending.oldest_overdue_seconds(now=clock())
    timings = repository.timings
    rate = scheduler.metrics.checked / max(duration, 1e-9)
    return SweepResult(
        sweep=sweep,
        sessions_due=starting.count,
        sessions_checked=scheduler.metrics.checked,
        successful_checks=handler.successful,
        failed_checks=handler.failed,
        checks_per_second=rate,
        full_sweep_duration_seconds=duration,
        throughput_derived_10000_sweep_seconds=10_000 / max(rate, 1e-9),
        check_duration=_latency_summary(handler.latencies),
        scheduler_query_latency=_latency_summary(timings.due_summary),
        claim_latency=_latency_summary(timings.claims),
        update_latency=_latency_summary(timings.updates),
        lease_release_latency=_latency_summary(timings.releases),
        backlog_start=starting.count,
        backlog_peak=starting.count,
        backlog_end=ending.count,
        oldest_overdue_start_seconds=overdue_start,
        oldest_overdue_peak_seconds=max(overdue_start, scheduler.metrics.oldest_overdue_seconds),
        oldest_overdue_end_seconds=overdue_end,
        maximum_queue_depth=scheduler.metrics.maximum_queue_depth,
        maximum_active_workers=scheduler.metrics.maximum_concurrent_checks,
        lease_conflicts=scheduler.metrics.lease_conflicts,
    )


async def _run_adaptive_scenario(
    repository: _TimedRepository,
    metrics: PrometheusMetrics,
    *,
    current_time: list[datetime],
    worker_count: int,
    queue_capacity: int,
    claim_batch_size: int,
    lease_seconds: float,
    check_delay_seconds: float,
) -> AdaptiveResult:
    base = current_time[0]
    sessions = await repository.repository.list()
    intervals = adaptive_intervals(len(sessions))
    for session, interval in zip(sessions, intervals, strict=True):
        session.next_check_at = base + timedelta(seconds=interval)
        await repository.repository.update(session)

    repository.reset_timings()
    handler = _SyntheticHandler(
        repository,
        clock=lambda: current_time[0],
        target_checks=len(sessions),
        next_delay_seconds=600,
        check_delay_seconds=check_delay_seconds,
    )
    scheduler = _make_scheduler(
        repository,
        handler,
        metrics,
        lambda: current_time[0],
        worker_count,
        queue_capacity,
        claim_batch_size,
        lease_seconds,
        0.001,
        "phase4-adaptive",
    )
    buckets: dict[int, int] = {}
    for interval in intervals:
        bucket = math.floor(interval)
        buckets[bucket] = buckets.get(bucket, 0) + 1
    checkpoints: list[AdaptiveCheckpoint] = []
    maximum_backlog = 0
    maximum_overdue = 0.0
    await scheduler.start()
    started = time.perf_counter()
    sorted_intervals = sorted(intervals)
    virtual_offset = min(sorted_intervals)
    while handler.checked < len(sessions):
        current_time[0] = base + timedelta(seconds=virtual_offset)
        before = await repository.due_session_summary(now=current_time[0])
        if before.count == 0:
            next_index = bisect.bisect_right(sorted_intervals, virtual_offset)
            if next_index >= len(sorted_intervals):
                break
            virtual_offset = sorted_intervals[next_index]
            continue
        checked_before = handler.checked
        maximum_backlog = max(maximum_backlog, before.count)
        before_age = before.oldest_overdue_seconds(now=current_time[0])
        maximum_overdue = max(maximum_overdue, before_age)
        batch_started = time.perf_counter()
        await scheduler.schedule_due()
        await scheduler.wait_until_idle()
        virtual_offset += time.perf_counter() - batch_started
        current_time[0] = base + timedelta(seconds=virtual_offset)
        remaining = await repository.due_session_summary(now=current_time[0])
        checkpoints.append(
            AdaptiveCheckpoint(
                offset_seconds=math.floor(virtual_offset),
                due_before_processing=before.count,
                checked=handler.checked - checked_before,
                backlog_after_processing=remaining.count,
                oldest_overdue_before_seconds=before_age,
                oldest_overdue_after_seconds=remaining.oldest_overdue_seconds(
                    now=current_time[0]
                ),
            )
        )
    await scheduler.shutdown()
    duration = time.perf_counter() - started
    ending = await repository.due_session_summary(now=current_time[0])
    timings = repository.timings
    return AdaptiveResult(
        sessions_scheduled=len(sessions),
        sessions_checked=handler.checked,
        successful_checks=handler.successful,
        failed_checks=handler.failed,
        processing_duration_seconds=duration,
        checks_per_processing_second=handler.checked / max(duration, 1e-9),
        schedule_window_seconds=max(intervals) - min(intervals),
        completion_offset_seconds=virtual_offset - min(intervals),
        jitter_min_seconds=min(intervals),
        jitter_max_seconds=max(intervals),
        distinct_next_check_times=len(set(intervals)),
        maximum_one_second_bucket=max(buckets.values()),
        maximum_due_backlog=maximum_backlog,
        maximum_oldest_overdue_seconds=maximum_overdue,
        backlog_end=ending.count,
        oldest_overdue_end_seconds=ending.oldest_overdue_seconds(now=current_time[0]),
        maximum_queue_depth=scheduler.metrics.maximum_queue_depth,
        maximum_active_workers=scheduler.metrics.maximum_concurrent_checks,
        lease_conflicts=scheduler.metrics.lease_conflicts,
        check_duration=_latency_summary(handler.latencies),
        scheduler_query_latency=_latency_summary(timings.due_summary),
        claim_latency=_latency_summary(timings.claims),
        checkpoints=tuple(checkpoints),
    )


def adaptive_intervals(population: int, *, seed: int = 20260927) -> list[float]:
    """Generate deterministic due times through the production polling policy."""

    if population < 1:
        raise ValueError("population must be at least 1")
    policy = PollingPolicy(default_seconds=30, jitter_seconds=5)
    generator = random.Random(seed)
    intervals: list[float] = []
    for index in range(population):
        bucket = index % 100
        if bucket < 25:
            status = QueueStatus.PRE_QUEUE
            progress = None
        elif bucket < 65:
            status = QueueStatus.ACTIVE_QUEUE
            progress = QueueProgress(
                session_id=f"adaptive-{index}", progress_percentage=10
            )
        elif bucket < 90:
            status = QueueStatus.ACTIVE_QUEUE
            progress = QueueProgress(
                session_id=f"adaptive-{index}", progress_percentage=70
            )
        elif bucket < 98:
            status = QueueStatus.SERVICED_SOON
            progress = None
        else:
            status = QueueStatus.TURN_STARTED
            progress = None
        intervals.append(
            policy.interval_seconds(status, progress, jitter=generator.uniform)
        )
    return intervals


def _make_scheduler(
    repository: _TimedRepository,
    handler: _SyntheticHandler,
    metrics: PrometheusMetrics,
    clock: Callable[[], datetime],
    worker_count: int,
    queue_capacity: int,
    claim_batch_size: int,
    lease_seconds: float,
    scheduler_tick_seconds: float,
    scheduler_id: str,
) -> ParkedSessionScheduler:
    return ParkedSessionScheduler(
        repository=repository,  # type: ignore[arg-type]
        handler=handler,
        worker_count=worker_count,
        queue_capacity=queue_capacity,
        claim_batch_size=claim_batch_size,
        lease_seconds=lease_seconds,
        failure_delay_seconds=30,
        scheduler_tick_seconds=scheduler_tick_seconds,
        shutdown_timeout_seconds=60,
        clock=clock,
        scheduler_id=scheduler_id,
        observability=metrics,
    )


async def _sample_resources(
    samples: list[ProcessResourceSnapshot],
    probe: ProcessResourceProbe,
    stop_event: asyncio.Event,
    interval_seconds: float,
) -> None:
    while True:
        try:
            samples.append(probe.sample())
        except Exception:  # noqa: BLE001 - optional sampling must not abort benchmark
            samples.append(ProcessResourceSnapshot())
        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def _summarize_resources(samples: Sequence[ProcessResourceSnapshot]) -> ResourceSummary:
    def summary(name: str) -> AveragePeak:
        values = [
            float(value)
            for sample in samples
            if (value := getattr(sample, name)) is not None
        ]
        return AveragePeak(
            average=statistics.fmean(values) if values else None,
            peak=max(values) if values else None,
        )

    return ResourceSummary(
        application_cpu_percent=summary("application_cpu_percent"),
        application_ram_bytes=summary("application_ram_bytes"),
        chrome_cpu_percent=summary("chrome_cpu_percent"),
        chrome_ram_bytes=summary("chrome_ram_bytes"),
    )


def _latency_summary(values: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values),
        average_seconds=statistics.fmean(values) if values else None,
        p50_seconds=percentile(values, 50),
        p95_seconds=percentile(values, 95),
        p99_seconds=percentile(values, 99),
        maximum_seconds=max(values) if values else None,
    )


def _validate_configuration(
    *,
    population: int,
    worker_count: int,
    queue_capacity: int,
    claim_batch_size: int,
    lease_seconds: float,
    scheduler_tick_seconds: float,
    check_delay_seconds: float,
    repeated_sweeps: int,
    sample_interval_seconds: float,
) -> None:
    if population < 1:
        raise ValueError("population must be at least 1")
    if not 1 <= worker_count <= 50:
        raise ValueError("worker_count must be between 1 and 50")
    if not 1 <= queue_capacity <= 50:
        raise ValueError("queue_capacity must be between 1 and 50")
    if not 1 <= claim_batch_size <= queue_capacity:
        raise ValueError("claim_batch_size must be between 1 and queue_capacity")
    if lease_seconds <= 0 or scheduler_tick_seconds <= 0:
        raise ValueError("lease and scheduler tick must be positive")
    if check_delay_seconds < 0 or sample_interval_seconds <= 0:
        raise ValueError("check delay cannot be negative and sample interval must be positive")
    if repeated_sweeps < 2:
        raise ValueError("repeated_sweeps must be at least 2")


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}s"


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local synthetic Phase 4 10,000-session monitoring benchmark"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("phase4-monitoring-synthetic.sqlite3"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("phase4-monitoring-benchmark.json"),
    )
    parser.add_argument("--population", type=int, default=PHASE4_POPULATION)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--queue-capacity", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--sweeps", type=int, default=2)
    parser.add_argument("--check-delay-seconds", type=float, default=0.001)
    parser.add_argument("--sample-interval-seconds", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_phase4_monitoring_benchmark(
            args.database,
            report_path=args.report,
            population=args.population,
            worker_count=args.workers,
            queue_capacity=args.queue_capacity,
            claim_batch_size=args.batch_size,
            repeated_sweeps=args.sweeps,
            check_delay_seconds=args.check_delay_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
