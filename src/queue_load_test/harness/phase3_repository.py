"""Local synthetic SQLite and scheduler benchmark for Phase 3."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringOutcome, ParkedSessionScheduler

_MONITORABLE_STATUSES = (
    QueueStatus.PARKED,
    QueueStatus.PRE_QUEUE,
    QueueStatus.ACTIVE_QUEUE,
    QueueStatus.PAUSED,
    QueueStatus.SERVICED_SOON,
    QueueStatus.TURN_STARTED,
    QueueStatus.READY,
    QueueStatus.CONNECTION_LOST,
)
_EXCLUDED_STATUSES = (
    QueueStatus.ADMITTED,
    QueueStatus.EXPIRED,
    QueueStatus.FAILED,
    QueueStatus.NEW,
    QueueStatus.CREATING,
)


@dataclass(frozen=True, slots=True)
class OperationLatency:
    samples: int
    average_ms: float
    p50_ms: float
    p95_ms: float
    maximum_ms: float


@dataclass(frozen=True, slots=True)
class Phase3RepositoryBenchmarkReport:
    generated_at: datetime
    sqlite_version: str
    session_count: int
    expected_due_sessions: int
    observed_due_sessions: int
    claim_batch_size: int
    seed_duration_ms: float
    database_size_bytes: int
    query_plan: tuple[str, ...]
    due_query: OperationLatency
    claim: OperationLatency
    update: OperationLatency
    lease_release: OperationLatency
    scheduler_iteration: OperationLatency

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )

    def render_text(self) -> str:
        lines = [
            "Phase 3 synthetic repository benchmark",
            f"Sessions: {self.session_count}",
            f"Eligible due sessions: {self.observed_due_sessions}",
            f"Claim batch size: {self.claim_batch_size}",
            f"Seed duration: {self.seed_duration_ms:.3f} ms",
            f"Database size: {self.database_size_bytes} bytes",
            "Query plan: " + " | ".join(self.query_plan),
        ]
        for name, latency in (
            ("Due query", self.due_query),
            ("Claim", self.claim),
            ("Update", self.update),
            ("Lease release", self.lease_release),
            ("Scheduler iteration", self.scheduler_iteration),
        ):
            lines.append(
                f"{name}: p50={latency.p50_ms:.3f} ms, "
                f"p95={latency.p95_ms:.3f} ms, max={latency.maximum_ms:.3f} ms "
                f"({latency.samples} samples)"
            )
        return "\n".join(lines) + "\n"


class _UnusedMonitoringHandler:
    async def check(self, session: QueueSession) -> MonitoringOutcome:
        raise AssertionError(f"benchmark scheduler unexpectedly processed {session.session_id}")


async def seed_synthetic_sessions(
    repository: SQLiteSessionRepository,
    *,
    now: datetime,
    session_count: int = 1000,
) -> int:
    """Seed a deterministic mix and return the expected eligible due count."""

    if session_count < 10 or session_count % 10:
        raise ValueError("session_count must be at least 10 and divisible by 10")

    due_count = session_count // 2
    future_count = session_count // 5
    active_lease_count = session_count // 10
    expired_lease_count = session_count // 10
    excluded_count = session_count - (
        due_count + future_count + active_lease_count + expired_lease_count
    )
    expected_due = due_count + expired_lease_count
    boundaries = (
        due_count,
        due_count + future_count,
        due_count + future_count + active_lease_count,
        due_count + future_count + active_lease_count + expired_lease_count,
    )

    for index in range(session_count):
        status = _MONITORABLE_STATUSES[index % len(_MONITORABLE_STATUSES)]
        next_check_at = now - timedelta(seconds=index + 1)
        worker_id: str | None = None
        lease_until: datetime | None = None
        if index < boundaries[0]:
            pass
        elif index < boundaries[1]:
            next_check_at = now + timedelta(minutes=5, seconds=index)
        elif index < boundaries[2]:
            worker_id = "active-lease"
            lease_until = now + timedelta(minutes=2)
        elif index < boundaries[3]:
            worker_id = "expired-lease"
            lease_until = now - timedelta(seconds=1)
        else:
            status = _EXCLUDED_STATUSES[(index - boundaries[3]) % len(_EXCLUDED_STATUSES)]

        session_id = f"synthetic-{index:04d}"
        await repository.create(
            QueueSession(
                session_id=session_id,
                queue_id=f"synthetic-queue-{index:04d}",
                transfer_url=f"https://queue.test/journey?q=synthetic-queue-{index:04d}",
                mode=SessionMode.HYBRID,
                status=status,
                state_path=Path(f".browser-state/{session_id}.json"),
                created_at=now - timedelta(minutes=10, seconds=index),
                next_check_at=next_check_at,
                worker_id=worker_id,
                lease_until=lease_until,
            )
        )

    if excluded_count != session_count // 10:
        raise AssertionError("synthetic distribution must reserve ten percent for exclusions")
    return expected_due


async def run_phase3_repository_benchmark(
    database: Path,
    *,
    session_count: int = 1000,
    claim_batch_size: int = 50,
    samples: int = 20,
    now: datetime | None = None,
) -> Phase3RepositoryBenchmarkReport:
    """Measure bounded SQLite operations without browser or staging traffic."""

    if claim_batch_size < 1 or claim_batch_size > session_count:
        raise ValueError("claim_batch_size must be between 1 and session_count")
    if samples < 1:
        raise ValueError("samples must be at least 1")
    benchmark_now = now or datetime.now(UTC)
    repository = SQLiteSessionRepository(database)
    try:
        await repository.initialize()
        if await repository.list():
            raise RuntimeError("Use an empty SQLite database for the Phase 3 benchmark")

        started = time.perf_counter()
        expected_due = await seed_synthetic_sessions(
            repository,
            now=benchmark_now,
            session_count=session_count,
        )
        seed_duration_ms = (time.perf_counter() - started) * 1000

        due_latencies: list[float] = []
        observed_due = 0
        for _ in range(samples):
            observed_due, elapsed = await _measure(
                lambda: repository.count_due_sessions(now=benchmark_now)
            )
            due_latencies.append(elapsed)

        claim_latencies: list[float] = []
        for sample in range(samples):
            claimed, elapsed = await _measure(
                partial(
                    repository.claim_due_sessions,
                    worker_id=f"claim-sample-{sample}",
                    now=benchmark_now,
                    lease_until=benchmark_now + timedelta(minutes=2),
                    limit=claim_batch_size,
                )
            )
            if len(claimed) != claim_batch_size:
                raise AssertionError("synthetic claim did not return the configured batch")
            claim_latencies.append(elapsed)
            for session in claimed:
                await repository.release_lease(
                    session.session_id,
                    worker_id=f"claim-sample-{sample}",
                )

        update_target = await repository.get(f"synthetic-{session_count // 2:04d}")
        if update_target is None:
            raise AssertionError("synthetic update target was not persisted")
        update_latencies: list[float] = []
        for sample in range(samples):
            update_target.last_checked_at = benchmark_now + timedelta(microseconds=sample)
            _, elapsed = await _measure(lambda: repository.update(update_target))
            update_latencies.append(elapsed)

        release_latencies: list[float] = []
        for sample in range(samples):
            claimed = await repository.claim_due_sessions(
                worker_id=f"release-sample-{sample}",
                now=benchmark_now,
                lease_until=benchmark_now + timedelta(minutes=2),
                limit=1,
            )
            if len(claimed) != 1:
                raise AssertionError("synthetic release sample could not claim a session")
            released, elapsed = await _measure(
                partial(
                    repository.release_lease,
                    claimed[0].session_id,
                    worker_id=f"release-sample-{sample}",
                )
            )
            if not released:
                raise AssertionError("synthetic lease release lost ownership")
            release_latencies.append(elapsed)

        scheduler_latencies: list[float] = []
        for sample in range(samples):
            scheduler = ParkedSessionScheduler(
                repository=repository,
                handler=_UnusedMonitoringHandler(),
                worker_count=5,
                queue_capacity=claim_batch_size,
                claim_batch_size=claim_batch_size,
                lease_seconds=120,
                failure_delay_seconds=30,
                clock=lambda: benchmark_now,
                scheduler_id=f"scheduler-sample-{sample}",
            )
            claimed_count, elapsed = await _measure(scheduler.schedule_due)
            if claimed_count != claim_batch_size:
                raise AssertionError("scheduler iteration did not retain its bounded batch")
            scheduler_latencies.append(elapsed)
            await scheduler.shutdown()

        query_plan = await repository.explain_due_session_query(
            now=benchmark_now,
            limit=claim_batch_size,
        )
        return Phase3RepositoryBenchmarkReport(
            generated_at=datetime.now(UTC),
            sqlite_version=sqlite3.sqlite_version,
            session_count=session_count,
            expected_due_sessions=expected_due,
            observed_due_sessions=observed_due,
            claim_batch_size=claim_batch_size,
            seed_duration_ms=seed_duration_ms,
            database_size_bytes=database.stat().st_size,
            query_plan=query_plan,
            due_query=_latency_summary(due_latencies),
            claim=_latency_summary(claim_latencies),
            update=_latency_summary(update_latencies),
            lease_release=_latency_summary(release_latencies),
            scheduler_iteration=_latency_summary(scheduler_latencies),
        )
    finally:
        await repository.close()


async def _measure[T](operation: Callable[[], Awaitable[T]]) -> tuple[T, float]:
    started = time.perf_counter()
    result = await operation()
    return result, (time.perf_counter() - started) * 1000


def _latency_summary(values: Sequence[float]) -> OperationLatency:
    return OperationLatency(
        samples=len(values),
        average_ms=statistics.fmean(values),
        p50_ms=percentile(values, 50) or 0.0,
        p95_ms=percentile(values, 95) or 0.0,
        maximum_ms=max(values),
    )


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark SQLite scheduling operations with synthetic Phase 3 sessions"
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--sessions", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--samples", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.database is not None:
        report = asyncio.run(
            run_phase3_repository_benchmark(
                args.database,
                session_count=args.sessions,
                claim_batch_size=args.batch_size,
                samples=args.samples,
            )
        )
    else:
        with tempfile.TemporaryDirectory(prefix="queue-load-test-phase3-") as directory:
            report = asyncio.run(
                run_phase3_repository_benchmark(
                    Path(directory) / "sessions.sqlite3",
                    session_count=args.sessions,
                    claim_batch_size=args.batch_size,
                    samples=args.samples,
                )
            )
    if args.report is not None:
        report.write_json(args.report)
    print(report.render_text())


if __name__ == "__main__":
    main()
