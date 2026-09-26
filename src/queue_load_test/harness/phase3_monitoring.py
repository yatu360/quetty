"""Synthetic Phase 3 monitoring-sweep benchmark for 1,000 parked sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from queue_load_test.harness.resource_benchmark import (
    ProcessResourceProbe,
    ProcessResourceSnapshot,
    PsutilProcessResourceProbe,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringOutcome, ParkedSessionScheduler, PollingPolicy

_POPULATION = 1000
_BASE_TIME = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MonitoringBenchmarkConfiguration:
    persisted_sessions: int
    worker_count: int
    queue_capacity: int
    claim_batch_size: int
    lease_seconds: float
    scheduler_tick_seconds: float
    synthetic_check_delay_seconds: float
    repeated_sweeps: int


@dataclass(frozen=True, slots=True)
class LatencySummary:
    count: int
    average_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    maximum_seconds: float | None


@dataclass(frozen=True, slots=True)
class AveragePeak:
    average: float | None
    peak: float | None


@dataclass(frozen=True, slots=True)
class MonitoringResourceSample:
    timestamp: datetime
    application_cpu_percent: float | None
    application_ram_bytes: int | None
    chrome_cpu_percent: float | None
    chrome_ram_bytes: int | None
    observed_chrome_processes: int | None
    active_contexts: int
    scheduler_backlog: float | None
    queue_depth: float | None
    active_workers: float | None


@dataclass(frozen=True, slots=True)
class MonitoringResourceSummary:
    application_cpu_percent: AveragePeak
    application_ram_bytes: AveragePeak
    chrome_cpu_percent: AveragePeak
    chrome_ram_bytes: AveragePeak
    observed_chrome_processes: AveragePeak
    scheduler_backlog: AveragePeak
    queue_depth: AveragePeak
    active_workers: AveragePeak


@dataclass(frozen=True, slots=True)
class SweepResult:
    sweep: int
    sessions_due: int
    sessions_checked: int
    successful_checks: int
    failed_checks: int
    checks_per_second: float
    full_sweep_duration_seconds: float
    check_duration: LatencySummary
    backlog_start: int
    backlog_peak: int
    backlog_end: int
    maximum_queue_depth: int
    maximum_active_workers: int
    lease_conflicts: int


@dataclass(frozen=True, slots=True)
class StaggeredCheckpoint:
    offset_seconds: int
    due_before_processing: int
    checked: int
    backlog_after_processing: int


@dataclass(frozen=True, slots=True)
class StaggeredResult:
    sessions_checked: int
    failed_checks: int
    wall_duration_seconds: float
    jitter_min_seconds: float
    jitter_max_seconds: float
    distinct_next_check_times: int
    maximum_one_second_bucket: int
    maximum_queue_depth: int
    backlog_end: int
    checkpoints: tuple[StaggeredCheckpoint, ...]


@dataclass(frozen=True, slots=True)
class Phase3MonitoringReport:
    generated_at: datetime
    staging_status: str
    configuration: MonitoringBenchmarkConfiguration
    seed_duration_seconds: float
    sweeps: tuple[SweepResult, ...]
    staggered: StaggeredResult
    resources: MonitoringResourceSummary
    samples: tuple[MonitoringResourceSample, ...]
    context_acquisition_wait_seconds: LatencySummary | None
    active_context_peak: int
    restore_failures: int | None
    identity_mismatches: int | None
    browser_crashes: int | None
    navigation_failures: int | None
    full_sweep_definition: str = (
        "Wall time from starting scheduling with all 1,000 benchmark sessions due "
        "until every session has completed its synthetic check, persistence update, "
        "and lease release. Initial database seeding is excluded."
    )
    scope_warning: str = (
        "Synthetic results exercise SQLite, leasing, bounded scheduling, workers, "
        "persistence, and backpressure only. They are not Queue-it/browser throughput."
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
            "Phase 3 synthetic monitoring-sweep benchmark",
            "",
            self.scope_warning,
            "",
            f"Staging status: {self.staging_status}",
            f"Persisted sessions: {self.configuration.persisted_sessions}",
            f"Full sweep definition: {self.full_sweep_definition}",
        ]
        for result in self.sweeps:
            lines.append(
                f"Sweep {result.sweep}: {result.sessions_checked}/"
                f"{result.sessions_due} checked in "
                f"{result.full_sweep_duration_seconds:.4f}s "
                f"({result.checks_per_second:.2f}/s), p95 "
                f"{_seconds(result.check_duration.p95_seconds)}, backlog "
                f"{result.backlog_start}->{result.backlog_end}"
            )
        lines.extend(
            (
                (
                    "Staggered: "
                    f"{self.staggered.sessions_checked} checked, "
                    f"{self.staggered.distinct_next_check_times} distinct due times, "
                    f"largest 1s bucket {self.staggered.maximum_one_second_bucket}, "
                    f"backlog end {self.staggered.backlog_end}"
                ),
                "Active context peak: 0 (synthetic handler; browser result UNKNOWN)",
                "Restore/navigation/identity/browser results: UNKNOWN (staging NOT RUN)",
            )
        )
        return "\n".join(lines) + "\n"


class _SyntheticHandler:
    def __init__(
        self,
        repository: SQLiteSessionRepository,
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

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        started = time.perf_counter()
        success = False
        try:
            if self._check_delay_seconds:
                await asyncio.sleep(self._check_delay_seconds)
            session.next_check_at = self._clock() + timedelta(
                seconds=self._next_delay_seconds
            )
            session.last_checked_at = self._clock()
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


async def run_synthetic_monitoring_benchmark(
    database: Path,
    *,
    report_path: Path,
    worker_count: int = 20,
    queue_capacity: int = 50,
    claim_batch_size: int = 50,
    lease_seconds: float = 120.0,
    scheduler_tick_seconds: float = 0.001,
    check_delay_seconds: float = 0.001,
    repeated_sweeps: int = 2,
    sample_interval_seconds: float = 0.01,
    process_probe: ProcessResourceProbe | None = None,
) -> Phase3MonitoringReport:
    """Run repeated all-due sweeps and one staggered pass without browser traffic."""

    _validate_configuration(
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
        raise ValueError("Synthetic monitoring benchmark requires an empty database")

    seed_started = time.perf_counter()
    await _seed_sessions(repository)
    seed_duration = time.perf_counter() - seed_started
    metrics = PrometheusMetrics()
    probe = process_probe or PsutilProcessResourceProbe()
    samples: list[MonitoringResourceSample] = []
    sampling_stop = asyncio.Event()
    sampling_task = asyncio.create_task(
        _sample_resources(
            samples,
            metrics,
            probe,
            sampling_stop,
            sample_interval_seconds,
        ),
        name="phase3-monitoring-resource-sampler",
    )
    sweeps: list[SweepResult] = []
    current_time = [_BASE_TIME]
    try:
        for sweep in range(1, repeated_sweeps + 1):
            if sweep > 1:
                current_time[0] += timedelta(seconds=60)
            sweeps.append(
                await _run_all_due_sweep(
                    repository,
                    metrics,
                    clock=lambda: current_time[0],
                    sweep=sweep,
                    worker_count=worker_count,
                    queue_capacity=queue_capacity,
                    claim_batch_size=claim_batch_size,
                    lease_seconds=lease_seconds,
                    scheduler_tick_seconds=scheduler_tick_seconds,
                    check_delay_seconds=check_delay_seconds,
                )
            )
        current_time[0] += timedelta(seconds=60)
        staggered = await _run_staggered_pass(
            repository,
            metrics,
            current_time=current_time,
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

    report = Phase3MonitoringReport(
        generated_at=datetime.now(UTC),
        staging_status="NOT RUN",
        configuration=MonitoringBenchmarkConfiguration(
            persisted_sessions=_POPULATION,
            worker_count=worker_count,
            queue_capacity=queue_capacity,
            claim_batch_size=claim_batch_size,
            lease_seconds=lease_seconds,
            scheduler_tick_seconds=scheduler_tick_seconds,
            synthetic_check_delay_seconds=check_delay_seconds,
            repeated_sweeps=repeated_sweeps,
        ),
        seed_duration_seconds=seed_duration,
        sweeps=tuple(sweeps),
        staggered=staggered,
        resources=_summarize_resources(samples),
        samples=tuple(samples),
        context_acquisition_wait_seconds=None,
        active_context_peak=0,
        restore_failures=None,
        identity_mismatches=None,
        browser_crashes=None,
        navigation_failures=None,
    )
    report.write_json(report_path)
    return report


async def _seed_sessions(repository: SQLiteSessionRepository) -> None:
    for index in range(_POPULATION):
        session_id = f"synthetic-{index:04d}"
        await repository.create(
            QueueSession(
                session_id=session_id,
                queue_id=f"synthetic-queue-{index:04d}",
                transfer_url=f"https://synthetic.invalid/journey?q={index:04d}",
                mode=SessionMode.TRANSFER_ONLY,
                status=QueueStatus.PARKED,
                state_path=Path(f".synthetic-state/{session_id}.json"),
                created_at=_BASE_TIME,
                next_check_at=_BASE_TIME,
            )
        )


async def _run_all_due_sweep(
    repository: SQLiteSessionRepository,
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
    due_start = await repository.count_due_sessions(now=clock())
    handler = _SyntheticHandler(
        repository,
        clock=clock,
        target_checks=due_start,
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
        f"synthetic-sweep-{sweep}",
    )
    stop_event = asyncio.Event()
    started = time.perf_counter()
    run_task = asyncio.create_task(
        scheduler.run(stop_event),
        name=f"phase3-monitoring-sweep-{sweep}",
    )
    try:
        await handler.complete.wait()
    finally:
        stop_event.set()
        await run_task
    duration = time.perf_counter() - started
    backlog_end = await repository.count_due_sessions(now=clock())
    return SweepResult(
        sweep=sweep,
        sessions_due=due_start,
        sessions_checked=scheduler.metrics.checked,
        successful_checks=handler.successful,
        failed_checks=handler.failed,
        checks_per_second=scheduler.metrics.checked / max(duration, 1e-9),
        full_sweep_duration_seconds=duration,
        check_duration=_latency_summary(handler.latencies),
        backlog_start=due_start,
        backlog_peak=due_start,
        backlog_end=backlog_end,
        maximum_queue_depth=scheduler.metrics.maximum_queue_depth,
        maximum_active_workers=scheduler.metrics.maximum_concurrent_checks,
        lease_conflicts=scheduler.metrics.lease_conflicts,
    )


async def _run_staggered_pass(
    repository: SQLiteSessionRepository,
    metrics: PrometheusMetrics,
    *,
    current_time: list[datetime],
    worker_count: int,
    queue_capacity: int,
    claim_batch_size: int,
    lease_seconds: float,
    check_delay_seconds: float,
) -> StaggeredResult:
    base = current_time[0]
    policy = PollingPolicy(default_seconds=30, jitter_seconds=5)
    generator = random.Random(20260926)
    intervals: list[float] = []
    sessions = await repository.list()
    for session in sessions:
        interval = policy.interval_seconds(
            QueueStatus.PARKED,
            None,
            jitter=generator.uniform,
        )
        intervals.append(interval)
        session.next_check_at = base + timedelta(seconds=interval)
        await repository.update(session)

    handler = _SyntheticHandler(
        repository,
        clock=lambda: current_time[0],
        target_checks=len(sessions),
        next_delay_seconds=60,
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
        "synthetic-staggered",
    )
    buckets: dict[int, int] = {}
    for interval in intervals:
        bucket = math.floor(interval)
        buckets[bucket] = buckets.get(bucket, 0) + 1
    checkpoints: list[StaggeredCheckpoint] = []
    await scheduler.start()
    started = time.perf_counter()
    for offset in range(math.floor(min(intervals)), math.ceil(max(intervals)) + 1):
        current_time[0] = base + timedelta(seconds=offset)
        due_before = await repository.count_due_sessions(now=current_time[0])
        checked_before = handler.checked
        remaining = due_before
        while remaining:
            await scheduler.schedule_due()
            await scheduler.wait_until_idle()
            remaining = await repository.count_due_sessions(now=current_time[0])
        checkpoints.append(
            StaggeredCheckpoint(
                offset_seconds=offset,
                due_before_processing=due_before,
                checked=handler.checked - checked_before,
                backlog_after_processing=remaining,
            )
        )
    await scheduler.shutdown()
    duration = time.perf_counter() - started
    backlog_end = await repository.count_due_sessions(now=current_time[0])
    return StaggeredResult(
        sessions_checked=handler.checked,
        failed_checks=handler.failed,
        wall_duration_seconds=duration,
        jitter_min_seconds=min(intervals),
        jitter_max_seconds=max(intervals),
        distinct_next_check_times=len(set(intervals)),
        maximum_one_second_bucket=max(buckets.values()),
        maximum_queue_depth=scheduler.metrics.maximum_queue_depth,
        backlog_end=backlog_end,
        checkpoints=tuple(checkpoints),
    )


def _make_scheduler(
    repository: SQLiteSessionRepository,
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
        repository=repository,
        handler=handler,
        worker_count=worker_count,
        queue_capacity=queue_capacity,
        claim_batch_size=claim_batch_size,
        lease_seconds=lease_seconds,
        failure_delay_seconds=30,
        scheduler_tick_seconds=scheduler_tick_seconds,
        shutdown_timeout_seconds=30,
        clock=clock,
        scheduler_id=scheduler_id,
        observability=metrics,
    )


async def _sample_resources(
    samples: list[MonitoringResourceSample],
    metrics: PrometheusMetrics,
    probe: ProcessResourceProbe,
    stop_event: asyncio.Event,
    interval_seconds: float,
) -> None:
    while True:
        try:
            resource = probe.sample()
        except Exception:  # noqa: BLE001 - optional sampling cannot abort the benchmark
            resource = ProcessResourceSnapshot()
        samples.append(
            MonitoringResourceSample(
                timestamp=datetime.now(UTC),
                application_cpu_percent=resource.application_cpu_percent,
                application_ram_bytes=resource.application_ram_bytes,
                chrome_cpu_percent=resource.chrome_cpu_percent,
                chrome_ram_bytes=resource.chrome_ram_bytes,
                observed_chrome_processes=resource.observed_chrome_processes,
                active_contexts=0,
                scheduler_backlog=_metric(metrics, "monitoring_due_backlog"),
                queue_depth=_metric(metrics, "monitoring_queue_depth"),
                active_workers=_metric(metrics, "monitoring_workers_active"),
            )
        )
        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def _summarize_resources(
    samples: Sequence[MonitoringResourceSample],
) -> MonitoringResourceSummary:
    def values(name: str) -> list[float]:
        return [
            float(value)
            for sample in samples
            if (value := getattr(sample, name)) is not None
        ]

    return MonitoringResourceSummary(
        application_cpu_percent=_average_peak(values("application_cpu_percent")),
        application_ram_bytes=_average_peak(values("application_ram_bytes")),
        chrome_cpu_percent=_average_peak(values("chrome_cpu_percent")),
        chrome_ram_bytes=_average_peak(values("chrome_ram_bytes")),
        observed_chrome_processes=_average_peak(values("observed_chrome_processes")),
        scheduler_backlog=_average_peak(values("scheduler_backlog")),
        queue_depth=_average_peak(values("queue_depth")),
        active_workers=_average_peak(values("active_workers")),
    )


def _latency_summary(values: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values),
        average_seconds=statistics.fmean(values) if values else None,
        p50_seconds=percentile(values, 50),
        p95_seconds=percentile(values, 95),
        maximum_seconds=max(values) if values else None,
    )


def _average_peak(values: Sequence[float]) -> AveragePeak:
    return AveragePeak(
        average=statistics.fmean(values) if values else None,
        peak=max(values) if values else None,
    )


def _metric(metrics: PrometheusMetrics, name: str) -> float | None:
    value = metrics.registry.get_sample_value(name)
    return float(value) if value is not None else None


def _validate_configuration(
    *,
    worker_count: int,
    queue_capacity: int,
    claim_batch_size: int,
    lease_seconds: float,
    scheduler_tick_seconds: float,
    check_delay_seconds: float,
    repeated_sweeps: int,
    sample_interval_seconds: float,
) -> None:
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
        description="Run the local synthetic Phase 3 monitoring-sweep benchmark"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("phase3-monitoring-synthetic.sqlite3"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("phase3-monitoring-benchmark.json"),
    )
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--queue-capacity", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--sweeps", type=int, default=2)
    parser.add_argument("--check-delay-seconds", type=float, default=0.001)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.01)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        run_synthetic_monitoring_benchmark(
            args.database,
            report_path=args.report,
            worker_count=args.workers,
            queue_capacity=args.queue_capacity,
            claim_batch_size=args.batch_size,
            check_delay_seconds=args.check_delay_seconds,
            repeated_sweeps=args.sweeps,
            sample_interval_seconds=args.sample_interval_seconds,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
