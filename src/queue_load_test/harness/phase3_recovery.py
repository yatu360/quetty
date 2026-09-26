"""Synthetic recovery and repeated-restart benchmark for Phase 3."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import RecoverySummary, SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringOutcome, ParkedSessionScheduler
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker


@dataclass(frozen=True, slots=True)
class Phase3RecoveryBenchmarkReport:
    generated_at: datetime
    session_count: int
    restart_repetitions: int
    restart_duration_average_ms: float
    restart_duration_p50_ms: float
    restart_duration_p95_ms: float
    restart_duration_maximum_ms: float
    initial_summary: RecoverySummary
    final_summary: RecoverySummary
    identity_preserved: bool
    terminal_states_preserved: bool
    state_associations_consistent: bool
    expired_leases_recovered: int
    resumed_checks: int
    scheduler_worker_tasks: int
    scheduler_queue_capacity: int
    scheduler_queue_peak: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )

    def render_text(self) -> str:
        return (
            "Phase 3 synthetic recovery benchmark\n"
            f"Sessions / valid Queue IDs: {self.session_count} / "
            f"{self.initial_summary.valid_queue_ids}\n"
            f"Startup restart latency: p50={self.restart_duration_p50_ms:.3f} ms, "
            f"p95={self.restart_duration_p95_ms:.3f} ms, "
            f"max={self.restart_duration_maximum_ms:.3f} ms\n"
            f"Expired leases recovered / resumed checks: "
            f"{self.expired_leases_recovered} / {self.resumed_checks}\n"
            f"Scheduler workers / queue peak/capacity: {self.scheduler_worker_tasks} / "
            f"{self.scheduler_queue_peak}/{self.scheduler_queue_capacity}\n"
            f"Identity preserved: {self.identity_preserved}\n"
            f"Terminal states preserved: {self.terminal_states_preserved}\n"
            f"State associations consistent: {self.state_associations_consistent}\n"
        )


class _RecoveryHandler:
    def __init__(self, repository: SQLiteSessionRepository, now: datetime) -> None:
        self._repository = repository
        self._now = now
        self.checked_ids: list[str] = []

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        self.checked_ids.append(session.session_id)
        session.last_checked_at = self._now
        session.last_error = None
        session.next_check_at = self._now + timedelta(minutes=5)
        await self._repository.update(session)
        return MonitoringOutcome(
            session_id=session.session_id,
            success=True,
            observed_status=session.status,
            next_check_at=session.next_check_at,
            queue_update_stale=False,
            progress_changed=False,
        )


async def seed_recovery_population(
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    *,
    now: datetime,
    session_count: int = 1000,
) -> None:
    """Seed a fixed mixed recovery population with known aggregate counts."""

    if session_count != 1000:
        raise ValueError("the Phase 3 recovery population must contain exactly 1000 sessions")
    for index in range(session_count):
        session_id = f"recovery-{index:04d}"
        status = QueueStatus.PARKED
        queue_id: str | None = f"recovery-queue-{index:04d}"
        next_check_at: datetime | None = now - timedelta(minutes=1)
        worker_id: str | None = None
        lease_until: datetime | None = None
        last_error: str | None = None

        if index < 50:
            worker_id = "interrupted-active-worker"
            lease_until = now + timedelta(minutes=1)
        elif index < 100:
            worker_id = "interrupted-expired-worker"
            lease_until = now - timedelta(seconds=1)
            next_check_at = now - timedelta(minutes=2)
        elif index < 800:
            pass
        elif index < 850:
            next_check_at = now + timedelta(minutes=5)
        elif index < 900:
            status = QueueStatus.CONNECTION_LOST
            last_error = "restore:NAVIGATION_FAILED"
        elif index < 930:
            status = QueueStatus.ADMITTED
            next_check_at = None
        elif index < 960:
            status = QueueStatus.EXPIRED
            next_check_at = None
        else:
            status = QueueStatus.FAILED
            queue_id = None
            next_check_at = None
            last_error = "creation:permanent_failure"

        session = QueueSession(
            session_id=session_id,
            queue_id=queue_id,
            transfer_url=f"https://queue.test/transfer/{index:04d}",
            mode=SessionMode.HYBRID,
            status=status,
            state_path=state_store.path_for(session_id),
            created_at=now - timedelta(hours=1, microseconds=index),
            next_check_at=next_check_at,
            last_error=last_error,
            worker_id=worker_id,
            lease_until=lease_until,
        )
        await repository.create(session)
        if queue_id is not None:
            await state_store.save(
                session_id,
                {
                    "cookies": [
                        {
                            "name": "synthetic-recovery",
                            "value": f"state-{index:04d}",
                        }
                    ],
                    "origins": [],
                },
            )


async def run_phase3_recovery_benchmark(
    database: Path,
    state_directory: Path,
    *,
    restart_repetitions: int = 3,
    now: datetime | None = None,
) -> Phase3RecoveryBenchmarkReport:
    """Exercise aggregate startup, repeated restart, lease recovery, and identity safety."""

    if restart_repetitions < 2:
        raise ValueError("restart_repetitions must be at least 2")
    if database.exists() or (state_directory.exists() and any(state_directory.iterdir())):
        raise RuntimeError("Use an absent database and empty state directory")
    benchmark_now = now or datetime.now(UTC)
    state_store = FileSystemStateStore(state_directory)
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    await seed_recovery_population(repository, state_store, now=benchmark_now)
    initial_summary = await StateConsistencyChecker(repository, state_store).recovery_summary(
        now=benchmark_now
    )
    initial_digest = await _identity_digest(repository)
    terminal_snapshot = await _terminal_snapshot(repository)
    await repository.close()

    restart_latencies: list[float] = []
    for _ in range(restart_repetitions):
        restarted = SQLiteSessionRepository(database)
        started = time.perf_counter()
        await restarted.initialize()
        summary = await restarted.recovery_summary(now=benchmark_now)
        restart_latencies.append((time.perf_counter() - started) * 1000)
        if summary.total_persisted_sessions != 1000:
            raise AssertionError("restart lost persisted sessions")
        if await _identity_digest(restarted) != initial_digest:
            raise AssertionError("restart changed persisted Queue-it identity")
        await restarted.close()

    recovered_repository = SQLiteSessionRepository(database)
    await recovered_repository.initialize()
    handler = _RecoveryHandler(recovered_repository, benchmark_now)
    scheduler = ParkedSessionScheduler(
        repository=recovered_repository,
        handler=handler,
        worker_count=5,
        queue_capacity=50,
        claim_batch_size=50,
        lease_seconds=120,
        failure_delay_seconds=30,
        clock=lambda: benchmark_now,
        scheduler_id="recovery-scheduler",
    )
    await scheduler.start()
    worker_tasks = scheduler.worker_task_count
    claimed = await scheduler.schedule_due()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    final_summary = await StateConsistencyChecker(
        recovered_repository,
        state_store,
    ).recovery_summary(now=benchmark_now)
    final_digest = await _identity_digest(recovered_repository)
    final_terminal_snapshot = await _terminal_snapshot(recovered_repository)
    await recovered_repository.close()

    expected_expired_ids = {f"recovery-{index:04d}" for index in range(50, 100)}
    checked_ids = set(handler.checked_ids)
    expired_recovered = len(checked_ids & expected_expired_ids)
    return Phase3RecoveryBenchmarkReport(
        generated_at=datetime.now(UTC),
        session_count=initial_summary.total_persisted_sessions,
        restart_repetitions=restart_repetitions,
        restart_duration_average_ms=statistics.fmean(restart_latencies),
        restart_duration_p50_ms=percentile(restart_latencies, 50) or 0.0,
        restart_duration_p95_ms=percentile(restart_latencies, 95) or 0.0,
        restart_duration_maximum_ms=max(restart_latencies),
        initial_summary=initial_summary,
        final_summary=final_summary,
        identity_preserved=final_digest == initial_digest,
        terminal_states_preserved=final_terminal_snapshot == terminal_snapshot,
        state_associations_consistent=final_summary.missing_state_files == 0
        and final_summary.corrupt_state_files == 0,
        expired_leases_recovered=expired_recovered,
        resumed_checks=claimed,
        scheduler_worker_tasks=worker_tasks,
        scheduler_queue_capacity=scheduler.queue_capacity,
        scheduler_queue_peak=scheduler.metrics.maximum_queue_depth,
    )


async def _identity_digest(repository: SQLiteSessionRepository) -> str:
    sessions = await repository.list()
    digest = hashlib.sha256()
    for session in sessions:
        digest.update(session.session_id.encode())
        digest.update(b"\0")
        digest.update((session.queue_id or "").encode())
        digest.update(b"\0")
        digest.update(session.transfer_url.encode())
        digest.update(b"\0")
        digest.update(str(session.state_path).encode())
        digest.update(b"\n")
    return digest.hexdigest()


async def _terminal_snapshot(
    repository: SQLiteSessionRepository,
) -> dict[str, QueueStatus]:
    terminal = {QueueStatus.ADMITTED, QueueStatus.EXPIRED, QueueStatus.FAILED}
    return {
        session.session_id: session.status
        for session in await repository.list()
        if session.status in terminal
    }


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, QueueStatus):
        return value.value
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark synthetic recovery across 1,000 persisted sessions"
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--restarts", type=int, default=3)
    args = parser.parse_args()
    if (args.database is None) != (args.state_directory is None):
        parser.error("--database and --state-directory must be provided together")
    return args


def main() -> None:
    args = _parse_args()
    if args.database is not None and args.state_directory is not None:
        report = asyncio.run(
            run_phase3_recovery_benchmark(
                args.database,
                args.state_directory,
                restart_repetitions=args.restarts,
            )
        )
    else:
        with tempfile.TemporaryDirectory(prefix="queue-load-test-phase3-recovery-") as directory:
            root = Path(directory)
            report = asyncio.run(
                run_phase3_recovery_benchmark(
                    root / "sessions.sqlite3",
                    root / "state",
                    restart_repetitions=args.restarts,
                )
            )
    if args.report is not None:
        report.write_json(args.report)
    print(report.render_text())


if __name__ == "__main__":
    main()
