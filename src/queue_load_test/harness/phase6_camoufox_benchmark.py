"""Controlled Phase 6 Camoufox capacity, park/reopen, and recovery benchmark.

Evidence produced here is **local and controlled**. It drives real Camoufox (and,
for the descriptive comparison, installed Chrome) against
:class:`LocalQueueSimulator` on 127.0.0.1, with real SQLite, real state files, and
real ``SIGKILL``-ed browser processes. It never contacts Queue-it or a staging
environment, so it cannot establish Queue-it throughput, restore reliability, or a
staging-safe concurrency level.

The durable identity under test is the persisted expected Queue ID. Fresh contexts
may present different fingerprint/browser-device characteristics; those values are
never read, compared, or persisted. A changed Queue ID is a failure.

The report is aggregate: no Queue IDs, session IDs, transfer URLs, storage state, or
fingerprint data. Synthetic identities exist only in the temporary work directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import math
import os
import platform
import signal
import sqlite3
import statistics
import sys
import tempfile
import time
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

from queue_load_test.browser import (
    BrowserContextCapacity,
    BrowserManager,
    CamoufoxBackend,
    ChromeBackend,
)
from queue_load_test.browser.backend import BrowserBackend
from queue_load_test.config import Settings
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase4_recovery import (
    Outcome,
    ScenarioResult,
    harness_polling_policy,
)
from queue_load_test.harness.phase5_workflow import workflow_settings
from queue_load_test.harness.resource_benchmark import (
    PsutilProcessResourceProbe,
    is_browser_main_process,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import (
    BrowserBackendName,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    QueueSessionCreator,
    QueueSessionMonitor,
)
from queue_load_test.scheduler.creation import CreationOutcomeKind, CreationWorkItem
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer, RestoreFailure, SessionRestoreResult
from queue_load_test.web.actions import OperatorAction, OperatorActionKind, OperatorActionStatus
from queue_load_test.web.manual import ManualOpenError, ManualOpenResult
from queue_load_test.web.service import ApplicationRunRuntime

try:  # optional benchmark extra
    psutil: Any = importlib.import_module("psutil")
except ImportError:  # pragma: no cover
    psutil = None

DEFAULT_CONTEXT_LEVELS = (5, 10, 20, 30, 40, 50)
CONTEXTS_PER_PROCESS = 25
NAVIGATION_TIMEOUT_MS = 10_000
_OBSERVATION_TIMEOUT_SECONDS = 3.0


# --- small shared helpers --------------------------------------------------------------


def stats(values: Iterable[float]) -> dict[str, float | int | None]:
    samples = list(values)
    if not samples:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(samples),
        "mean": round(statistics.fmean(samples), 4),
        "p50": round(percentile(samples, 50) or 0.0, 4),
        "p95": round(percentile(samples, 95) or 0.0, 4),
        "max": round(max(samples), 4),
    }


def average_peak(values: Iterable[float | int | None]) -> dict[str, float | None]:
    available = [float(value) for value in values if value is not None]
    return {
        "average": round(statistics.fmean(available), 2) if available else None,
        "peak": round(max(available), 2) if available else None,
    }


def metric_total(metrics: PrometheusMetrics, name: str) -> float:
    """Sum every labelled sample of one counter family (``_total`` samples only)."""

    total = 0.0
    for family in metrics.registry.collect():
        for sample in family.samples:
            if sample.name == name:
                total += float(sample.value)
    return total


def histogram_count(metrics: PrometheusMetrics, name: str) -> float:
    return metric_total(metrics, f"{name}_count")


def browser_main_processes(backend: BrowserBackendName) -> list[Any]:
    """Top-level managed browser processes descended from this Python process."""

    if psutil is None:
        return []
    found = []
    try:
        children = psutil.Process(os.getpid()).children(recursive=True)
    except psutil.Error:
        return []
    for process in children:
        try:
            if is_browser_main_process(backend, process.cmdline()):
                found.append(process)
        except psutil.Error:
            continue
    return found


def main_pids(backend: BrowserBackendName) -> set[int]:
    return {int(process.pid) for process in browser_main_processes(backend)}


def processes_gone(backend: BrowserBackendName, pids: set[int]) -> Callable[[], bool]:
    return lambda: not (main_pids(backend) & pids)


def new_processes_gone(backend: BrowserBackendName, baseline: set[int]) -> Callable[[], bool]:
    return lambda: not (main_pids(backend) - baseline)


def slot_disconnected(manager: BrowserManager, index: int = 0) -> Callable[[], bool]:
    return lambda: not manager._slots[index].browser.is_connected()


def kill_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


async def until(
    predicate: Callable[[], Awaitable[bool] | bool],
    *,
    timeout: float,
    interval: float = 0.05,
) -> float | None:
    started = time.perf_counter()
    while True:
        outcome = predicate()
        if isinstance(outcome, Awaitable):
            outcome = await outcome
        if outcome:
            return round(time.perf_counter() - started, 3)
        if time.perf_counter() - started > timeout:
            return None
        await asyncio.sleep(interval)


def make_backend(backend: BrowserBackendName, *, serialize: bool = True) -> BrowserBackend:
    if backend is BrowserBackendName.CHROME:
        return ChromeBackend()
    return CamoufoxBackend(serialize_contexts=serialize)


def make_manager(
    backend: BrowserBackendName,
    *,
    processes: int,
    contexts_per_process: int,
    max_active: int | None = None,
    metrics: PrometheusMetrics | None = None,
    serialize: bool = True,
    shared_capacity: BrowserContextCapacity | None = None,
) -> BrowserManager:
    return BrowserManager(
        chrome_process_count=processes,
        max_contexts_per_browser=contexts_per_process,
        max_active_contexts=max_active or processes * contexts_per_process,
        headless=True,
        observability=metrics,
        shared_capacity=shared_capacity,
        backend=make_backend(backend, serialize=serialize),
        operation_timeout_seconds=20.0,
        close_timeout_seconds=5.0,
    )


@dataclass(slots=True)
class RestoreObservation:
    duration_seconds: float
    success: bool
    identity_match: bool | None
    failure: str | None
    method: str
    state_refreshed: bool
    mode: str
    attempt_failures: tuple[str, ...] = ()


class RecordingRestorer:
    """Delegate to the real restorer and keep sanitized per-restore evidence."""

    def __init__(self, restorer: QueueSessionRestorer) -> None:
        self.restorer = restorer
        self.observations: list[RestoreObservation] = []

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        started = time.perf_counter()
        result = await self.restorer.restore(session)
        self.observations.append(
            RestoreObservation(
                duration_seconds=time.perf_counter() - started,
                success=result.success,
                identity_match=result.identity_match,
                failure=result.failure.value if result.failure is not None else None,
                method=result.method.value,
                state_refreshed=result.state_refreshed,
                mode=session.mode.value,
                attempt_failures=tuple(
                    f"{attempt.method.value}:{attempt.failure.value}"
                    for attempt in result.attempts
                    if attempt.failure is not None
                ),
            )
        )
        return result

    def reset(self) -> None:
        self.observations.clear()

    def summary(self) -> dict[str, object]:
        items = self.observations
        return {
            "restores": len(items),
            "successes": sum(item.success for item in items),
            "identity_mismatches": sum(item.identity_match is False for item in items),
            "failures": dict(Counter(item.failure for item in items if item.failure)),
            "attempt_failures": dict(
                Counter(failure for item in items for failure in item.attempt_failures)
            ),
            "methods": dict(Counter(item.method for item in items if item.success)),
            "hybrid_state_refresh_failures": sum(
                item.success and item.mode == SessionMode.HYBRID.value and not item.state_refreshed
                for item in items
            ),
            "latency_seconds": stats(item.duration_seconds for item in items),
        }


def make_restorer(
    manager: BrowserManager,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    simulator: LocalQueueSimulator,
    backend: BrowserBackendName,
    metrics: PrometheusMetrics | None = None,
) -> QueueSessionRestorer:
    return QueueSessionRestorer(
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
        expected_journey_url=simulator.queue_url,
        storage_navigation_url=simulator.queue_url,
        admission_detector=AdmissionDetector.from_urls(simulator.protected_url),
        navigation_timeout_ms=NAVIGATION_TIMEOUT_MS,
        admission_wait_timeout_ms=0,
        observation_timeout_seconds=_OBSERVATION_TIMEOUT_SECONDS,
        observation_interval_seconds=0.1,
        observability=metrics,
        browser_backend=backend,
    )


def make_scheduler(
    repository: SQLiteSessionRepository,
    restorer: RecordingRestorer,
    *,
    workers: int,
    metrics: PrometheusMetrics | None,
    scheduler_id: str,
    lease_seconds: float = 30.0,
) -> ParkedSessionScheduler:
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
    return ParkedSessionScheduler(
        repository=repository,
        handler=monitor,
        worker_count=workers,
        queue_capacity=max(2, workers * 2),
        claim_batch_size=max(2, workers * 2),
        lease_seconds=lease_seconds,
        failure_delay_seconds=3600,
        scheduler_tick_seconds=0.05,
        shutdown_timeout_seconds=15,
        scheduler_id=scheduler_id,
        observability=metrics,
    )


def mark_all_due(database: Path, session_ids: Iterable[str] | None = None) -> int:
    """Make parked sessions due now through an independent connection (harness only)."""

    due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database) as connection:
        if session_ids is None:
            cursor = connection.execute(
                "UPDATE queue_sessions SET next_check_at = ? "
                "WHERE status NOT IN ('ADMITTED', 'EXPIRED', 'FAILED')",
                (due,),
            )
        else:
            ids = list(session_ids)
            cursor = connection.executemany(
                "UPDATE queue_sessions SET next_check_at = ? WHERE session_id = ?",
                [(due, session_id) for session_id in ids],
            )
        return cursor.rowcount


def owned_rows(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        leases, owners = connection.execute(
            "SELECT SUM(worker_id IS NOT NULL), SUM(manual_owner_id IS NOT NULL) "
            "FROM queue_sessions"
        ).fetchone()
    return {"automatic_leases": int(leases or 0), "manual_owners": int(owners or 0)}


def identity_map(database: Path) -> dict[str, str | None]:
    with sqlite3.connect(database) as connection:
        return dict(connection.execute("SELECT session_id, queue_id FROM queue_sessions"))


def identity_digest(identities: dict[str, str | None]) -> str:
    """Stable digest over (session, Queue ID) pairs; the pairs never leave memory."""

    digest = hashlib.sha256()
    for session_id, queue_id in sorted(identities.items()):
        digest.update(f"{session_id}\0{queue_id}\n".encode())
    return digest.hexdigest()


async def run_sweep(
    database: Path,
    repository: SQLiteSessionRepository,
    scheduler: ParkedSessionScheduler,
    manager: BrowserManager,
    *,
    timeout_seconds: float,
    on_tick: Callable[[float], Awaitable[None]] | None = None,
) -> dict[str, object]:
    """Run the real scheduler until nothing is due, queued, or checking."""

    stop = asyncio.Event()
    started = time.perf_counter()
    task = asyncio.create_task(scheduler.run(stop), name="phase6-sweep")
    queue_depths: list[int] = []
    backlogs: list[int] = []
    overdue: list[float] = []
    peak_contexts = 0
    completed = False
    checked_before = scheduler.metrics.checked
    try:
        while time.perf_counter() - started < timeout_seconds:
            await asyncio.sleep(0.1)
            elapsed = time.perf_counter() - started
            peak_contexts = max(peak_contexts, manager.active_context_count)
            queue_depths.append(scheduler.queue_size)
            backlogs.append(scheduler.metrics.due_backlog)
            overdue.append(scheduler.metrics.oldest_overdue_seconds)
            if on_tick is not None:
                await on_tick(elapsed)
            summary = await repository.due_session_summary(now=datetime.now(UTC))
            if (
                summary.count == 0
                and scheduler.queue_size == 0
                and scheduler.metrics.currently_checking == 0
                and scheduler.metrics.checked > checked_before
            ):
                completed = True
                break
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=30)
    return {
        "completed": completed,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "checks": scheduler.metrics.checked - checked_before,
        "peak_active_contexts": peak_contexts,
        "queue_depth": average_peak(queue_depths),
        "due_backlog": average_peak(backlogs),
        "oldest_overdue_seconds": average_peak(overdue),
        "contexts_after": manager.active_context_count,
        "leases_after": owned_rows(database)["automatic_leases"],
    }


async def create_population(
    *,
    manager: BrowserManager,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    simulator: LocalQueueSimulator,
    backend: BrowserBackendName,
    count: int,
    concurrency: int,
    prefix: str,
    mode: SessionMode = SessionMode.HYBRID,
) -> dict[str, object]:
    """Acquire synthetic Queue identities through the real creator and park them."""

    creator = QueueSessionCreator(
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
        staging_url=simulator.entry_url,
        state_directory=state_store.directory,
        mode=mode,
        browser_backend=backend,
        navigation_timeout_ms=NAVIGATION_TIMEOUT_MS,
        live_page_timeout_seconds=10.0,
    )
    gate = asyncio.Semaphore(concurrency)
    outcomes: Counter[str] = Counter()
    durations: list[float] = []

    async def one(sequence: int) -> None:
        async with gate:
            outcome = await creator.create(
                CreationWorkItem(sequence=sequence, session_id=f"{prefix}-{sequence:04d}")
            )
            outcomes[outcome.kind.value] += 1
            durations.append(outcome.duration_seconds)

    started = time.perf_counter()
    await asyncio.gather(*(one(index) for index in range(1, count + 1)))
    return {
        "requested": count,
        "outcomes": dict(outcomes),
        "created": outcomes[CreationOutcomeKind.SUCCESS.value],
        "duration_seconds": round(time.perf_counter() - started, 3),
        "creation_latency_seconds": stats(durations),
    }


async def seed_sessions(
    repository: SQLiteSessionRepository,
    simulator: LocalQueueSimulator,
    state_store: FileSystemStateStore,
    *,
    backend: BrowserBackendName,
    count: int,
    prefix: str,
) -> list[str]:
    """Persist synthetic TRANSFER-restorable sessions without browser acquisition."""

    ids = []
    for index in range(count):
        queue_id = f"{prefix}-q{index:04d}"
        session_id = f"{prefix}-s{index:04d}"
        simulator.stages.setdefault(queue_id, "active")
        await repository.create(
            QueueSession(
                session_id=session_id,
                queue_id=queue_id,
                transfer_url=simulator.transfer_url(queue_id),
                mode=SessionMode.HYBRID,
                browser_backend=backend,
                status=QueueStatus.ACTIVE_QUEUE,
                state_path=state_store.path_for(session_id),
                next_check_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        ids.append(session_id)
    return ids


# --- 1-3: capacity cases -------------------------------------------------------------


class _SkipSweep(Exception):
    """Internal: end a capacity case after its hold phase."""


@dataclass(frozen=True, slots=True)
class CapacityCase:
    backend: BrowserBackendName
    contexts: int
    processes: int
    serialized: bool
    label: str

    @property
    def family(self) -> str:
        if self.backend is BrowserBackendName.CHROME:
            return "chrome-shared-processes" if self.processes < self.contexts else "chrome-p"
        return "camoufox-serialized" if self.serialized else "camoufox-unserialized"

    @classmethod
    def shared(cls, backend: BrowserBackendName, contexts: int) -> CapacityCase:
        """``contexts`` concurrent contexts sharing ceil(contexts / 25) processes."""

        processes = max(1, math.ceil(contexts / CONTEXTS_PER_PROCESS))
        name = "unserialized" if backend is BrowserBackendName.CAMOUFOX else "shared"
        return cls(backend, contexts, processes, False, f"{backend.value}-{name}-c{contexts}")

    @classmethod
    def per_process(cls, backend: BrowserBackendName, processes: int) -> CapacityCase:
        """One live context per process: the shipped Camoufox profile (processes <= 4)."""

        serialized = backend is BrowserBackendName.CAMOUFOX
        name = "serialized" if serialized else "per-process"
        return cls(backend, processes, processes, serialized, f"{backend.value}-{name}-p{processes}")


def churn_failed(result: dict[str, Any]) -> bool:
    restoration = result.get("restoration") or {}
    restores = int(restoration.get("restores", 0) or 0)
    return bool(restores) and int(restoration.get("successes", 0) or 0) < restores


def stop_reason(
    result: dict[str, Any],
    *,
    host_ram_bytes: int | None,
    logical_cpus: int,
) -> str | None:
    """Objective reasons not to run the next, larger case for the same backend."""

    failures = result["failures"]
    if result["status"] != "COMPLETED":
        return f"case failed ({result['error_category']})"
    if failures["context_creation_failures"] or failures["browser_crashes"]:
        return "context creation failures or browser crashes"
    if failures["navigation_failures"]:
        return "hold-phase navigation failures"
    if failures["identity_mismatches"]:
        return "Queue ID mismatches"
    if failures["browser_operation_timeouts"]:
        return "browser operation timeouts"
    if result["achieved_active_contexts"] < result["configured_contexts"]:
        return "context shortfall"
    resources = result["resources"]
    ram_peak = sum(
        value
        for value in (
            resources["application_rss_bytes"]["peak"],
            resources["browser_tree_rss_bytes"]["peak"],
        )
        if value is not None
    )
    if host_ram_bytes and ram_peak >= 0.8 * host_ram_bytes:
        return "summed peak RSS reached 80% of host RAM"
    cpu_average = resources["browser_cpu_percent"]["average"] or 0.0
    if cpu_average >= 0.9 * 100 * logical_cpus:
        return "average browser CPU reached 90% of host capacity"
    return None


async def run_capacity_case(
    case: CapacityCase,
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    navigation_waves: int,
    sample_interval_seconds: float,
    allocation_workers: int = 10,
    run_sweep_phase: bool = True,
) -> dict[str, Any]:
    """Hold ``contexts`` live contexts through navigation waves, then run one churn sweep.

    The hold phase creates every context first and then navigates them together. The
    sweep is the production pattern: fixed workers repeatedly create a fresh context,
    restore, inspect, and close, so creation and navigation overlap continuously.
    """

    metrics = PrometheusMetrics()
    manager = make_manager(
        case.backend,
        processes=case.processes,
        contexts_per_process=math.ceil(case.contexts / case.processes),
        max_active=case.contexts,
        metrics=metrics,
        serialize=case.serialized,
    )
    probe = PsutilProcessResourceProbe(backend=case.backend)
    samples: list[dict[str, Any]] = []
    stop_sampling = asyncio.Event()
    owned: list[Any] = []
    acquisition: list[float] = []
    creation: list[float] = []
    lock_wait: list[float] = []
    navigation: list[float] = []
    creation_failures = 0
    navigation_failures = 0
    peak_contexts = 0
    status = "COMPLETED"
    error: str | None = None
    sweep: dict[str, object] = {}
    recorder: RecordingRestorer | None = None
    repository: SQLiteSessionRepository | None = None

    async def sampler() -> None:
        nonlocal peak_contexts
        while True:
            snapshot = probe.sample()
            peak_contexts = max(peak_contexts, manager.active_context_count)
            samples.append(
                {
                    "application_cpu_percent": snapshot.application_cpu_percent,
                    "application_rss_bytes": snapshot.application_ram_bytes,
                    "browser_cpu_percent": snapshot.browser_cpu_percent,
                    "browser_tree_rss_bytes": snapshot.browser_ram_bytes,
                    "browser_tree_processes": snapshot.observed_browser_processes,
                    "browser_main_processes": snapshot.observed_browser_main_processes,
                    "active_contexts": manager.active_context_count,
                }
            )
            try:
                await asyncio.wait_for(stop_sampling.wait(), timeout=sample_interval_seconds)
                return
            except TimeoutError:
                continue

    started = time.perf_counter()
    sampling = asyncio.create_task(sampler(), name=f"sampler-{case.label}")
    try:
        await manager.start()
        work: asyncio.Queue[int] = asyncio.Queue()
        for index in range(case.contexts):
            work.put_nowait(index)

        async def allocate() -> None:
            nonlocal creation_failures
            while True:
                try:
                    work.get_nowait()
                except asyncio.QueueEmpty:
                    return
                began = time.perf_counter()
                try:
                    context = await manager.create_context()
                except Exception:  # noqa: BLE001 - recorded case failure
                    creation_failures += 1
                    continue
                acquisition.append(time.perf_counter() - began)
                creation.append(context.creation_duration_seconds)
                lock_wait.append(context.acquisition_wait_seconds)
                owned.append((context, await context.context.new_page()))

        await asyncio.gather(
            *(allocate() for _ in range(min(allocation_workers, case.contexts)))
        )

        async def navigate(index: int, page: Any) -> None:
            nonlocal navigation_failures
            began = time.perf_counter()
            try:
                response = await page.goto(
                    simulator.transfer_url(f"capacity-{index:03d}"),
                    wait_until="domcontentloaded",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
                if response is not None and response.status >= 400:
                    navigation_failures += 1
            except Exception:  # noqa: BLE001 - timeouts and navigation errors
                navigation_failures += 1
            finally:
                navigation.append(time.perf_counter() - began)

        for _ in range(navigation_waves):
            await asyncio.gather(
                *(navigate(index, page) for index, (_, page) in enumerate(owned))
            )
        achieved = manager.active_context_count
        for context, _ in owned:
            await context.close()
        owned.clear()

        if not run_sweep_phase:
            sweep = {"skipped": "churn sweep already failed at a lower level"}
            raise _SkipSweep
        # One real scheduler sweep at the same concurrency: 2x contexts due sessions.
        repository = SQLiteSessionRepository(directory / f"{case.label}.sqlite3")
        await repository.initialize()
        state_store = FileSystemStateStore(directory / f"{case.label}-state")
        await seed_sessions(
            repository,
            simulator,
            state_store,
            backend=case.backend,
            count=max(8, case.contexts * 2),
            prefix=case.label,
        )
        recorder = RecordingRestorer(
            make_restorer(manager, repository, state_store, simulator, case.backend, metrics)
        )
        scheduler = make_scheduler(
            repository,
            recorder,
            workers=case.contexts,
            metrics=metrics,
            scheduler_id=f"capacity-{case.label}",
        )
        sweep = await run_sweep(
            directory / f"{case.label}.sqlite3",
            repository,
            scheduler,
            manager,
            timeout_seconds=max(60.0, case.contexts * 6.0),
        )
        duration = float(str(sweep["duration_seconds"])) or 1.0
        successes = sum(item.success for item in recorder.observations)
        sweep["local_simulator_checks_per_second"] = round(successes / duration, 3)
    except _SkipSweep:
        pass
    except Exception as exc:  # noqa: BLE001 - preserve objective failure output
        status = "FAILED"
        error = type(exc).__name__
        achieved = manager.active_context_count if manager.started else 0
    finally:
        for context, _ in owned:
            with contextlib.suppress(Exception):
                await context.close()
        contexts_final = manager.active_context_count if manager.started else 0
        await manager.shutdown()
        if repository is not None:
            await repository.close()
        stop_sampling.set()
        await sampling
    await asyncio.sleep(0.5)
    restore_summary = recorder.summary() if recorder is not None else {}
    return {
        "case": case.label,
        "family": case.family,
        "backend": case.backend.value,
        "status": status,
        "error_category": error,
        "serialized_contexts": case.serialized,
        "configured_browser_processes": case.processes,
        "configured_contexts": case.contexts,
        "configured_contexts_per_process": math.ceil(case.contexts / case.processes),
        "achieved_active_contexts": achieved,
        "observed_peak_active_contexts": peak_contexts,
        "navigation_waves": navigation_waves,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "context_creation_seconds": stats(creation),
        "context_acquisition_seconds": stats(acquisition),
        "allocation_lock_wait_seconds": stats(lock_wait),
        "local_navigation_seconds": stats(navigation),
        "sweep": sweep,
        "restoration": restore_summary,
        "resources": {
            key: average_peak(sample[key] for sample in samples)
            for key in (
                "application_cpu_percent",
                "application_rss_bytes",
                "browser_cpu_percent",
                "browser_tree_rss_bytes",
                "browser_tree_processes",
                "browser_main_processes",
            )
        },
        "samples": len(samples),
        "failures": {
            "context_creation_failures": creation_failures
            + int(metric_total(metrics, "browser_context_creation_failures_total")),
            "navigation_failures": navigation_failures,
            "restore_navigation_failures": int(
                metric_total(metrics, "navigation_failures_total")
            ),
            "transfer_restore_failures": int(
                metric_total(metrics, "transfer_restore_failures_total")
            ),
            "storage_restore_failures": int(
                metric_total(metrics, "state_restore_failures_total")
            ),
            "identity_mismatches": int(str(restore_summary.get("identity_mismatches", 0) or 0)),
            "browser_crashes": int(metric_total(metrics, "browser_crashes_total")),
            "browser_operation_timeouts": int(
                metric_total(metrics, "browser_operation_timeouts_total")
            ),
            "cleanup_failures": int(metric_total(metrics, "browser_cleanup_failures_total")),
            "lost_contexts": int(metric_total(metrics, "browser_contexts_lost_total")),
            "restarts": int(histogram_count(metrics, "browser_restart_duration_seconds")),
        },
        "final": {
            "contexts": contexts_final,
            "browser_main_processes": len(browser_main_processes(case.backend)),
        },
    }


async def run_capacity_matrix(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backends: Sequence[BrowserBackendName],
    levels: Sequence[int],
    navigation_waves: int,
    sample_interval_seconds: float,
    per_process_levels: Sequence[int] = (1, 2, 3, 4),
) -> dict[str, Any]:
    """Run each family in increasing order, stopping a family on objective evidence."""

    directory.mkdir(parents=True, exist_ok=True)
    host_ram = int(psutil.virtual_memory().total) if psutil is not None else None
    cpus = os.cpu_count() or 1
    families: list[list[CapacityCase]] = []
    for backend in backends:
        families.append([CapacityCase.per_process(backend, count) for count in per_process_levels])
        families.append([CapacityCase.shared(backend, level) for level in levels])
    results: list[dict[str, Any]] = []
    stopped: dict[str, dict[str, object]] = {}
    churn_failures: dict[str, str] = {}
    for family in families:
        for case in family:
            result = await run_capacity_case(
                case,
                simulator,
                directory,
                navigation_waves=navigation_waves,
                sample_interval_seconds=sample_interval_seconds,
                run_sweep_phase=case.family not in churn_failures,
            )
            results.append(result)
            restoration = result.get("restoration") or {}
            print(
                f"[capacity] {case.label}: {result['status']} "
                f"achieved={result['achieved_active_contexts']} "
                f"restores={restoration.get('successes')}/{restoration.get('restores')} "
                f"hold_nav_failures={result['failures']['navigation_failures']}",
                flush=True,
            )
            if churn_failed(result) and case.family not in churn_failures:
                churn_failures[case.family] = case.label
            reason = stop_reason(result, host_ram_bytes=host_ram, logical_cpus=cpus)
            if reason is not None:
                stopped[case.family] = {"after_case": case.label, "reason": reason}
                break
    return {
        "cases": results,
        "stopped_early": stopped,
        "churn_sweep_first_failure": churn_failures,
    }


# --- 4-6: park/reopen, full browser restart, application restart --------------------


@dataclass(slots=True)
class PopulationContext:
    database: Path
    state_directory: Path
    session_ids: list[str] = field(default_factory=list)
    baseline_identities: dict[str, str | None] = field(default_factory=dict)


async def scenario_park_reopen(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
    size: int,
    sweeps: int,
    processes: int,
    workers: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key=f"park_reopen_{backend.value}",
        title=f"{backend.value} park/reopen sweeps over {size} persisted sessions",
        evidence="real browser + LocalQueueSimulator + SQLite + state files",
    )
    metrics = PrometheusMetrics()
    manager = make_manager(
        backend, processes=processes, contexts_per_process=workers, metrics=metrics
    )
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    state_store = FileSystemStateStore(population.state_directory)
    try:
        await manager.start()
        creation = await create_population(
            manager=manager,
            repository=repository,
            state_store=state_store,
            simulator=simulator,
            backend=backend,
            count=size,
            concurrency=workers,
            prefix=f"park-{backend.value}",
        )
        result.measurements["creation"] = creation
        population.baseline_identities = identity_map(population.database)
        population.session_ids = sorted(population.baseline_identities)
        result.check("population_created", creation["created"] == size)
        result.check(
            "unique_queue_ids",
            len(set(population.baseline_identities.values())) == size
            and None not in population.baseline_identities.values(),
        )
        result.check(
            "hybrid_state_saved_for_every_session",
            all(state_store.path_for(sid).exists() for sid in population.session_ids),
        )
        result.check("contexts_closed_after_creation", manager.active_context_count == 0)
        recorder = RecordingRestorer(
            make_restorer(manager, repository, state_store, simulator, backend, metrics)
        )
        sweep_rows = []
        baseline = identity_digest(population.baseline_identities)
        new_before_sweeps = simulator.new_identities
        for sweep_index in range(sweeps):
            mark_all_due(population.database)
            recorder.reset()
            # A scheduler drains and stops at the end of run(); each sweep gets a new one.
            scheduler = make_scheduler(
                repository,
                recorder,
                workers=workers,
                metrics=metrics,
                scheduler_id=f"park-reopen-{sweep_index + 1}",
            )
            sweep = await run_sweep(
                population.database, repository, scheduler, manager, timeout_seconds=300
            )
            summary = recorder.summary()
            identities_now = identity_digest(identity_map(population.database))
            sweep_rows.append({"sweep": sweep_index + 1, **sweep, "restoration": summary})
            result.check(f"sweep_{sweep_index + 1}_completed", bool(sweep["completed"]))
            result.check(
                f"sweep_{sweep_index + 1}_every_session_verified",
                summary["successes"] == size and summary["identity_mismatches"] == 0,
            )
            result.check(f"sweep_{sweep_index + 1}_queue_ids_unchanged", identities_now == baseline)
            result.check(
                f"sweep_{sweep_index + 1}_zero_contexts_and_leases_after",
                sweep["contexts_after"] == 0 and sweep["leases_after"] == 0,
            )
            result.check(
                f"sweep_{sweep_index + 1}_no_state_refresh_failures",
                summary["hybrid_state_refresh_failures"] == 0,
            )
        result.measurements["sweeps"] = sweep_rows
        result.check(
            "no_new_identities_during_sweeps", simulator.new_identities == new_before_sweeps
        )
        result.measurements["browser_restarts"] = manager.restart_count
    finally:
        await manager.shutdown()
        await repository.close()
    return result.finish()


async def scenario_full_browser_restart(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
    processes: int,
    cycles: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key=f"full_browser_restart_{backend.value}",
        title="Close every browser process, relaunch, restore parked sessions",
        evidence="real browser processes fully stopped and relaunched",
    )
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    state_store = FileSystemStateStore(population.state_directory)
    baseline = identity_digest(population.baseline_identities)
    new_before = simulator.new_identities
    rows = []
    try:
        for cycle in range(cycles):
            manager = make_manager(backend, processes=processes, contexts_per_process=5)
            pids_before = main_pids(backend)
            launch_started = time.perf_counter()
            await manager.start()
            launch = time.perf_counter() - launch_started
            pids = main_pids(backend) - pids_before
            recorder = RecordingRestorer(
                make_restorer(manager, repository, state_store, simulator, backend)
            )
            gate = asyncio.Semaphore(processes)

            async def restore(
                session_id: str,
                gate: asyncio.Semaphore = gate,
                recorder: RecordingRestorer = recorder,
            ) -> None:
                async with gate:
                    session = await repository.get(session_id)
                    assert session is not None
                    await recorder.restore(session)

            restore_started = time.perf_counter()
            await asyncio.gather(*(restore(sid) for sid in population.session_ids))
            restore_seconds = time.perf_counter() - restore_started
            contexts_after = manager.active_context_count
            await manager.shutdown()
            await until(processes_gone(backend, pids), timeout=10)
            summary = recorder.summary()
            rows.append(
                {
                    "cycle": cycle + 1,
                    "launch_seconds": round(launch, 3),
                    "new_main_processes": len(pids),
                    "restore_all_seconds": round(restore_seconds, 3),
                    "restoration": summary,
                    "contexts_after": contexts_after,
                    "main_processes_after_shutdown": len(main_pids(backend) & pids),
                }
            )
            label = f"cycle_{cycle + 1}"
            result.check(f"{label}_launched_configured_processes", len(pids) == processes)
            result.check(
                f"{label}_every_expected_queue_id_verified",
                summary["successes"] == len(population.session_ids)
                and summary["identity_mismatches"] == 0,
            )
            result.check(f"{label}_zero_contexts_after", contexts_after == 0)
            result.check(
                f"{label}_processes_closed", not (main_pids(backend) & pids)
            )
        result.check(
            "queue_ids_unchanged",
            identity_digest(identity_map(population.database)) == baseline,
        )
        result.check("no_new_identities", simulator.new_identities == new_before)
        result.measurements["cycles"] = rows
    finally:
        await repository.close()
    return result.finish()


async def scenario_application_restart(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
    processes: int,
    workers: int,
) -> ScenarioResult:
    """Stop runtime, repository, and browser with stranded leases; restart and recover."""

    result = ScenarioResult(
        key=f"application_restart_{backend.value}",
        title="Application restart with stranded leases and foreign-backend rows",
        evidence="repository/state/browser fully stopped; new objects on the same files",
    )
    other = (
        BrowserBackendName.CHROME
        if backend is BrowserBackendName.CAMOUFOX
        else BrowserBackendName.CAMOUFOX
    )
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    state_store = FileSystemStateStore(population.state_directory)
    # A foreign-backend row with its own storage state: it must never be loaded.
    foreign_id = f"foreign-{other.value}"
    foreign_queue = f"foreign-{other.value}-queue"
    simulator.stages.setdefault(foreign_queue, "active")
    await repository.create(
        QueueSession(
            session_id=foreign_id,
            queue_id=foreign_queue,
            transfer_url=simulator.transfer_url(foreign_queue),
            mode=SessionMode.HYBRID,
            browser_backend=other,
            status=QueueStatus.ACTIVE_QUEUE,
            state_path=state_store.path_for(foreign_id),
        )
    )
    await state_store.save(
        foreign_id,
        {
            "cookies": [
                {
                    "name": "queue_id",
                    "value": foreign_queue,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        },
    )
    foreign_state_hash = hashlib.sha256(state_store.path_for(foreign_id).read_bytes()).hexdigest()
    # Strand leases as if the previous process died mid-claim.
    now = datetime.now(UTC)
    mark_all_due(population.database, population.session_ids[:5])
    stranded = await repository.claim_due_sessions(
        worker_id="crashed-previous-process",
        now=now,
        lease_until=now + timedelta(seconds=1),
        limit=5,
    )
    before = identity_map(population.database)
    new_before = simulator.new_identities
    await repository.close()
    await asyncio.sleep(1.2)

    started = time.perf_counter()
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    state_store = FileSystemStateStore(population.state_directory)
    summary = await repository.recovery_summary(now=datetime.now(UTC))
    metrics = PrometheusMetrics()
    manager = make_manager(
        backend, processes=processes, contexts_per_process=workers, metrics=metrics
    )
    try:
        await manager.start()
        startup = time.perf_counter() - started
        recorder = RecordingRestorer(
            make_restorer(manager, repository, state_store, simulator, backend, metrics)
        )
        scheduler = make_scheduler(
            repository, recorder, workers=workers, metrics=metrics, scheduler_id="restarted"
        )
        mark_all_due(population.database)
        sweep = await run_sweep(
            population.database, repository, scheduler, manager, timeout_seconds=300
        )
        restored = recorder.summary()
        foreign = await repository.get(foreign_id)
        after = identity_map(population.database)
        failures = restored["failures"]
        assert isinstance(failures, dict)
        result.check("stranded_leases_detected_as_expired", summary.expired_leases == len(stranded))
        result.check(
            "stranded_leases_recovered_by_scheduler",
            scheduler.metrics.expired_leases_recovered >= len(stranded),
        )
        result.check("sweep_completed", bool(sweep["completed"]))
        result.check(
            "every_same_backend_session_verified",
            restored["successes"] == len(population.session_ids)
            and restored["identity_mismatches"] == 0,
        )
        result.check("persisted_queue_ids_unchanged", after == before)
        result.check("no_accidental_replacement_identities", simulator.new_identities == new_before)
        result.check(
            "foreign_backend_row_rejected_before_browser",
            failures.get(RestoreFailure.BACKEND_MISMATCH.value, 0) >= 1,
        )
        result.check(
            "foreign_backend_row_keeps_queue_id_and_provenance",
            foreign is not None
            and foreign.queue_id == foreign_queue
            and foreign.browser_backend is other,
        )
        result.check(
            "foreign_backend_state_never_loaded_or_rewritten",
            hashlib.sha256(state_store.path_for(foreign_id).read_bytes()).hexdigest()
            == foreign_state_hash,
        )
        result.check(
            "zero_leases_and_contexts_after",
            sweep["leases_after"] == 0 and sweep["contexts_after"] == 0,
        )
        # The reverse direction: this backend's rows are rejected by the other backend.
        reverse_manager = make_manager(other, processes=1, contexts_per_process=1)
        reverse = QueueSessionRestorer(
            browser_manager=reverse_manager,
            repository=repository,
            state_store=state_store,
            browser_backend=other,
        )
        victim_id = population.session_ids[0]
        victim_state = hashlib.sha256(state_store.path_for(victim_id).read_bytes()).hexdigest()
        victim = await repository.get(victim_id)
        assert victim is not None
        reversed_result = await reverse.restore(victim)
        victim_after = await repository.get(victim_id)
        result.check(
            "reverse_backend_restore_rejected_without_browser_launch",
            reversed_result.failure is RestoreFailure.BACKEND_MISMATCH
            and not reverse_manager.started,
        )
        result.check(
            "reverse_backend_state_not_touched_and_queue_id_kept",
            hashlib.sha256(state_store.path_for(victim_id).read_bytes()).hexdigest()
            == victim_state
            and victim_after is not None
            and victim_after.queue_id == victim.queue_id,
        )
        result.measurements.update(
            {
                "startup_to_browser_ready_seconds": round(startup, 3),
                "expired_leases_at_restart": summary.expired_leases,
                "valid_queue_ids_at_restart": summary.valid_queue_ids,
                "sweep": sweep,
                "restoration": restored,
            }
        )
        # Clean the foreign row so later scenarios see only this backend's population.
        await repository.delete_unowned_session(foreign_id)
        await state_store.delete(foreign_id)
    finally:
        await manager.shutdown()
        await repository.close()
    return result.finish()


# --- 7-10: browser process failure -----------------------------------------------------


async def _verify_one_restore(
    manager: BrowserManager,
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    backend: BrowserBackendName,
    session_id: str,
) -> bool:
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    try:
        state_store = FileSystemStateStore(population.state_directory)
        restorer = make_restorer(manager, repository, state_store, simulator, backend)
        session = await repository.get(session_id)
        assert session is not None
        restored = await restorer.restore(session)
        persisted = await repository.get(session_id)
        return bool(
            restored.success
            and restored.identity_match is True
            and persisted is not None
            and persisted.queue_id == population.baseline_identities[session_id]
        )
    finally:
        await repository.close()


async def _open_live_contexts(
    manager: BrowserManager, simulator: LocalQueueSimulator, count: int
) -> list[Any]:
    contexts = []
    for index in range(count):
        owned = await manager.create_context()
        page = await owned.context.new_page()
        await page.goto(
            simulator.transfer_url(f"live-{index}"),
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        contexts.append(owned)
    return contexts


async def scenario_process_failure(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
) -> ScenarioResult:
    """Kill a managed browser with zero, one, and multiple active contexts."""

    result = ScenarioResult(
        key=f"browser_process_failure_{backend.value}",
        title="SIGKILL one managed browser process with 0 / 1 / many active contexts",
        evidence="real SIGKILL of the top-level browser process",
    )
    rows = []
    for label, active, serialize in (
        ("zero_active", 0, True),
        ("one_active", 1, True),
        # One process holding several contexts is only possible without serialization.
        ("multiple_active_unserialized", 3, False),
    ):
        metrics = PrometheusMetrics()
        shared = BrowserContextCapacity(5)
        manager = make_manager(
            backend,
            processes=1,
            contexts_per_process=5,
            max_active=5,
            metrics=metrics,
            serialize=serialize,
            shared_capacity=shared,
        )
        before = main_pids(backend)
        await manager.start()
        pids = main_pids(backend) - before
        contexts: list[Any] = []
        try:
            contexts = await _open_live_contexts(manager, simulator, active)
            victim = next(iter(pids))
            killed_at = time.perf_counter()
            kill_pid(victim)
            detected = await until(slot_disconnected(manager), timeout=10)
            await manager.capacity()  # runtime callers repair failed slots
            recovered = time.perf_counter() - killed_at
            capacity = await manager.capacity()
            after = main_pids(backend) - before
            lost = int(metric_total(metrics, "browser_contexts_lost_total"))
            restored_ok = await _verify_one_restore(
                manager, simulator, population, backend, population.session_ids[0]
            )
            row = {
                "case": label,
                "serialized_contexts": serialize,
                "active_contexts_at_kill": active,
                "disconnect_detected_seconds": detected,
                "kill_to_replaced_seconds": round(recovered, 3),
                "restart_seconds": [round(value, 3) for value in manager.restart_durations],
                "lost_contexts_counted": lost,
                "active_contexts_after": capacity.active_contexts,
                "shared_capacity_active_after": shared.active,
                "connected_processes_after": capacity.connected_processes,
                "managed_main_processes_after": len(after),
                "restore_after_recovery_verified": restored_ok,
            }
            rows.append(row)
            result.check(f"{label}_disconnect_detected", detected is not None)
            result.check(f"{label}_lost_contexts_accounted", lost == active)
            result.check(
                f"{label}_capacity_released",
                all(context.closed for context in contexts) and shared.active == 0,
            )
            result.check(
                f"{label}_failed_slot_replaced",
                manager.restart_count == 1 and capacity.connected_processes == 1,
            )
            result.check(
                f"{label}_process_count_not_multiplied",
                len(after) == 1 and victim not in after,
            )
            result.check(f"{label}_queue_id_restores_after_recovery", restored_ok)
        finally:
            await manager.shutdown()
            await until(new_processes_gone(backend, before), timeout=10)
    result.check(
        "persisted_queue_ids_unchanged",
        identity_map(population.database) == population.baseline_identities,
    )
    result.measurements["cases"] = rows
    return result.finish()


async def scenario_multi_slot_failure(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
) -> ScenarioResult:
    result = ScenarioResult(
        key=f"multi_slot_failure_{backend.value}",
        title="Two managed processes: kill one, the healthy one keeps serving",
        evidence="real SIGKILL of one of two top-level browser processes",
    )
    metrics = PrometheusMetrics()
    manager = make_manager(backend, processes=2, contexts_per_process=1, metrics=metrics)
    before = main_pids(backend)
    await manager.start()
    pids = main_pids(backend) - before
    try:
        contexts = await _open_live_contexts(manager, simulator, 2)
        slot_browsers = [slot.browser for slot in manager._slots]
        victim = min(pids)
        kill_pid(victim)
        await until(
            lambda: any(not slot.browser.is_connected() for slot in manager._slots),
            timeout=10,
        )
        failed = [
            index for index, slot in enumerate(manager._slots) if not slot.browser.is_connected()
        ]
        healthy_index = 1 - failed[0] if len(failed) == 1 else None
        healthy_ok = False
        if healthy_index is not None:
            survivor = next(
                context for context in contexts if context.browser_id == healthy_index
            )
            page = await survivor.context.new_page()
            response = await page.goto(
                simulator.transfer_url("healthy-slot"),
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            healthy_ok = response is not None and response.status == 200
        await manager.capacity()
        after = main_pids(backend) - before
        replaced = [
            index
            for index, slot in enumerate(manager._slots)
            if slot.browser is not slot_browsers[index]
        ]
        result.check("exactly_one_slot_failed", len(failed) == 1)
        result.check("healthy_slot_remains_usable_during_failure", healthy_ok)
        result.check("only_failed_slot_replaced", replaced == failed)
        result.check(
            "healthy_process_survives_and_count_stays_bounded",
            len(after) == 2 and (pids - {victim}) <= after and victim not in after,
        )
        result.check(
            "lost_contexts_only_from_failed_slot",
            int(metric_total(metrics, "browser_contexts_lost_total")) == 1,
        )
        for context in contexts:
            await context.close()
        result.check(
            "queue_id_restores_after_recovery",
            await _verify_one_restore(
                manager, simulator, population, backend, population.session_ids[1]
            ),
        )
        result.measurements.update(
            {
                "restart_seconds": [round(value, 3) for value in manager.restart_durations],
                "managed_main_processes_after": len(after),
            }
        )
    finally:
        await manager.shutdown()
        await until(lambda: not (main_pids(backend) - before), timeout=10)
    return result.finish()


async def scenario_repeated_failure(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
    cycles: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key=f"repeated_failure_{backend.value}",
        title=f"Kill and recover one managed browser slot {cycles} times",
        evidence="repeated real SIGKILL of the top-level browser process",
    )
    metrics = PrometheusMetrics()
    manager = make_manager(backend, processes=1, contexts_per_process=2, metrics=metrics)
    before = main_pids(backend)
    await manager.start()
    process_counts: list[int] = []
    context_leaks = 0
    verified = 0
    try:
        for cycle in range(cycles):
            live = await _open_live_contexts(manager, simulator, 1)
            current = main_pids(backend) - before
            for pid in current:
                kill_pid(pid)
            await until(lambda: not manager._slots[0].browser.is_connected(), timeout=10)
            await manager.capacity()
            await until(lambda: len(main_pids(backend) - before) == 1, timeout=10)
            process_counts.append(len(main_pids(backend) - before))
            context_leaks += manager.active_context_count + int(not live[0].closed)
            if cycle % 3 == 2 or cycle == cycles - 1:
                session_id = population.session_ids[cycle % len(population.session_ids)]
                verified += int(
                    await _verify_one_restore(manager, simulator, population, backend, session_id)
                )
        durations = list(manager.restart_durations)
        restart_failures = int(metric_total(metrics, "browser_restart_failures_total"))
        result.check("every_kill_recovered", manager.restart_count == cycles)
        result.check("zero_restart_failures", restart_failures == 0)
        result.check("no_process_leaks", all(count == 1 for count in process_counts))
        result.check("no_context_leaks", context_leaks == 0)
        result.check(
            "sampled_restores_verified_expected_queue_id",
            verified == len([c for c in range(cycles) if c % 3 == 2 or c == cycles - 1]),
        )
        result.check(
            "queue_id_changes_zero",
            identity_map(population.database) == population.baseline_identities,
        )
        result.measurements.update(
            {
                "cycles": cycles,
                "restart_seconds": stats(durations),
                "restart_failures": restart_failures,
                "lost_contexts": int(metric_total(metrics, "browser_contexts_lost_total")),
                "managed_main_processes_per_cycle": dict(Counter(process_counts)),
            }
        )
    finally:
        await manager.shutdown()
        await until(lambda: not (main_pids(backend) - before), timeout=10)
    result.check("zero_processes_after_shutdown", not (main_pids(backend) - before))
    return result.finish()


async def scenario_failure_during_monitoring(
    simulator: LocalQueueSimulator,
    population: PopulationContext,
    *,
    backend: BrowserBackendName,
    processes: int,
    workers: int,
) -> ScenarioResult:
    result = ScenarioResult(
        key=f"failure_during_monitoring_{backend.value}",
        title="SIGKILL a managed browser while restores/checks are in flight",
        evidence="slowed local pages keep contexts live; real SIGKILL mid-check",
    )
    metrics = PrometheusMetrics()
    manager = make_manager(
        backend, processes=processes, contexts_per_process=workers, metrics=metrics
    )
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    state_store = FileSystemStateStore(population.state_directory)
    queue_ids = [qid for qid in population.baseline_identities.values() if qid is not None]
    simulator.slow_seconds = 1.0
    simulator.slow_ids.update(queue_ids)
    new_before = simulator.new_identities
    kills: list[float] = []
    before = main_pids(backend)
    try:
        await manager.start()
        recorder = RecordingRestorer(
            make_restorer(manager, repository, state_store, simulator, backend, metrics)
        )
        scheduler = make_scheduler(
            repository, recorder, workers=workers, metrics=metrics, scheduler_id="kill-mid-check"
        )
        mark_all_due(population.database)

        async def on_tick(elapsed: float) -> None:
            if len(kills) < 2 and manager.active_context_count > 0 and elapsed > 1.5 * (
                len(kills) + 1
            ):
                pids = sorted(main_pids(backend) - before)
                if pids:
                    kill_pid(pids[0])
                    kills.append(round(elapsed, 3))

        sweep = await run_sweep(
            population.database,
            repository,
            scheduler,
            manager,
            timeout_seconds=300,
            on_tick=on_tick,
        )
        # Retries exhausted by the kill leave rows CONNECTION_LOST; they must be retryable.
        simulator.slow_ids.difference_update(queue_ids)
        sessions = await repository.list()
        retry_ids = [s.session_id for s in sessions if s.status is QueueStatus.CONNECTION_LOST]
        retry_sweep: dict[str, object] = {}
        if retry_ids:
            mark_all_due(population.database, retry_ids)
            retry_scheduler = make_scheduler(
                repository, recorder, workers=workers, metrics=metrics, scheduler_id="retry"
            )
            retry_sweep = await run_sweep(
                population.database, repository, retry_scheduler, manager, timeout_seconds=120
            )
        final_sessions = await repository.list()
        restored = recorder.summary()
        latency = restored["latency_seconds"]
        assert isinstance(latency, dict)
        deadline = recorder.restorer._attempt_timeout_seconds
        result.check("browser_killed_during_checks", len(kills) >= 1)
        result.check("sweep_completed", bool(sweep["completed"]))
        result.check(
            "every_restore_attempt_ended_within_deadline",
            (latency["max"] or 0) <= deadline + 1.0,
        )
        result.check("killed_slot_replaced", manager.restart_count >= len(kills))
        result.check(
            "leases_released_after_failure",
            owned_rows(population.database)["automatic_leases"] == 0,
        )
        result.check(
            "transient_failures_left_retryable_not_failed",
            not any(s.status is QueueStatus.FAILED for s in final_sessions),
        )
        result.check(
            "retry_after_failure_verifies_identity",
            not retry_ids or bool(retry_sweep.get("completed")),
        )
        result.check(
            "expected_queue_ids_unchanged",
            identity_map(population.database) == population.baseline_identities,
        )
        result.check("no_replacement_identity_created", simulator.new_identities == new_before)
        result.check("zero_contexts_after", manager.active_context_count == 0)
        result.measurements.update(
            {
                "kills_at_seconds": kills,
                "restore_attempt_deadline_seconds": round(deadline, 3),
                "sweep": sweep,
                "restoration": restored,
                "sessions_retried_after_kill": len(retry_ids),
                "retry_sweep": retry_sweep,
                "browser_restarts": manager.restart_count,
                "lost_contexts": int(metric_total(metrics, "browser_contexts_lost_total")),
            }
        )
    finally:
        simulator.slow_ids.difference_update(queue_ids)
        await manager.shutdown()
        await repository.close()
    return result.finish()


# --- 11: restoration failure cases -----------------------------------------------------


async def scenario_restoration_failures(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backend: BrowserBackendName,
) -> ScenarioResult:
    result = ScenarioResult(
        key=f"restoration_failures_{backend.value}",
        title="Transfer, state, provenance, navigation, and identity failures",
        evidence="dedicated synthetic sessions; faults injected by the local simulator",
    )
    database = directory / f"restore-failures-{backend.value}.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    state_store = FileSystemStateStore(directory / f"restore-failures-{backend.value}-state")
    metrics = PrometheusMetrics()
    manager = make_manager(backend, processes=1, contexts_per_process=2, metrics=metrics)
    rows: dict[str, dict[str, object]] = {}
    try:
        await manager.start()
        await create_population(
            manager=manager,
            repository=repository,
            state_store=state_store,
            simulator=simulator,
            backend=backend,
            count=6,
            concurrency=1,
            prefix=f"fault-{backend.value}",
        )
        identities = identity_map(database)
        ids = sorted(identities)
        restorer = make_restorer(manager, repository, state_store, simulator, backend)
        new_before = simulator.new_identities

        async def run(label: str, session_id: str) -> SessionRestoreResult:
            session = await repository.get(session_id)
            assert session is not None
            outcome = await restorer.restore(session)
            persisted = await repository.get(session_id)
            rows[label] = {
                "success": outcome.success,
                "method": outcome.method.value,
                "failure": outcome.failure.value if outcome.failure else None,
                "attempts": [
                    {
                        "method": attempt.method.value,
                        "failure": attempt.failure.value if attempt.failure else None,
                    }
                    for attempt in outcome.attempts
                ],
                "expected_queue_id_preserved": persisted is not None
                and persisted.queue_id == identities[session_id],
            }
            result.check(
                f"{label}_expected_queue_id_preserved",
                bool(rows[label]["expected_queue_id_preserved"]),
            )
            return outcome

        transfer_down, missing, corrupt, foreign, navigation, mismatch = ids
        simulator.transfer_down_ids.add(str(identities[transfer_down]))
        outcome = await run("transfer_failure_hybrid_storage_fallback", transfer_down)
        result.check(
            "transfer_failure_falls_back_to_same_backend_storage_state",
            outcome.success and outcome.method.value == "STORAGE_STATE",
        )

        simulator.transfer_down_ids.add(str(identities[missing]))
        await state_store.delete(missing)
        outcome = await run("missing_storage_state", missing)
        result.check(
            "missing_state_fails_without_new_identity",
            not outcome.success and outcome.failure is RestoreFailure.STATE_MISSING,
        )

        simulator.transfer_down_ids.add(str(identities[corrupt]))
        state_store.path_for(corrupt).write_text("{not valid json", encoding="utf-8")
        outcome = await run("corrupt_storage_state", corrupt)
        result.check(
            "corrupt_state_fails_as_corrupt",
            not outcome.success and outcome.failure is RestoreFailure.STATE_CORRUPT,
        )

        other = (
            BrowserBackendName.CHROME
            if backend is BrowserBackendName.CAMOUFOX
            else BrowserBackendName.CAMOUFOX
        )
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE queue_sessions SET browser_backend = ? WHERE session_id = ?",
                (other.value, foreign),
            )
        contexts_before = histogram_count(metrics, "browser_context_creation_duration_seconds")
        outcome = await run("backend_provenance_mismatch", foreign)
        result.check(
            "provenance_mismatch_rejected_before_any_context",
            outcome.failure is RestoreFailure.BACKEND_MISMATCH
            and histogram_count(metrics, "browser_context_creation_duration_seconds")
            == contexts_before,
        )

        simulator.empty_response_ids.add(str(identities[navigation]))
        outcome = await run("navigation_failure", navigation)
        result.check(
            "navigation_failure_is_transient_failure",
            not outcome.success
            and outcome.attempts[0].failure is RestoreFailure.NAVIGATION_FAILED
            and outcome.failure
            in {RestoreFailure.NAVIGATION_FAILED, RestoreFailure.STATE_CONTEXT_FAILED},
        )

        simulator.mismatch_ids.add(str(identities[mismatch]))
        outcome = await run("identity_mismatch", mismatch)
        result.check(
            "identity_mismatch_detected",
            not outcome.success and outcome.failure is RestoreFailure.IDENTITY_MISMATCH,
        )
        result.check("no_new_identities_created", simulator.new_identities == new_before)
        result.check("zero_contexts_after", manager.active_context_count == 0)
        result.measurements["cases"] = rows
    finally:
        for queue_id in identity_map(database).values():
            if queue_id is not None:
                simulator.transfer_down_ids.discard(queue_id)
                simulator.empty_response_ids.discard(queue_id)
                simulator.mismatch_ids.discard(queue_id)
        await manager.shutdown()
        await repository.close()
    return result.finish()


# --- 12-14: headed/manual failure, pause under failure, shutdown ----------------------


class _AppHarness:
    """The real operator runtime (no HTTP layer) over one persisted run."""

    def __init__(
        self,
        directory: Path,
        simulator: LocalQueueSimulator,
        *,
        backend: BrowserBackendName,
        headed: bool,
        requested: int,
    ) -> None:
        self.database = directory / "app.sqlite3"
        base = workflow_settings(directory, database=self.database, backend=backend)
        self.settings: Settings = base.model_copy(
            update={
                "creation_headless": True,
                "max_manual_open_sessions": 2,
                "manual_open_lease_seconds": 6,
                "operator_workers": 2,
                "monitor_workers": 2,
            }
        )
        self.simulator = simulator
        self.backend = backend
        self.headed = headed
        self.requested = requested
        self.repository: SQLiteSessionRepository | None = None
        self.runtime: ApplicationRunRuntime | None = None

    async def start(self, *, create: bool) -> ApplicationRunRuntime:
        self.repository = SQLiteSessionRepository(self.database)
        await self.repository.initialize()
        if create:
            run = await self.repository.create_run(
                RunConfig(
                    run_id=str(uuid4()),
                    target_url=self.simulator.entry_url,
                    requested_sessions=self.requested,
                    created_at=datetime.now(UTC),
                    browser_backend=self.backend,
                )
            )
        else:
            active = await self.repository.get_active_run()
            assert active is not None
            run = active
        self.runtime = ApplicationRunRuntime(
            settings=self.settings,
            repository=self.repository,
            manual_headless=not self.headed,
        )
        await self.runtime.start_run(run)
        return self.runtime

    async def stop(self) -> float:
        started = time.perf_counter()
        if self.runtime is not None:
            await self.runtime.close()
        if self.repository is not None:
            await self.repository.close()
        self.runtime = None
        self.repository = None
        return round(time.perf_counter() - started, 3)

    def valid(self) -> int:
        with sqlite3.connect(self.database) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(DISTINCT queue_id) FROM queue_sessions "
                    "WHERE queue_id IS NOT NULL AND status != 'FAILED'"
                ).fetchone()[0]
            )

    def row(self, session_id: str) -> dict[str, Any]:
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            found = connection.execute(
                "SELECT * FROM queue_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(found) if found is not None else {}

    def checked(self) -> dict[str, str | None]:
        with sqlite3.connect(self.database) as connection:
            return dict(
                connection.execute("SELECT session_id, last_checked_at FROM queue_sessions")
            )


async def open_when_free(
    runtime: ApplicationRunRuntime, session_id: str, *, timeout: float = 20.0
) -> ManualOpenResult:
    """Open a row, retrying while automatic monitoring legitimately holds its lease."""

    deadline = time.perf_counter() + timeout
    while True:
        try:
            return await runtime.open_session(session_id)
        except ManualOpenError as exc:
            if "being checked" not in str(exc) or time.perf_counter() > deadline:
                raise
            await asyncio.sleep(0.2)


async def _await_action(runtime: ApplicationRunRuntime, session_id: str, timeout: float) -> str:
    async def finished() -> bool:
        action = runtime.session_action(session_id)
        return action is not None and action.status in {
            OperatorActionStatus.SUCCESS,
            OperatorActionStatus.FAILED,
        }

    await until(finished, timeout=timeout, interval=0.1)
    action = runtime.session_action(session_id)
    return action.status.value if action is not None else "MISSING"


async def scenario_manual_and_shutdown(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backend: BrowserBackendName,
    headed: bool,
    manual_churn_cycles: int = 10,
) -> list[ScenarioResult]:
    """Headed/manual failure (12), pause under failure (13), and shutdown (14)."""

    manual = ScenarioResult(
        key=f"manual_failure_{backend.value}",
        title="Headed Open: Close, window close, process kill, app shutdown, reopen",
        evidence=(
            "real operator runtime; "
            + ("visible headed" if headed else "headless-mode")
            + " manual browser pool"
        ),
    )
    pause = ScenarioResult(
        key=f"pause_under_failure_{backend.value}",
        title="Paused monitoring with a killed automatic browser",
        evidence="real operator runtime; SIGKILL of the automatic browser process",
    )
    shutdown = ScenarioResult(
        key=f"shutdown_under_load_{backend.value}",
        title="Shutdown with automatic, queued/active operator, and headed work",
        evidence="real operator runtime; slowed local pages keep work in flight",
    )
    app = _AppHarness(directory, simulator, backend=backend, headed=headed, requested=6)
    baseline_pids = main_pids(backend)
    runtime = await app.start(create=True)
    try:
        acquired = await until(lambda: app.valid() >= app.requested, timeout=120, interval=0.25)
        manual.check("acquisition_reached_target", acquired is not None)
        identities = identity_map(app.database)
        session_ids = sorted(identities)
        first, second, third, fourth, fifth, sixth = session_ids[:6]
        new_after_acquire = simulator.new_identities
        await until(lambda: app.row(first).get("worker_id") is None, timeout=15)
        automatic_pids = main_pids(backend) - baseline_pids

        # (12a) explicit Close
        _step(backend, "12a explicit Close")
        before_open = main_pids(backend)
        opened = await open_when_free(runtime, first)
        headed_pids = main_pids(backend) - before_open
        manual.check(
            "open_restores_persisted_identity",
            opened.message == "Opened in browser"
            and app.row(first).get("manual_owner_id") is not None
            and app.row(first).get("queue_id") == identities[first],
        )
        await runtime.close_session(first)
        manual.check(
            "explicit_close_releases_ownership",
            app.row(first).get("manual_owner_id") is None
            and app.row(first).get("queue_id") == identities[first],
        )

        # (12b) page/window close
        _step(backend, "12b page/window close")
        assert runtime._manual_sessions is not None
        opened = await open_when_free(runtime, first)
        record = runtime._manual_sessions._open.get(first)
        if record is not None:
            await record.page.close()
        released = await until(lambda: app.row(first).get("manual_owner_id") is None, timeout=15)
        manual.check(
            "window_close_releases_ownership",
            opened.message == "Opened in browser" and released is not None,
        )

        # (12c) browser process kill
        _step(backend, "12c browser process kill")
        before_open = main_pids(backend)
        opened = await open_when_free(runtime, first)
        headed_pids |= main_pids(backend) - before_open
        # Kill the whole headed pool: the window's process is one of its slots.
        live_headed = sorted(main_pids(backend) - automatic_pids - baseline_pids)
        killed = bool([pid for pid in live_headed if kill_pid(pid)])
        manual.measurements["headed_pool_processes_at_kill"] = len(live_headed)
        released_kill = await until(
            lambda: app.row(first).get("manual_owner_id") is None
            and runtime._manual_sessions is not None
            and runtime._manual_sessions.open_count == 0,
            timeout=20,
        )
        row = app.row(first)
        manual.check(
            "process_kill_releases_ownership",
            opened.message == "Opened in browser" and killed and released_kill is not None,
        )
        manual.check(
            "process_kill_keeps_queue_id_and_retryable_status",
            row.get("queue_id") == identities[first] and row.get("status") != "FAILED",
        )
        reopened = await open_when_free(runtime, first)
        manual.check(
            "next_open_after_kill_reconstructs_journey",
            reopened.message == "Opened in browser"
            and app.row(first).get("queue_id") == identities[first],
        )

        # (12d) application shutdown with the headed window open
        _step(backend, "12d application shutdown with the headed win")
        shutdown_with_open = await app.stop()
        manual.check(
            "shutdown_with_open_window_releases_everything",
            owned_rows(app.database) == {"automatic_leases": 0, "manual_owners": 0}
            and not (main_pids(backend) - baseline_pids),
        )
        runtime = await app.start(create=False)
        await until(lambda: bool(main_pids(backend) - baseline_pids), timeout=20)
        await until(lambda: app.row(first).get("worker_id") is None, timeout=15)
        automatic_pids = main_pids(backend) - baseline_pids
        reopened = await open_when_free(runtime, first)
        manual.check(
            "open_after_restart_reconstructs_same_queue_id",
            reopened.message == "Opened in browser"
            and app.row(first).get("queue_id") == identities[first],
        )
        await runtime.close_session(first)
        # The manual pool is not context-serialized: two windows opened concurrently.
        pair = await asyncio.gather(
            open_when_free(runtime, second), open_when_free(runtime, third)
        )
        manual.check(
            "two_concurrent_manual_opens_both_verified",
            all(item.message == "Opened in browser" for item in pair)
            and app.row(second).get("queue_id") == identities[second]
            and app.row(third).get("queue_id") == identities[third],
        )
        await asyncio.gather(runtime.close_session(second), runtime.close_session(third))
        manual.check(
            "two_concurrent_manual_closes_release_ownership",
            app.row(second).get("manual_owner_id") is None
            and app.row(third).get("manual_owner_id") is None,
        )
        # (12e) manual-pool churn: repeated concurrent open/close of two windows
        _step(backend, "12e manual pool churn")
        churn = await _manual_churn(runtime, app, second, third, cycles=manual_churn_cycles)
        manual.measurements["manual_pool_churn"] = churn
        manual.check(
            "manual_pool_churn_opens_verified_and_closes_bounded",
            churn["open_failures"] == 0
            and churn["stalled_closes"] == 0
            and app.row(second).get("queue_id") == identities[second]
            and app.row(third).get("queue_id") == identities[third],
        )
        manual.check(
            "manual_paths_created_no_identities",
            simulator.new_identities == new_after_acquire,
        )
        manual.measurements["shutdown_with_open_window_seconds"] = shutdown_with_open
        manual.check(
            "queue_ids_unchanged", identity_map(app.database) == identities
        )

        # (13) pause under failure
        _step(backend, "13 pause under failure")
        await runtime.pause_monitoring()
        await asyncio.sleep(1.0)  # let in-flight checks finish
        scheduler = runtime._monitoring_scheduler
        assert scheduler is not None
        claimed_before = scheduler.metrics.claimed
        checked_before = app.checked()
        automatic_killed = [pid for pid in sorted(automatic_pids) if kill_pid(pid)]
        pause.check("automatic_browser_killed_while_paused", bool(automatic_killed))
        await asyncio.sleep(5.0)  # longer than the 2s poll interval of every session
        pause.check("no_new_automatic_claims_while_paused", scheduler.metrics.claimed == claimed_before)
        pause.check(
            "no_rows_checked_while_paused",
            {sid: at for sid, at in app.checked().items() if sid != second}
            == {sid: at for sid, at in checked_before.items() if sid != second},
        )
        simulator.progress[str(identities[second])] = 64
        await runtime.request_action(OperatorActionKind.REFRESH, second)
        refresh_status = await _await_action(runtime, second, timeout=30)
        pause.check(
            "refresh_now_works_while_paused_after_browser_kill",
            refresh_status == OperatorActionStatus.SUCCESS.value
            and app.row(second).get("queue_id") == identities[second],
        )
        opened = await open_when_free(runtime, third)
        pause.check("manual_open_works_while_paused", opened.message == "Opened in browser")
        await runtime.close_session(third)
        await runtime.resume_monitoring()
        resumed_from = app.checked()
        resumed = await until(
            lambda: all(
                app.checked().get(sid) != resumed_from.get(sid) for sid in session_ids
            ),
            timeout=40,
            interval=0.25,
        )
        pause.check("resume_continues_due_work", resumed is not None)
        pause.check("queue_ids_unchanged", identity_map(app.database) == identities)
        pause.measurements["resume_to_all_checked_seconds"] = resumed

        # (14) shutdown with automatic work active, operator work queued/active, headed open
        _step(backend, "14 shutdown with automatic work active, ope")
        opened = await open_when_free(runtime, fourth)
        shutdown.check("headed_session_open_before_shutdown", opened.message == "Opened in browser")
        simulator.slow_seconds = 3.0
        simulator.slow_ids.update(str(qid) for qid in identities.values())
        mark_all_due(app.database, [fifth, sixth])
        automatic_busy = await until(lambda: scheduler_or_zero(runtime) > 0, timeout=10)
        for session_id in (first, second, third):
            with contextlib.suppress(Exception):
                await runtime.request_action(OperatorActionKind.REFRESH, session_id)
        operator_busy = await until(lambda: len(runtime_actions(runtime)) >= 3, timeout=10)
        active_actions = runtime_actions(runtime)
        statuses = Counter(action.status.value for action in active_actions)
        shutdown.measurements["in_flight_at_shutdown"] = {
            "automatic_checks": scheduler_or_zero(runtime),
            "operator_actions_running": statuses.get(OperatorActionStatus.RUNNING.value, 0),
            "operator_actions_queued": statuses.get(OperatorActionStatus.REQUESTED.value, 0),
            "manual_open": runtime._manual_sessions.open_count
            if runtime._manual_sessions is not None
            else 0,
        }
        shutdown.check(
            "automatic_and_operator_work_in_flight_before_shutdown",
            automatic_busy is not None
            and operator_busy is not None
            and scheduler_or_zero(runtime) > 0,
        )
        shutdown.check(
            "operator_work_both_running_and_queued",
            statuses.get(OperatorActionStatus.RUNNING.value, 0) >= 1
            and statuses.get(OperatorActionStatus.REQUESTED.value, 0) >= 1,
        )
        duration = await app.stop()
        await until(lambda: not (main_pids(backend) - baseline_pids), timeout=10)
        shutdown.check(
            "shutdown_bounded",
            duration <= app.settings.shutdown_timeout_seconds * 3 + 15,
        )
        shutdown.check(
            "zero_automatic_leases_and_manual_owners",
            owned_rows(app.database) == {"automatic_leases": 0, "manual_owners": 0},
        )
        shutdown.check(
            "zero_unintended_browser_processes", not (main_pids(backend) - baseline_pids)
        )
        shutdown.check("persisted_queue_identities_intact", identity_map(app.database) == identities)
        shutdown.check(
            "shutdown_created_no_identities", simulator.new_identities == new_after_acquire
        )
        shutdown.measurements["shutdown_seconds"] = duration
    finally:
        simulator.slow_ids.clear()
        if app.runtime is not None:
            await app.stop()
    return [manual.finish(), pause.finish(), shutdown.finish()]


async def _manual_churn(
    runtime: ApplicationRunRuntime,
    app: _AppHarness,
    first: str,
    second: str,
    *,
    cycles: int,
) -> dict[str, object]:
    """Open two windows concurrently and close them, ``cycles`` times."""

    open_seconds: list[float] = []
    close_seconds: list[float] = []
    open_failures = 0
    stalled = 0
    for _ in range(cycles):
        began = time.perf_counter()
        opened = await asyncio.gather(
            open_when_free(runtime, first),
            open_when_free(runtime, second),
            return_exceptions=True,
        )
        open_seconds.append(time.perf_counter() - began)
        open_failures += sum(
            not isinstance(item, ManualOpenResult) or item.message != "Opened in browser"
            for item in opened
        )
        began = time.perf_counter()
        await asyncio.gather(runtime.close_session(first), runtime.close_session(second))
        elapsed = time.perf_counter() - began
        close_seconds.append(elapsed)
        stalled += int(elapsed > 10.0)
        await until(
            lambda: app.row(first).get("manual_owner_id") is None
            and app.row(second).get("manual_owner_id") is None,
            timeout=15,
        )
    return {
        "cycles": cycles,
        "open_failures": open_failures,
        "stalled_closes": stalled,
        "concurrent_open_seconds": stats(open_seconds),
        "concurrent_close_seconds": stats(close_seconds),
    }


def _step(backend: BrowserBackendName, label: str) -> None:
    print(f"[step] {backend.value} {label}", flush=True)


def scheduler_or_zero(runtime: ApplicationRunRuntime) -> int:
    scheduler = runtime._monitoring_scheduler
    return scheduler.metrics.currently_checking if scheduler is not None else 0


def runtime_actions(runtime: ApplicationRunRuntime) -> list[OperatorAction]:
    manager = runtime._operator_actions
    if manager is None:
        return []
    return [
        action
        for action in manager._actions.values()
        if action.status in {OperatorActionStatus.REQUESTED, OperatorActionStatus.RUNNING}
    ]


# --- report ------------------------------------------------------------------------------


def environment() -> dict[str, object]:
    def package(name: str) -> str | None:
        try:
            return version(name)
        except Exception:  # noqa: BLE001
            return None

    camoufox_build = None
    with contextlib.suppress(Exception):
        from queue_load_test.browser import CAMOUFOX_BROWSER_VERSION

        camoufox_build = CAMOUFOX_BROWSER_VERSION
    return {
        "host_platform": platform.platform(),
        "host_machine": platform.machine(),
        "host_logical_cpus": os.cpu_count(),
        "host_ram_bytes": int(psutil.virtual_memory().total) if psutil is not None else None,
        "python": sys.version.split()[0],
        "playwright": package("playwright"),
        "camoufox_package": package("camoufox"),
        "camoufox_browser_build": camoufox_build,
    }


async def browser_versions(backends: Sequence[BrowserBackendName]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for backend in backends:
        manager = make_manager(backend, processes=1, contexts_per_process=1)
        try:
            await manager.start()
            versions[backend.value] = manager.backend_diagnostics().browser_version
        except Exception as exc:  # noqa: BLE001
            versions[backend.value] = f"unavailable: {type(exc).__name__}"
        finally:
            await manager.shutdown()
    return versions


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.name
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def scenario_dict(result: ScenarioResult) -> dict[str, object]:
    return asdict(result) | {"outcome": result.outcome.value}


async def run_phase6_benchmark(
    directory: Path,
    *,
    backends: Sequence[BrowserBackendName],
    levels: Sequence[int],
    navigation_waves: int,
    sample_interval_seconds: float,
    population_size: int,
    sweeps: int,
    processes: int,
    workers: int,
    restart_cycles: int,
    failure_cycles: int,
    headed: bool,
    include_capacity: bool = True,
    only: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    simulator = LocalQueueSimulator(new_identity_prefix="sim-phase6")
    await simulator.start()
    with contextlib.suppress(NotImplementedError, RuntimeError):
        # Operator diagnostics for a stalled run: `kill -USR1 <pid>` prints task stacks.
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, _dump_tasks)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "target": "LocalQueueSimulator on 127.0.0.1 (not Queue-it)",
        "staging_status": "NOT RUN",
        "environment": environment(),
        "configuration": {
            "backends": [backend.value for backend in backends],
            "context_levels": list(levels),
            "contexts_per_process_for_capacity": CONTEXTS_PER_PROCESS,
            "navigation_waves": navigation_waves,
            "sample_interval_seconds": sample_interval_seconds,
            "navigation_timeout_ms": NAVIGATION_TIMEOUT_MS,
            "production_profile": {
                "browser_processes": processes,
                "monitor_workers": workers,
                "camoufox_contexts_serialized_per_process": True,
            },
            "population_size": population_size,
            "sweeps": sweeps,
            "full_browser_restart_cycles": restart_cycles,
            "repeated_failure_cycles": failure_cycles,
            "headed_manual_pool": headed,
        },
        "measurement_notes": {
            "cpu": "Process CPU percent where 100 means one logical core.",
            "ram": (
                "Summed RSS of the backend's browser process tree; shared pages may be "
                "counted more than once. Camoufox tree = camoufox + plugin-container "
                "children; Chrome tree = processes named *chrome*."
            ),
            "throughput": (
                "local_simulator_checks_per_second is a local page rate, not Queue-it "
                "throughput or a staging-safe level."
            ),
            "fingerprints": "No fingerprint/browser-device values are read or compared.",
        },
    }
    try:
        report["environment"]["browser_versions"] = await browser_versions(backends)
        if include_capacity:
            report["capacity"] = await run_capacity_matrix(
                simulator,
                directory / "capacity",
                backends=backends,
                levels=levels,
                navigation_waves=navigation_waves,
                sample_interval_seconds=sample_interval_seconds,
            )
        scenarios: list[ScenarioResult] = []
        for backend in backends:
            population = PopulationContext(
                database=directory / f"population-{backend.value}.sqlite3",
                state_directory=directory / f"population-{backend.value}-state",
            )
            steps: list[tuple[str, Callable[[], Awaitable[ScenarioResult]]]] = [
                (
                    "park_reopen",
                    partial(
                        scenario_park_reopen,
                        simulator,
                        population,
                        backend=backend,
                        size=population_size,
                        sweeps=sweeps,
                        processes=processes,
                        workers=workers,
                    ),
                ),
                (
                    "full_browser_restart",
                    partial(
                        scenario_full_browser_restart,
                        simulator,
                        population,
                        backend=backend,
                        processes=processes,
                        cycles=restart_cycles,
                    ),
                ),
                (
                    "application_restart",
                    partial(
                        scenario_application_restart,
                        simulator,
                        population,
                        backend=backend,
                        processes=processes,
                        workers=workers,
                    ),
                ),
                (
                    "process_failure",
                    partial(scenario_process_failure, simulator, population, backend=backend),
                ),
                (
                    "multi_slot_failure",
                    partial(scenario_multi_slot_failure, simulator, population, backend=backend),
                ),
                (
                    "repeated_failure",
                    partial(
                        scenario_repeated_failure,
                        simulator,
                        population,
                        backend=backend,
                        cycles=failure_cycles,
                    ),
                ),
                (
                    "failure_during_monitoring",
                    partial(
                        scenario_failure_during_monitoring,
                        simulator,
                        population,
                        backend=backend,
                        processes=processes,
                        workers=workers,
                    ),
                ),
                (
                    "restoration_failures",
                    partial(scenario_restoration_failures, simulator, directory, backend=backend),
                ),
            ]
            for name, step in steps:
                if only and name not in only:
                    continue
                try:
                    scenario = await step()
                except Exception as exc:  # noqa: BLE001 - record and continue
                    scenario = ScenarioResult(
                        key=f"{name}_{backend.value}",
                        title=name,
                        evidence="scenario raised",
                        outcome=Outcome.FAIL,
                        notes=[_exception_note(exc)],
                    )
                    scenario.checks["completed_without_exception"] = False
                _print_scenario(scenario)
                scenarios.append(scenario)
            app_directory = directory / f"app-{backend.value}"
            app_directory.mkdir(parents=True, exist_ok=True)
            if only and "manual" not in only:
                continue
            try:
                app_results = await scenario_manual_and_shutdown(
                    simulator, app_directory, backend=backend, headed=headed
                )
            except Exception as exc:  # noqa: BLE001
                failed = ScenarioResult(
                    key=f"manual_and_shutdown_{backend.value}",
                    title="manual/pause/shutdown",
                    evidence="scenario raised",
                    outcome=Outcome.FAIL,
                    notes=[_exception_note(exc)],
                )
                failed.checks["completed_without_exception"] = False
                app_results = [failed]
            for scenario in app_results:
                _print_scenario(scenario)
                scenarios.append(scenario)
        report["scenarios"] = [scenario_dict(scenario) for scenario in scenarios]
        report["summary"] = {
            "scenarios_passed": sum(s.outcome is Outcome.PASS for s in scenarios),
            "scenarios_failed": sum(s.outcome is Outcome.FAIL for s in scenarios),
            "checks_passed": sum(sum(s.checks.values()) for s in scenarios),
            "checks_failed": sum(len(s.checks) - sum(s.checks.values()) for s in scenarios),
        }
    finally:
        await simulator.close()
        report["duration_seconds"] = round(time.perf_counter() - started, 1)
        report["leftover_browser_main_processes"] = {
            backend.value: len(browser_main_processes(backend)) for backend in backends
        }
    return report


def _dump_tasks() -> None:
    for task in asyncio.all_tasks():
        print(f"--- task {task.get_name()}", file=sys.stderr)
        task.print_stack(limit=12, file=sys.stderr)
    sys.stderr.flush()


def _exception_note(exc: BaseException) -> str:
    """Exception type, harness line, and message (harness messages carry no identities)."""

    frames = traceback.extract_tb(exc.__traceback__)
    here = [frame for frame in frames if frame.filename == __file__]
    line = here[-1].lineno if here else None
    return f"{type(exc).__name__} at line {line}: {str(exc)[:160]}"


def _print_scenario(scenario: ScenarioResult) -> None:
    failed = [name for name, passed in scenario.checks.items() if not passed]
    print(
        f"[{scenario.outcome.value}] {scenario.key}: "
        f"{sum(scenario.checks.values())}/{len(scenario.checks)} checks"
        + (f" failed={failed}" if failed else "")
        + (f" notes={scenario.notes}" if scenario.notes else ""),
        flush=True,
    )


def _parse_levels(value: str) -> tuple[int, ...]:
    levels = tuple(int(item) for item in value.split(",") if item.strip())
    if not levels or any(level < 1 or level > 100 for level in levels):
        raise argparse.ArgumentTypeError("levels must be integers between 1 and 100")
    return levels


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--levels", type=_parse_levels, default=DEFAULT_CONTEXT_LEVELS)
    parser.add_argument("--no-chrome", action="store_true", help="skip the Chrome comparison")
    parser.add_argument("--no-capacity", action="store_true")
    parser.add_argument("--navigation-waves", type=int, default=3)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.25)
    parser.add_argument("--population", type=int, default=40)
    parser.add_argument("--sweeps", type=int, default=5)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--restart-cycles", type=int, default=3)
    parser.add_argument("--failure-cycles", type=int, default=10)
    parser.add_argument("--headed", action="store_true", help="visible manual browser pool")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated scenario names (debugging); park_reopen is required first",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    backends = [BrowserBackendName.CAMOUFOX]
    if not args.no_chrome:
        backends.append(BrowserBackendName.CHROME)
    with tempfile.TemporaryDirectory(prefix="phase6-camoufox-") as work:
        report = asyncio.run(
            run_phase6_benchmark(
                Path(work),
                backends=backends,
                levels=args.levels,
                navigation_waves=args.navigation_waves,
                sample_interval_seconds=args.sample_interval_seconds,
                population_size=args.population,
                sweeps=args.sweeps,
                processes=args.processes,
                workers=args.workers,
                restart_cycles=args.restart_cycles,
                failure_cycles=args.failure_cycles,
                headed=args.headed,
                include_capacity=not args.no_capacity,
                only=frozenset(item for item in args.only.split(",") if item),
            )
        )
    text = json.dumps(report, indent=2, default=_json_default) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps(report.get("summary", {}), indent=2))


if __name__ == "__main__":
    main()
