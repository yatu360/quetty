"""Synthetic SQLite and local browser-state benchmark for Phase 3."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker
from queue_load_test.state.base import BrowserState


@dataclass(frozen=True, slots=True)
class StorageOperation:
    samples: int
    total_duration_seconds: float
    operations_per_second: float
    average_ms: float
    p50_ms: float
    p95_ms: float
    maximum_ms: float


@dataclass(frozen=True, slots=True)
class ConsistencySummary:
    consistent: bool
    database_sessions: int
    hybrid_sessions_requiring_state: int
    referenced_state_paths: int
    state_files: int
    temporary_files: int
    findings_by_kind: dict[str, int]
    scan_duration_ms: float


@dataclass(frozen=True, slots=True)
class Phase3StorageBenchmarkReport:
    generated_at: datetime
    session_count: int
    database_size_bytes: int
    state_directory_size_bytes: int
    state_file_count: int
    average_state_file_size_bytes: float
    minimum_state_file_size_bytes: int
    maximum_state_file_size_bytes: int
    directory_entry_count: int
    seed_database_duration_ms: float
    save: StorageOperation
    load: StorageOperation
    replace: StorageOperation
    delete: StorageOperation
    baseline_consistency: ConsistencySummary
    restart_consistency: ConsistencySummary
    restart_session_count: int
    event_loop_probe_ticks: int
    event_loop_lag_p95_ms: float
    event_loop_lag_maximum_ms: float

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
            "Phase 3 synthetic persistence and state benchmark",
            f"Sessions/state files: {self.session_count}/{self.state_file_count}",
            f"SQLite size: {self.database_size_bytes} bytes",
            (
                f"State storage: {self.state_directory_size_bytes} bytes total, "
                f"{self.average_state_file_size_bytes:.1f} bytes average"
            ),
        ]
        for name, operation in (
            ("Save", self.save),
            ("Load", self.load),
            ("Replace", self.replace),
            ("Delete", self.delete),
        ):
            lines.append(
                f"{name}: {operation.operations_per_second:.1f}/s, "
                f"p50={operation.p50_ms:.3f} ms, p95={operation.p95_ms:.3f} ms, "
                f"max={operation.maximum_ms:.3f} ms"
            )
        lines.extend(
            (
                (
                    f"Baseline consistency: "
                    f"{'PASS' if self.baseline_consistency.consistent else 'FAIL'} "
                    f"({sum(self.baseline_consistency.findings_by_kind.values())} findings)"
                ),
                (
                    f"Restart reconstruction: {self.restart_session_count} sessions; "
                    f"{'PASS' if self.restart_consistency.consistent else 'FAIL'}"
                ),
                (
                    f"Event-loop lag while operating: "
                    f"p95={self.event_loop_lag_p95_ms:.3f} ms, "
                    f"max={self.event_loop_lag_maximum_ms:.3f} ms"
                ),
            )
        )
        return "\n".join(lines) + "\n"


async def run_phase3_storage_benchmark(
    database: Path,
    state_directory: Path,
    *,
    session_count: int = 1000,
    delete_samples: int = 100,
    now: datetime | None = None,
) -> Phase3StorageBenchmarkReport:
    """Benchmark an empty, dedicated database and state directory."""

    if session_count < 1:
        raise ValueError("session_count must be at least 1")
    if delete_samples < 1 or delete_samples > session_count:
        raise ValueError("delete_samples must be between 1 and session_count")
    if database.exists() or (state_directory.exists() and any(state_directory.iterdir())):
        raise RuntimeError("Use an absent database and empty state directory")

    benchmark_now = now or datetime.now(UTC)
    repository = SQLiteSessionRepository(database)
    store = FileSystemStateStore(state_directory)
    await repository.initialize()
    stop_probe = asyncio.Event()
    probe_task = asyncio.create_task(_event_loop_lag_probe(stop_probe))
    try:
        started = time.perf_counter()
        for index in range(session_count):
            session_id = _session_id(index)
            await repository.create(
                QueueSession(
                    session_id=session_id,
                    queue_id=f"synthetic-storage-queue-{index:04d}",
                    transfer_url=f"https://queue.test/transfer/{index:04d}",
                    mode=SessionMode.HYBRID,
                    status=QueueStatus.PARKED,
                    state_path=store.path_for(session_id),
                    created_at=benchmark_now + timedelta(microseconds=index),
                    next_check_at=benchmark_now + timedelta(minutes=5),
                )
            )
        seed_database_duration_ms = (time.perf_counter() - started) * 1000

        save_latencies = await _measure_series(
            session_count,
            lambda index: store.save(_session_id(index), _representative_state(index, 1)),
        )
        load_latencies = await _measure_series(
            session_count,
            lambda index: _load_required(store, index),
        )
        replace_latencies = await _measure_series(
            session_count,
            lambda index: store.save(_session_id(index), _representative_state(index, 2)),
        )

        delete_probe_ids = [f"delete-probe-{index:04d}" for index in range(delete_samples)]
        for index, session_id in enumerate(delete_probe_ids):
            await store.save(session_id, _representative_state(index, 1))
        delete_latencies = await _measure_items(
            delete_probe_ids,
            lambda session_id: _delete_required(store, session_id),
        )

        baseline_consistency = await _measure_consistency(repository, store)
        state_files = sorted(state_directory.glob("*.json"))
        state_sizes = [path.stat().st_size for path in state_files]
        directory_entries = list(state_directory.iterdir())
    finally:
        stop_probe.set()
        probe_ticks, probe_lags = await probe_task
        await repository.close()

    restarted_repository = SQLiteSessionRepository(database)
    try:
        await restarted_repository.initialize()
        restarted_sessions = await restarted_repository.list()
        restart_consistency = await _measure_consistency(
            restarted_repository,
            FileSystemStateStore(state_directory),
        )
    finally:
        await restarted_repository.close()

    return Phase3StorageBenchmarkReport(
        generated_at=datetime.now(UTC),
        session_count=session_count,
        database_size_bytes=database.stat().st_size,
        state_directory_size_bytes=sum(state_sizes),
        state_file_count=len(state_files),
        average_state_file_size_bytes=statistics.fmean(state_sizes),
        minimum_state_file_size_bytes=min(state_sizes),
        maximum_state_file_size_bytes=max(state_sizes),
        directory_entry_count=len(directory_entries),
        seed_database_duration_ms=seed_database_duration_ms,
        save=_operation_summary(save_latencies),
        load=_operation_summary(load_latencies),
        replace=_operation_summary(replace_latencies),
        delete=_operation_summary(delete_latencies),
        baseline_consistency=baseline_consistency,
        restart_consistency=restart_consistency,
        restart_session_count=len(restarted_sessions),
        event_loop_probe_ticks=probe_ticks,
        event_loop_lag_p95_ms=percentile(probe_lags, 95) or 0.0,
        event_loop_lag_maximum_ms=max(probe_lags, default=0.0),
    )


def _session_id(index: int) -> str:
    return f"storage-{index:04d}"


def _representative_state(index: int, version: int) -> BrowserState:
    token = f"synthetic-{index:04d}-v{version}-" + ("x" * 256)
    return {
        "cookies": [
            {
                "name": "QueueITAccepted-Synthetic",
                "value": token,
                "domain": "queue.test",
                "path": "/",
                "expires": 2_000_000_000,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "synthetic-session",
                "value": token[::-1],
                "domain": "queue.test",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            },
        ],
        "origins": [
            {
                "origin": "https://queue.test",
                "localStorage": [
                    {"name": "synthetic-state", "value": token},
                    {"name": "version", "value": str(version)},
                ],
            }
        ],
    }


async def _load_required(store: FileSystemStateStore, index: int) -> BrowserState:
    state = await store.load(_session_id(index))
    if state is None:
        raise AssertionError(f"synthetic state {_session_id(index)} disappeared")
    return state


async def _delete_required(store: FileSystemStateStore, session_id: str) -> bool:
    deleted = await store.delete(session_id)
    if not deleted:
        raise AssertionError(f"synthetic delete probe {session_id} disappeared")
    return deleted


async def _measure_series(
    count: int,
    operation: Callable[[int], Awaitable[object]],
) -> list[float]:
    latencies: list[float] = []
    for index in range(count):
        started = time.perf_counter()
        await operation(index)
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


async def _measure_items[T](
    items: Sequence[T],
    operation: Callable[[T], Awaitable[object]],
) -> list[float]:
    latencies: list[float] = []
    for item in items:
        started = time.perf_counter()
        await operation(item)
        latencies.append((time.perf_counter() - started) * 1000)
    return latencies


def _operation_summary(latencies: Sequence[float]) -> StorageOperation:
    total_seconds = sum(latencies) / 1000
    return StorageOperation(
        samples=len(latencies),
        total_duration_seconds=total_seconds,
        operations_per_second=len(latencies) / total_seconds,
        average_ms=statistics.fmean(latencies),
        p50_ms=percentile(latencies, 50) or 0.0,
        p95_ms=percentile(latencies, 95) or 0.0,
        maximum_ms=max(latencies),
    )


async def _measure_consistency(
    repository: SQLiteSessionRepository,
    store: FileSystemStateStore,
) -> ConsistencySummary:
    started = time.perf_counter()
    report = await StateConsistencyChecker(repository, store).check()
    elapsed_ms = (time.perf_counter() - started) * 1000
    findings_by_kind: dict[str, int] = {}
    for finding in report.findings:
        findings_by_kind[finding.kind] = findings_by_kind.get(finding.kind, 0) + 1
    return ConsistencySummary(
        consistent=report.is_consistent,
        database_sessions=report.database_sessions,
        hybrid_sessions_requiring_state=report.hybrid_sessions_requiring_state,
        referenced_state_paths=report.referenced_state_paths,
        state_files=report.state_files,
        temporary_files=report.temporary_files,
        findings_by_kind=findings_by_kind,
        scan_duration_ms=elapsed_ms,
    )


async def _event_loop_lag_probe(stop: asyncio.Event) -> tuple[int, list[float]]:
    interval = 0.001
    loop = asyncio.get_running_loop()
    target = loop.time() + interval
    lags: list[float] = []
    while not stop.is_set():
        await asyncio.sleep(interval)
        observed = loop.time()
        lags.append(max(0.0, observed - target) * 1000)
        target = observed + interval
    return len(lags), lags


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark SQLite and local browser state with synthetic sessions"
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--sessions", type=int, default=1000)
    parser.add_argument("--delete-samples", type=int, default=100)
    args = parser.parse_args()
    if (args.database is None) != (args.state_directory is None):
        parser.error("--database and --state-directory must be provided together")
    return args


def main() -> None:
    args = _parse_args()
    if args.database is not None and args.state_directory is not None:
        report = asyncio.run(
            run_phase3_storage_benchmark(
                args.database,
                args.state_directory,
                session_count=args.sessions,
                delete_samples=args.delete_samples,
            )
        )
    else:
        with tempfile.TemporaryDirectory(prefix="queue-load-test-phase3-storage-") as directory:
            root = Path(directory)
            report = asyncio.run(
                run_phase3_storage_benchmark(
                    root / "sessions.sqlite3",
                    root / "state",
                    session_count=args.sessions,
                    delete_samples=args.delete_samples,
                )
            )
    if args.report is not None:
        report.write_json(args.report)
    print(report.render_text())


if __name__ == "__main__":
    main()
