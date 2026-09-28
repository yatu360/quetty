"""Controlled Phase 7 Patchright recovery, concurrency, capacity, and resource benchmark.

Patchright is the candidate and installed Chrome driven by Playwright is the control.
Camoufox is deliberately excluded. Every browser call targets
:class:`LocalQueueSimulator` on 127.0.0.1, so results are local evidence only: they
are not Queue-it throughput, restore reliability, or a staging-safe concurrency level.

The durable identity under test is the persisted expected Queue ID. The report is
aggregate: it never contains Queue IDs, session IDs, transfer URLs, storage state, or
fingerprint values. Synthetic identities exist only in the temporary work directory.

The Phase 6 scenarios for park/reopen, restarts, kills, restoration faults, and the
operator runtime are reused unchanged; this module adds the families Phase 7 needs on
top: multi-context concurrency with reacquisition, create/navigate/close churn, a
monitoring-shaped workload with a resource timeline, stuck navigation, and a shutdown
matrix.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import math
import os
import signal
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any

from queue_load_test.browser import BrowserManager, OwnedBrowserContext
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase4_recovery import Outcome, ScenarioResult
from queue_load_test.harness.phase6_camoufox_benchmark import (
    NAVIGATION_TIMEOUT_MS,
    PopulationContext,
    RecordingRestorer,
    _AppHarness,
    _dump_tasks,
    _exception_note,
    _json_default,
    _print_scenario,
    average_peak,
    create_population,
    environment,
    identity_digest,
    identity_map,
    kill_pid,
    main_pids,
    make_manager,
    make_restorer,
    make_scheduler,
    mark_all_due,
    metric_total,
    open_when_free,
    owned_rows,
    runtime_actions,
    scenario_application_restart,
    scenario_dict,
    scenario_failure_during_monitoring,
    scenario_full_browser_restart,
    scenario_manual_and_shutdown,
    scenario_multi_slot_failure,
    scenario_park_reopen,
    scenario_process_failure,
    scenario_repeated_failure,
    scenario_restoration_failures,
    scheduler_or_zero,
    seed_sessions,
    stats,
    stop_reason,
    until,
)
from queue_load_test.harness.phase6_camoufox_benchmark import (
    run_sweep as _run_sweep,
)
from queue_load_test.harness.resource_benchmark import PsutilProcessResourceProbe
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import BrowserBackendName, QueueSession, QueueStatus
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    QueueSessionMonitor,
)
from queue_load_test.scheduler.monitoring import MonitoringOutcome, PollingPolicy
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer
from queue_load_test.web.actions import OperatorActionKind, OperatorActionStatus

try:  # optional benchmark extra
    HOST_RAM_BYTES: int | None = int(
        importlib.import_module("psutil").virtual_memory().total
    )
except ImportError:  # pragma: no cover
    HOST_RAM_BYTES = None

CANDIDATE = BrowserBackendName.PATCHRIGHT
CONTROL = BrowserBackendName.CHROME
DEFAULT_SINGLE_PROCESS_LEVELS = (1, 5, 10, 20, 25)
DEFAULT_MULTI_PROCESS_LEVELS = (50,)
DEFAULT_CHURN_LEVELS = (1, 5, 10, 20)
# Any single browser step that has not settled after this long is recorded as stuck.
STEP_DEADLINE_SECONDS = 15.0
# A fresh context that cannot navigate the local simulator this fast is unhealthy.
HEALTH_PROBE_DEADLINE_SECONDS = 5.0
# Simulated stuck page in the shutdown matrix: longer than the application's 30 s
# navigation timeout, so only the application's own deadlines can end the work.
STUCK_PAGE_SECONDS = 90.0


def foreign_for(backend: BrowserBackendName) -> BrowserBackendName:
    """Phase 7 provenance pairing: candidate and control reject each other's rows."""

    return CONTROL if backend is CANDIDATE else CANDIDATE


# --- shared helpers ----------------------------------------------------------------------


def browser_level_contexts(manager: BrowserManager) -> int | None:
    """Contexts the browser processes themselves report, independent of manager books."""

    total = 0
    try:
        for slot in manager._slots:
            if slot.browser.is_connected():
                total += len(slot.browser.contexts)
    except Exception:  # noqa: BLE001 - a process can die between calls
        return None
    return total


async def health_probe(manager: BrowserManager, simulator: LocalQueueSimulator) -> float | None:
    """Seconds for a fresh context to navigate; ``None`` means wedged or failed."""

    began = time.perf_counter()
    owned: OwnedBrowserContext | None = None
    try:
        async with asyncio.timeout(HEALTH_PROBE_DEADLINE_SECONDS):
            owned = await manager.create_context()
            page = await owned.context.new_page()
            response = await page.goto(
                simulator.transfer_url("health-probe"),
                wait_until="domcontentloaded",
                timeout=HEALTH_PROBE_DEADLINE_SECONDS * 1000,
            )
            if response is None or response.status != 200:
                return None
    except Exception:  # noqa: BLE001 - any failure is an unhealthy probe
        return None
    finally:
        if owned is not None:
            with contextlib.suppress(Exception):
                await owned.close()
    return round(time.perf_counter() - began, 3)


def instrument_acquisition(manager: BrowserManager) -> tuple[list[float], list[float]]:
    """Record total create_context time and allocation-lock wait for every caller."""

    total: list[float] = []
    lock_wait: list[float] = []
    original = manager.create_context

    async def create_context(**kwargs: Any) -> OwnedBrowserContext:
        began = time.perf_counter()
        owned = await original(**kwargs)
        total.append(time.perf_counter() - began)
        lock_wait.append(owned.acquisition_wait_seconds)
        return owned

    manager.create_context = create_context  # type: ignore[method-assign]
    return total, lock_wait


class TimedHandler:
    """Wrap the production monitor and time every check."""

    def __init__(self, monitor: QueueSessionMonitor) -> None:
        self.monitor = monitor
        self.durations: list[float] = []
        self.completed_at: list[float] = []
        self.raised = 0

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        began = time.perf_counter()
        try:
            return await self.monitor.check(session)
        except Exception:
            self.raised += 1
            raise
        finally:
            finished = time.perf_counter()
            self.durations.append(finished - began)
            self.completed_at.append(finished)


class ResourceSampler:
    """Periodic application/browser/scheduler sampling for the resource timeline."""

    def __init__(
        self,
        backend: BrowserBackendName,
        manager: BrowserManager,
        *,
        interval: float,
        metrics: PrometheusMetrics | None = None,
        extra: Callable[[], dict[str, float | int | None]] | None = None,
    ) -> None:
        self._backend = backend
        self._manager = manager
        self._interval = interval
        self._metrics = metrics
        self._extra = extra
        self._probe = PsutilProcessResourceProbe(backend=backend)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = 0.0
        self.samples: list[dict[str, float | int | None]] = []

    def start(self) -> None:
        self._started = time.perf_counter()
        self._task = asyncio.create_task(self._run(), name=f"sampler-{self._backend.value}")

    async def stop(self) -> list[dict[str, float | int | None]]:
        self._stop.set()
        if self._task is not None:
            await self._task
        return self.samples

    def sample_now(self) -> dict[str, float | int | None]:
        snapshot = self._probe.sample()
        row: dict[str, float | int | None] = {
            "t": round(time.perf_counter() - self._started, 2),
            "application_cpu_percent": _round(snapshot.application_cpu_percent),
            "application_rss_mb": _mb(snapshot.application_ram_bytes),
            "browser_cpu_percent": _round(snapshot.browser_cpu_percent),
            "browser_tree_rss_mb": _mb(snapshot.browser_ram_bytes),
            "browser_tree_processes": snapshot.observed_browser_processes,
            "browser_main_processes": snapshot.observed_browser_main_processes,
            "managed_processes": self._manager.managed_process_count,
            "active_contexts": self._manager.active_context_count,
            "browser_level_contexts": browser_level_contexts(self._manager),
            "browser_restarts": self._manager.restart_count,
        }
        if self._metrics is not None:
            row.update(
                {
                    "operation_timeouts": int(
                        metric_total(self._metrics, "browser_operation_timeouts_total")
                    ),
                    "navigation_failures": int(
                        metric_total(self._metrics, "navigation_failures_total")
                    ),
                    "context_creation_failures": int(
                        metric_total(self._metrics, "browser_context_creation_failures_total")
                    ),
                }
            )
        if self._extra is not None:
            row.update(self._extra())
        self.samples.append(row)
        return row

    async def _run(self) -> None:
        while True:
            self.sample_now()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except TimeoutError:
                continue


def _round(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _mb(value: int | None) -> float | None:
    return round(value / 1_048_576, 1) if value is not None else None


def summarize_samples(
    samples: Sequence[dict[str, float | int | None]], keys: Sequence[str]
) -> dict[str, dict[str, float | None]]:
    return {key: average_peak(sample.get(key) for sample in samples) for key in keys}


RESOURCE_KEYS = (
    "application_cpu_percent",
    "application_rss_mb",
    "browser_cpu_percent",
    "browser_tree_rss_mb",
    "browser_tree_processes",
    "browser_main_processes",
    "managed_processes",
    "active_contexts",
    "browser_level_contexts",
)


def monitoring_polling_policy(interval: float, jitter: float) -> PollingPolicy:
    """Every active session becomes due again ``interval`` seconds after its check."""

    return PollingPolicy(
        default_seconds=interval,
        jitter_seconds=jitter,
        pre_queue_min_seconds=interval,
        pre_queue_max_seconds=interval,
        active_early_min_seconds=interval,
        active_early_max_seconds=interval,
        active_mid_min_seconds=interval,
        active_mid_max_seconds=interval,
        serviced_soon_min_seconds=interval,
        serviced_soon_max_seconds=interval,
        turn_started_seconds=interval,
    )


# --- B: context concurrency ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConcurrencyCase:
    backend: BrowserBackendName
    processes: int
    contexts: int

    @property
    def family(self) -> str:
        return f"{self.backend.value}-p{self.processes}"

    @property
    def label(self) -> str:
        return f"{self.backend.value}-p{self.processes}-c{self.contexts}"

    @property
    def contexts_per_process(self) -> int:
        return math.ceil(self.contexts / self.processes)


def concurrency_families(
    backend: BrowserBackendName,
    single_process_levels: Sequence[int],
    multi_process_levels: Sequence[int],
) -> list[list[ConcurrencyCase]]:
    """One-process levels, then ``ceil(contexts / 25)``-process levels (existing ceilings)."""

    single = [ConcurrencyCase(backend, 1, level) for level in single_process_levels if level <= 25]
    multi = [
        ConcurrencyCase(backend, max(2, math.ceil(level / 25)), level)
        for level in multi_process_levels
        if math.ceil(level / 25) <= 4
    ]
    return [family for family in (single, multi) if family]


def concurrency_stop_reason(result: dict[str, Any]) -> str | None:
    """Phase 6 objective stop rules plus reacquisition, churn, health, and leak evidence."""

    reason = stop_reason(
        result, host_ram_bytes=HOST_RAM_BYTES, logical_cpus=os.cpu_count() or 1
    )
    if reason is not None:
        return reason
    if result["reacquire"]["failures"]:
        return "reacquisition failures"
    restoration = result.get("restoration") or {}
    if restoration and restoration.get("successes") != restoration.get("restores"):
        return "churn sweep restore failures"
    if result["health_probe_seconds"] is None:
        return "process wedged (health probe failed)"
    if result["final"]["new_main_processes_after_shutdown"]:
        return "process leak after shutdown"
    return None


async def run_concurrency_case(
    case: ConcurrencyCase,
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    navigation_waves: int,
    hold_seconds: float,
    sample_interval_seconds: float,
) -> dict[str, Any]:
    """Create, navigate, hold, close, reacquire, then churn ``contexts`` live contexts."""

    metrics = PrometheusMetrics()
    manager = make_manager(
        case.backend,
        processes=case.processes,
        contexts_per_process=case.contexts_per_process,
        max_active=case.contexts,
        metrics=metrics,
    )
    baseline = main_pids(case.backend)
    sampler = ResourceSampler(
        case.backend, manager, interval=sample_interval_seconds, metrics=metrics
    )
    owned: list[tuple[OwnedBrowserContext, Any]] = []
    acquisition: list[float] = []
    creation: list[float] = []
    lock_wait: list[float] = []
    navigation: list[float] = []
    counters: Counter[str] = Counter()
    status = "COMPLETED"
    error: str | None = None
    achieved = 0
    browser_contexts_while_held: int | None = None
    browser_contexts_after_close: int | None = None
    reacquire: dict[str, Any] = {"failures": 0}
    restore_summary: dict[str, object] = {}
    sweep: dict[str, object] = {}
    probe: float | None = None
    repository: SQLiteSessionRepository | None = None

    async def allocate(count: int, bucket: str) -> list[tuple[OwnedBrowserContext, Any]]:
        work = list(range(count))
        made: list[tuple[OwnedBrowserContext, Any]] = []

        async def worker() -> None:
            while work:
                work.pop()
                began = time.perf_counter()
                try:
                    context = await manager.create_context()
                    page = await asyncio.wait_for(
                        context.context.new_page(), timeout=STEP_DEADLINE_SECONDS
                    )
                except Exception:  # noqa: BLE001 - recorded case failure
                    counters[f"{bucket}_creation_failures"] += 1
                    continue
                if bucket == "hold":
                    acquisition.append(time.perf_counter() - began)
                    creation.append(context.creation_duration_seconds)
                    lock_wait.append(context.acquisition_wait_seconds)
                made.append((context, page))

        await asyncio.gather(*(worker() for _ in range(min(10, count))))
        return made

    async def navigate(index: int, page: Any, bucket: str) -> None:
        began = time.perf_counter()
        try:
            response = await page.goto(
                simulator.transfer_url(f"concurrency-{index:03d}"),
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            if response is None or response.status >= 400:
                counters[f"{bucket}_navigation_failures"] += 1
        except Exception:  # noqa: BLE001 - timeouts and navigation errors
            counters[f"{bucket}_navigation_failures"] += 1
        finally:
            if bucket == "hold":
                navigation.append(time.perf_counter() - began)

    started = time.perf_counter()
    sampler.start()
    try:
        await manager.start()
        owned = await allocate(case.contexts, "hold")
        achieved = manager.active_context_count
        for _ in range(navigation_waves):
            await asyncio.gather(
                *(navigate(index, page, "hold") for index, (_, page) in enumerate(owned))
            )
        await asyncio.sleep(hold_seconds)
        browser_contexts_while_held = browser_level_contexts(manager)
        await asyncio.gather(
            *(navigate(index, page, "hold") for index, (_, page) in enumerate(owned))
        )
        close_started = time.perf_counter()
        await asyncio.gather(*(context.close() for context, _ in owned))
        close_seconds = time.perf_counter() - close_started
        owned.clear()
        await asyncio.sleep(0.2)
        browser_contexts_after_close = browser_level_contexts(manager)

        # Reacquire the same number of contexts on the same processes.
        reacquire_started = time.perf_counter()
        owned = await allocate(case.contexts, "reacquire")
        reacquired = manager.active_context_count
        await asyncio.gather(
            *(navigate(index, page, "reacquire") for index, (_, page) in enumerate(owned))
        )
        await asyncio.gather(*(context.close() for context, _ in owned))
        owned.clear()
        reacquire = {
            "achieved_active_contexts": reacquired,
            "failures": counters["reacquire_creation_failures"]
            + counters["reacquire_navigation_failures"]
            + max(0, case.contexts - reacquired),
            "duration_seconds": round(time.perf_counter() - reacquire_started, 3),
            "contexts_after_close": manager.active_context_count,
        }

        # Production churn: fixed workers = contexts, fresh context per restore.
        database = directory / f"{case.label}.sqlite3"
        repository = SQLiteSessionRepository(database)
        await repository.initialize()
        state_store = FileSystemStateStore(directory / f"{case.label}-state")
        await seed_sessions(
            repository,
            simulator,
            state_store,
            backend=case.backend,
            count=max(10, case.contexts * 3),
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
            scheduler_id=f"concurrency-{case.label}",
        )
        sweep = await _run_sweep(
            database,
            repository,
            scheduler,
            manager,
            timeout_seconds=max(60.0, case.contexts * 6.0),
        )
        duration = float(str(sweep["duration_seconds"])) or 1.0
        successes = sum(item.success for item in recorder.observations)
        sweep["local_simulator_checks_per_second"] = round(successes / duration, 3)
        restore_summary = recorder.summary()
        probe = await health_probe(manager, simulator)
    except Exception as exc:  # noqa: BLE001 - preserve objective failure output
        status = "FAILED"
        error = type(exc).__name__
        close_seconds = 0.0
    finally:
        for context, _ in owned:
            with contextlib.suppress(Exception):
                await context.close()
        contexts_final = manager.active_context_count if manager.started else 0
        shutdown_started = time.perf_counter()
        await manager.shutdown()
        shutdown_seconds = time.perf_counter() - shutdown_started
        if repository is not None:
            await repository.close()
        samples = await sampler.stop()
    await until(lambda: not (main_pids(case.backend) - baseline), timeout=10)
    resources = summarize_samples(samples, RESOURCE_KEYS)
    return {
        "case": case.label,
        "family": case.family,
        "backend": case.backend.value,
        "status": status,
        "error_category": error,
        "configured_browser_processes": case.processes,
        "configured_contexts": case.contexts,
        "configured_contexts_per_process": case.contexts_per_process,
        "achieved_active_contexts": achieved,
        "browser_level_contexts_while_held": browser_contexts_while_held,
        "browser_level_contexts_after_close": browser_contexts_after_close,
        "navigation_waves": navigation_waves + 1,
        "hold_seconds": hold_seconds,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "context_creation_seconds": stats(creation),
        "context_acquisition_seconds": stats(acquisition),
        "allocation_lock_wait_seconds": stats(lock_wait),
        "local_navigation_seconds": stats(navigation),
        "close_all_seconds": round(close_seconds, 3),
        "reacquire": reacquire,
        "sweep": sweep,
        "restoration": restore_summary,
        "health_probe_seconds": probe,
        "shutdown_seconds": round(shutdown_seconds, 3),
        # Keys match Phase 6 stop_reason(); MB values are converted back to bytes there.
        "resources": {
            "application_rss_bytes": _bytes_peak(resources["application_rss_mb"]),
            "browser_tree_rss_bytes": _bytes_peak(resources["browser_tree_rss_mb"]),
            "browser_cpu_percent": resources["browser_cpu_percent"],
            "application_cpu_percent": resources["application_cpu_percent"],
            "browser_tree_processes": resources["browser_tree_processes"],
            "browser_main_processes": resources["browser_main_processes"],
            "active_contexts": resources["active_contexts"],
        },
        "samples": len(samples),
        "failures": {
            "context_creation_failures": counters["hold_creation_failures"]
            + int(metric_total(metrics, "browser_context_creation_failures_total")),
            "navigation_failures": counters["hold_navigation_failures"],
            "identity_mismatches": int(str(restore_summary.get("identity_mismatches", 0) or 0)),
            "browser_crashes": int(metric_total(metrics, "browser_crashes_total")),
            "browser_operation_timeouts": int(
                metric_total(metrics, "browser_operation_timeouts_total")
            ),
            "cleanup_failures": int(metric_total(metrics, "browser_cleanup_failures_total")),
        },
        "final": {
            "contexts": contexts_final,
            "new_main_processes_after_shutdown": len(main_pids(case.backend) - baseline),
        },
    }


def _bytes_peak(summary: dict[str, float | None]) -> dict[str, float | None]:
    return {
        key: (value * 1_048_576 if value is not None else None)
        for key, value in summary.items()
    }


async def run_concurrency_matrix(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backends: Sequence[BrowserBackendName],
    single_process_levels: Sequence[int],
    multi_process_levels: Sequence[int],
    navigation_waves: int,
    hold_seconds: float,
    sample_interval_seconds: float,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    stopped: dict[str, dict[str, str]] = {}
    for backend in backends:
        for family in concurrency_families(backend, single_process_levels, multi_process_levels):
            for case in family:
                result = await run_concurrency_case(
                    case,
                    simulator,
                    directory,
                    navigation_waves=navigation_waves,
                    hold_seconds=hold_seconds,
                    sample_interval_seconds=sample_interval_seconds,
                )
                results.append(result)
                restoration = result.get("restoration") or {}
                print(
                    f"[concurrency] {case.label}: {result['status']} "
                    f"achieved={result['achieved_active_contexts']} "
                    f"reacquire_failures={result['reacquire']['failures']} "
                    f"churn={restoration.get('successes')}/{restoration.get('restores')} "
                    f"hold_nav_failures={result['failures']['navigation_failures']} "
                    f"probe={result['health_probe_seconds']}",
                    flush=True,
                )
                reason = concurrency_stop_reason(result)
                if reason is not None:
                    stopped[case.family] = {"after_case": case.label, "reason": reason}
                    break
    healthy: dict[str, int] = {}
    for result in results:
        if concurrency_stop_reason(result) is None:
            name = str(result["family"])
            healthy[name] = max(healthy.get(name, 0), int(result["configured_contexts"]))
    return {
        "cases": results,
        "stopped_early": stopped,
        "highest_healthy_level": healthy,
    }


# --- C: creation/navigation/close churn ----------------------------------------------------


@dataclass(slots=True)
class ChurnLevel:
    timings: dict[str, list[float]] = field(
        default_factory=lambda: {
            name: [] for name in ("create", "page", "navigate", "inspect", "close", "cycle")
        }
    )
    failures: Counter[str] = field(default_factory=Counter)
    stuck: Counter[str] = field(default_factory=Counter)
    peak_manager_contexts: int = 0
    peak_browser_contexts: int = 0


async def _churn_level(
    manager: BrowserManager, simulator: LocalQueueSimulator, workers: int, cycles: int
) -> ChurnLevel:
    """``workers`` loops share ``cycles`` create/page/navigate/inspect/close cycles."""

    level = ChurnLevel()
    remaining = [cycles]

    async def step(name: str, operation: Awaitable[Any]) -> Any:
        began = time.perf_counter()
        try:
            return await asyncio.wait_for(operation, timeout=STEP_DEADLINE_SECONDS)
        except TimeoutError:
            level.stuck[name] += 1
            raise
        finally:
            level.timings[name].append(time.perf_counter() - began)

    async def worker(index: int) -> None:
        while remaining[0] > 0:
            remaining[0] -= 1
            began = time.perf_counter()
            owned: OwnedBrowserContext | None = None
            try:
                owned = await step("create", manager.create_context())
                page = await step("page", owned.context.new_page())
                response = await step(
                    "navigate",
                    page.goto(
                        simulator.transfer_url(f"churn-{index:02d}"),
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    ),
                )
                if response is None or response.status != 200:
                    level.failures["navigate_status"] += 1
                length = await step("inspect", page.evaluate("document.body.innerText.length"))
                if not length:
                    level.failures["inspect_empty"] += 1
                level.peak_manager_contexts = max(
                    level.peak_manager_contexts, manager.active_context_count
                )
                level.peak_browser_contexts = max(
                    level.peak_browser_contexts, browser_level_contexts(manager) or 0
                )
            except Exception as exc:  # noqa: BLE001 - counted by type
                level.failures[type(exc).__name__] += 1
            finally:
                if owned is not None:
                    try:
                        await step("close", owned.close())
                    except Exception as exc:  # noqa: BLE001
                        level.failures[f"close_{type(exc).__name__}"] += 1
                level.timings["cycle"].append(time.perf_counter() - began)

    await asyncio.gather(*(worker(index) for index in range(workers)))
    return level


async def scenario_churn(
    simulator: LocalQueueSimulator,
    *,
    backend: BrowserBackendName,
    levels: Sequence[int],
    cycles_per_level: int,
    sample_interval_seconds: float,
) -> ScenarioResult:
    """Fixed workers repeatedly create/page/navigate/inspect/close on one long-lived process."""

    result = ScenarioResult(
        key=f"churn_{backend.value}",
        title=f"Create/page/navigate/inspect/close churn at {list(levels)} workers",
        evidence="one long-lived managed browser process; LocalQueueSimulator pages",
    )
    metrics = PrometheusMetrics()
    top = max(levels)
    manager = make_manager(
        backend, processes=1, contexts_per_process=top, max_active=top, metrics=metrics
    )
    baseline = main_pids(backend)
    sampler = ResourceSampler(backend, manager, interval=sample_interval_seconds, metrics=metrics)
    rows: list[dict[str, Any]] = []
    await manager.start()
    sampler.start()
    pids = main_pids(backend) - baseline
    try:
        for workers in levels:
            level_started = time.perf_counter()
            level = await _churn_level(manager, simulator, workers, cycles_per_level)
            elapsed = time.perf_counter() - level_started
            failures, stuck = level.failures, level.stuck
            peak_browser = level.peak_browser_contexts
            await asyncio.sleep(0.3)
            after_manager = manager.active_context_count
            after_browser = browser_level_contexts(manager)
            probe = await health_probe(manager, simulator)
            connected = all(slot.browser.is_connected() for slot in manager._slots)
            processes_now = main_pids(backend) - baseline
            row = {
                "workers": workers,
                "cycles": cycles_per_level,
                "duration_seconds": round(elapsed, 3),
                "cycles_per_second": round(cycles_per_level / elapsed, 2),
                "latency_seconds": {
                    name: stats(values) for name, values in level.timings.items()
                },
                "failures": dict(failures),
                "stuck_calls": dict(stuck),
                "peak_manager_contexts": level.peak_manager_contexts,
                "peak_browser_level_contexts": peak_browser,
                "manager_contexts_after": after_manager,
                "browser_level_contexts_after": after_browser,
                "health_probe_seconds": probe,
                "connected_after": connected,
                "restarts_so_far": manager.restart_count,
                "managed_main_processes": len(processes_now),
            }
            rows.append(row)
            label = f"w{workers}"
            print(
                f"[churn] {backend.value} {label}: {cycles_per_level} cycles "
                f"{row['cycles_per_second']}/s failures={dict(failures)} stuck={dict(stuck)} "
                f"probe={probe}",
                flush=True,
            )
            result.check(f"{label}_no_cycle_failures", not failures)
            result.check(f"{label}_no_stuck_browser_calls", not stuck)
            result.check(
                f"{label}_contexts_return_to_zero", after_manager == 0 and after_browser == 0
            )
            result.check(f"{label}_context_count_bounded_by_workers", peak_browser <= workers)
            result.check(
                f"{label}_process_healthy_not_wedged", connected and probe is not None
            )
            result.check(
                f"{label}_single_process_no_leak",
                processes_now == pids and manager.restart_count == 0,
            )
            if failures or stuck or probe is None:
                result.notes.append(f"stopped after {label}: churn failures or wedge")
                break
    finally:
        samples = await sampler.stop()
        shutdown_started = time.perf_counter()
        await manager.shutdown()
        shutdown_seconds = time.perf_counter() - shutdown_started
    gone = await until(lambda: not (main_pids(backend) - baseline), timeout=10)
    result.check("shutdown_bounded", shutdown_seconds <= 10.0)
    result.check("no_process_leak_after_shutdown", gone is not None)
    result.measurements.update(
        {
            "levels": rows,
            "total_cycles": cycles_per_level * len(rows),
            "shutdown_seconds": round(shutdown_seconds, 3),
            "resources": summarize_samples(samples, RESOURCE_KEYS),
            "cleanup_failures": int(metric_total(metrics, "browser_cleanup_failures_total")),
        }
    )
    return result.finish()


# --- D + K: monitoring-shaped workload ------------------------------------------------------


@dataclass(slots=True)
class MonitoringProfile:
    processes: int
    workers: int
    contexts_per_process: int = 25

    @property
    def label(self) -> str:
        return f"p{self.processes}-w{self.workers}"


@dataclass(slots=True)
class MonitoringPopulation:
    database: Path
    state_directory: Path
    identities: dict[str, str | None] = field(default_factory=dict)


async def scenario_monitoring_workload(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backend: BrowserBackendName,
    size: int,
    profiles: Sequence[MonitoringProfile],
    duration_seconds: float,
    poll_interval_seconds: float,
    sample_interval_seconds: float,
) -> ScenarioResult:
    """Run the production scheduler/monitor/restorer continuously over a parked population."""

    result = ScenarioResult(
        key=f"monitoring_workload_{backend.value}",
        title=(
            f"{size} persisted sessions re-checked every {poll_interval_seconds:g}s for "
            f"{duration_seconds:g}s per profile"
        ),
        evidence="production scheduler, monitor, restorer, SQLite, state files; local pages",
    )
    population = MonitoringPopulation(
        database=directory / f"monitoring-{backend.value}.sqlite3",
        state_directory=directory / f"monitoring-{backend.value}-state",
    )
    repository = SQLiteSessionRepository(population.database)
    await repository.initialize()
    state_store = FileSystemStateStore(population.state_directory)
    creator_manager = make_manager(backend, processes=2, contexts_per_process=4)
    try:
        await creator_manager.start()
        creation = await create_population(
            manager=creator_manager,
            repository=repository,
            state_store=state_store,
            simulator=simulator,
            backend=backend,
            count=size,
            concurrency=8,
            prefix=f"monitor-{backend.value}",
        )
    finally:
        await creator_manager.shutdown()
    population.identities = identity_map(population.database)
    baseline = identity_digest(population.identities)
    issued_after_creation = simulator.new_identities
    result.measurements["creation"] = creation
    result.check("population_created", creation["created"] == size)
    rows: list[dict[str, Any]] = []
    try:
        for profile in profiles:
            row = await _monitoring_profile(
                simulator,
                population,
                repository,
                state_store,
                backend=backend,
                profile=profile,
                duration_seconds=duration_seconds,
                poll_interval_seconds=poll_interval_seconds,
                sample_interval_seconds=sample_interval_seconds,
            )
            rows.append(row)
            label = profile.label
            print(
                f"[monitoring] {backend.value} {label}: {row['checks_per_second']} checks/s "
                f"check p50/p95={row['check_seconds']['p50']}/{row['check_seconds']['p95']} "
                f"backlog final={row['due_backlog_final']} failures={row['restoration']['failures']}",
                flush=True,
            )
            restoration = row["restoration"]
            result.check(f"{label}_checks_completed", row["checks"] > 0)
            result.check(
                f"{label}_zero_identity_mismatches", restoration["identity_mismatches"] == 0
            )
            result.check(
                f"{label}_every_restore_succeeded",
                restoration["successes"] == restoration["restores"],
            )
            result.check(f"{label}_no_check_raised", row["checks_raised"] == 0)
            result.check(
                f"{label}_active_contexts_bounded_by_workers",
                row["active_context_peak"] <= profile.workers,
            )
            result.check(
                f"{label}_queue_depth_bounded",
                (row["queue_depth"]["peak"] or 0) <= profile.workers * 2,
            )
            result.check(
                f"{label}_zero_contexts_and_leases_after_drain",
                row["contexts_after"] == 0 and row["leases_after"] == 0,
            )
            result.check(f"{label}_drain_bounded", row["drain_seconds"] <= 20.0)
            result.check(
                f"{label}_queue_ids_unchanged",
                identity_digest(identity_map(population.database)) == baseline,
            )
            result.check(f"{label}_no_process_leak", row["new_main_processes_after"] == 0)
        result.check(
            "no_replacement_identities", simulator.new_identities == issued_after_creation
        )
    finally:
        await repository.close()
    result.measurements["profiles"] = rows
    return result.finish()


async def _monitoring_profile(
    simulator: LocalQueueSimulator,
    population: MonitoringPopulation,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    *,
    backend: BrowserBackendName,
    profile: MonitoringProfile,
    duration_seconds: float,
    poll_interval_seconds: float,
    sample_interval_seconds: float,
) -> dict[str, Any]:
    metrics = PrometheusMetrics()
    manager = make_manager(
        backend,
        processes=profile.processes,
        contexts_per_process=profile.contexts_per_process,
        max_active=profile.processes * profile.contexts_per_process,
        metrics=metrics,
    )
    baseline_pids = main_pids(backend)
    acquisition, lock_wait = instrument_acquisition(manager)
    recorder = RecordingRestorer(
        make_restorer(manager, repository, state_store, simulator, backend, metrics)
    )
    handler = TimedHandler(
        QueueSessionMonitor(
            repository=repository,
            restorer=recorder,
            polling_policy=monitoring_polling_policy(poll_interval_seconds, 0.5),
            retry_policy=MonitoringRetryPolicy(
                max_attempts=3,
                initial_backoff_seconds=0.2,
                maximum_backoff_seconds=1.0,
                jitter_seconds=0.1,
            ),
            observability=metrics,
        )
    )
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=handler,
        worker_count=profile.workers,
        queue_capacity=profile.workers * 2,
        claim_batch_size=profile.workers * 2,
        lease_seconds=60.0,
        failure_delay_seconds=3600,
        scheduler_tick_seconds=0.05,
        shutdown_timeout_seconds=15,
        scheduler_id=f"monitoring-{profile.label}",
        observability=metrics,
    )

    def scheduler_view() -> dict[str, float | int | None]:
        return {
            "queue_depth": scheduler.queue_size,
            "due_backlog": scheduler.metrics.due_backlog,
            "oldest_overdue_seconds": round(scheduler.metrics.oldest_overdue_seconds, 2),
            "currently_checking": scheduler.metrics.currently_checking,
            "checks_completed": len(handler.durations),
        }

    sampler = ResourceSampler(
        backend, manager, interval=sample_interval_seconds, metrics=metrics, extra=scheduler_view
    )
    await manager.start()
    mark_all_due(population.database)
    stop = asyncio.Event()
    started = time.perf_counter()
    sampler.start()
    task = asyncio.create_task(scheduler.run(stop), name=f"monitoring-{profile.label}")
    try:
        await asyncio.sleep(duration_seconds)
    finally:
        window_end = time.perf_counter()
        stop.set()
        drain_started = time.perf_counter()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=30)
        drain_seconds = time.perf_counter() - drain_started
        samples = await sampler.stop()
        contexts_after = manager.active_context_count
        await manager.shutdown()
    await until(lambda: not (main_pids(backend) - baseline_pids), timeout=10)
    in_window = [at for at in handler.completed_at if at <= window_end]
    window = window_end - started
    # Steady state excludes the initial all-due burst (first poll interval).
    steady = [at for at in in_window if at - started >= poll_interval_seconds]
    steady_window = max(0.001, window - poll_interval_seconds)
    backlog = [sample.get("due_backlog") for sample in samples]
    return {
        "profile": profile.label,
        "browser_processes": profile.processes,
        "monitor_workers": profile.workers,
        "population": len(population.identities),
        "poll_interval_seconds": poll_interval_seconds,
        "offered_checks_per_second": round(len(population.identities) / poll_interval_seconds, 2),
        "window_seconds": round(window, 2),
        "checks": len(in_window),
        "checks_per_second": round(len(in_window) / window, 2),
        "steady_state_checks_per_second": round(len(steady) / steady_window, 2),
        "check_seconds": stats(handler.durations),
        "checks_raised": handler.raised,
        "restoration": recorder.summary(),
        "context_acquisition_seconds": stats(acquisition),
        "allocation_lock_wait_seconds": stats(lock_wait),
        "active_context_peak": max(
            (int(sample.get("active_contexts") or 0) for sample in samples), default=0
        ),
        "queue_depth": average_peak(sample.get("queue_depth") for sample in samples),
        "due_backlog": average_peak(backlog),
        "due_backlog_final": backlog[-1] if backlog else None,
        "oldest_overdue_seconds": average_peak(
            sample.get("oldest_overdue_seconds") for sample in samples
        ),
        "drain_seconds": round(drain_seconds, 3),
        "contexts_after": contexts_after,
        "leases_after": owned_rows(population.database)["automatic_leases"],
        "browser_restarts": manager.restart_count,
        "operation_timeouts": int(metric_total(metrics, "browser_operation_timeouts_total")),
        "navigation_failures": int(metric_total(metrics, "navigation_failures_total")),
        "context_creation_failures": int(
            metric_total(metrics, "browser_context_creation_failures_total")
        ),
        "new_main_processes_after": len(main_pids(backend) - baseline_pids),
        "resources": summarize_samples(samples, RESOURCE_KEYS),
        "timeline": samples,
    }


# --- G: stuck/slow navigation ---------------------------------------------------------------


async def scenario_stuck_navigation(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backend: BrowserBackendName,
    repeated_timeouts: int,
) -> ScenarioResult:
    """Navigation that never completes in time: deadlines, workers, leases, health, identity."""

    result = ScenarioResult(
        key=f"stuck_navigation_{backend.value}",
        title="Slow/stuck navigation beyond the navigation and attempt deadlines",
        evidence="LocalQueueSimulator delays responses 30s for chosen synthetic identities",
    )
    database = directory / f"stuck-{backend.value}.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    state_store = FileSystemStateStore(directory / f"stuck-{backend.value}-state")
    metrics = PrometheusMetrics()
    manager = make_manager(backend, processes=1, contexts_per_process=6, metrics=metrics)
    baseline_pids = main_pids(backend)
    navigation_timeout_ms = 2_000
    attempt_timeout = 6.0
    slow: list[str] = []
    try:
        await manager.start()
        await create_population(
            manager=manager,
            repository=repository,
            state_store=state_store,
            simulator=simulator,
            backend=backend,
            count=10,
            concurrency=2,
            prefix=f"stuck-{backend.value}",
        )
        identities = identity_map(database)
        ids = sorted(identities)
        new_before = simulator.new_identities
        slow_ids, fast_ids = ids[:4], ids[4:]
        slow = [str(identities[sid]) for sid in slow_ids]
        simulator.slow_seconds = 30.0
        simulator.slow_ids.update(slow)
        restorer = QueueSessionRestorer(
            browser_manager=manager,
            repository=repository,
            state_store=state_store,
            expected_journey_url=simulator.queue_url,
            storage_navigation_url=simulator.queue_url,
            admission_detector=AdmissionDetector.from_urls(simulator.protected_url),
            navigation_timeout_ms=navigation_timeout_ms,
            admission_wait_timeout_ms=0,
            observation_timeout_seconds=1.0,
            observation_interval_seconds=0.1,
            observability=metrics,
            attempt_timeout_seconds=attempt_timeout,
            browser_backend=backend,
        )
        recorder = RecordingRestorer(restorer)

        # G1: a scheduler sweep where 4 of 10 due sessions never answer in time.
        scheduler = make_scheduler(
            repository, recorder, workers=2, metrics=metrics, scheduler_id="stuck-sweep"
        )
        mark_all_due(database)
        sweep = await _run_sweep(database, repository, scheduler, manager, timeout_seconds=180)
        observations = list(recorder.observations)
        swept = recorder.summary()
        sessions = {s.session_id: s for s in await repository.list()}
        slow_rows = [sessions[sid] for sid in slow_ids]
        timed_out = sum(not item.success for item in observations)
        max_restore = max((item.duration_seconds for item in observations), default=0.0)
        per_restore_bound = 2 * attempt_timeout + 1.0  # TRANSFER then STORAGE_STATE
        result.check("sweep_completed_despite_stuck_sessions", bool(sweep["completed"]))
        result.check(
            "every_fast_session_verified_while_others_stuck",
            swept["successes"] == len(fast_ids) and swept["identity_mismatches"] == 0,
        )
        result.check("deadline_fired_for_every_slow_session", timed_out >= len(slow_ids))
        result.check(
            "every_restore_ended_within_attempt_deadlines", max_restore <= per_restore_bound
        )
        result.check("leases_released_after_timeouts", sweep["leases_after"] == 0)
        result.check(
            "slow_rows_retryable_not_failed",
            all(row.status is not QueueStatus.FAILED and row.worker_id is None for row in slow_rows),
        )
        result.check("zero_contexts_after_sweep", sweep["contexts_after"] == 0)
        result.check(
            "no_identity_mismatch_from_timeouts",
            all(item.identity_match is not False for item in observations),
        )
        result.measurements["sweep"] = sweep
        result.measurements["sweep_restoration"] = swept
        result.measurements["restore_deadline_seconds"] = {
            "navigation": navigation_timeout_ms / 1000,
            "attempt": attempt_timeout,
            "per_restore_bound": per_restore_bound,
            "max_observed": round(max_restore, 3),
        }
        result.measurements["slow_row_statuses"] = dict(
            Counter(row.status.value for row in slow_rows)
        )

        # G2: repeated timeouts on one connected process, interleaved with healthy work.
        timeouts = 0
        healthy: list[float] = []
        started = time.perf_counter()
        for index in range(repeated_timeouts):
            async def slow_nav(queue_id: str = slow[index % len(slow)]) -> bool:
                owned = await manager.create_context()
                try:
                    page = await owned.context.new_page()
                    await page.goto(
                        simulator.transfer_url(queue_id),
                        wait_until="domcontentloaded",
                        timeout=1_000,
                    )
                    return False
                except Exception:  # noqa: BLE001 - the expected navigation timeout
                    return True
                finally:
                    await owned.close()

            timed_out, probe = await asyncio.gather(slow_nav(), health_probe(manager, simulator))
            timeouts += int(timed_out)
            if probe is not None:
                healthy.append(probe)
        connected = all(slot.browser.is_connected() for slot in manager._slots)
        final_probe = await health_probe(manager, simulator)
        result.check("repeated_navigation_timeouts_all_fired", timeouts == repeated_timeouts)
        result.check(
            "healthy_work_on_same_process_unaffected", len(healthy) == repeated_timeouts
        )
        result.check("process_still_connected_and_healthy", connected and final_probe is not None)
        result.measurements["repeated_timeouts"] = {
            "count": repeated_timeouts,
            "fired": timeouts,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "interleaved_healthy_navigation_seconds": stats(healthy),
            "restarts": manager.restart_count,
            "unresponsive_restarts": manager.unresponsive_restart_count,
            "final_health_probe_seconds": final_probe,
            "health_restart_policy": (
                "none (disconnect and abandoned-operation replacement only)"
                if getattr(manager._backend, "unresponsive_restart_threshold", None) is None
                else "consecutive-timeout threshold"
            ),
        }
        result.check(
            "no_restart_needed_for_a_healthy_connected_process",
            manager.restart_count == 0 and manager.unresponsive_restart_count == 0,
        )

        # G3: slow pages recover; the same rows restore their expected Queue IDs.
        simulator.slow_ids.difference_update(slow)
        recorder.reset()
        mark_all_due(database, slow_ids)
        retry_scheduler = make_scheduler(
            repository, recorder, workers=2, metrics=metrics, scheduler_id="stuck-retry"
        )
        retry = await _run_sweep(
            database, repository, retry_scheduler, manager, timeout_seconds=60
        )
        retried = recorder.summary()
        result.check(
            "retry_after_recovery_verifies_expected_queue_ids",
            bool(retry["completed"])
            and retried["successes"] == len(slow_ids)
            and retried["identity_mismatches"] == 0,
        )
        result.check("expected_queue_ids_unchanged", identity_map(database) == identities)
        result.check("no_replacement_identity_created", simulator.new_identities == new_before)
        result.measurements["retry_restoration"] = retried
        result.measurements["operation_timeouts"] = int(
            metric_total(metrics, "browser_operation_timeouts_total")
        )
    finally:
        simulator.slow_ids.difference_update(slow)
        simulator.slow_seconds = 3.0
        shutdown_started = time.perf_counter()
        await manager.shutdown()
        shutdown_seconds = time.perf_counter() - shutdown_started
        await repository.close()
    gone = await until(lambda: not (main_pids(backend) - baseline_pids), timeout=10)
    result.check("shutdown_bounded_after_timeouts", shutdown_seconds <= 10.0)
    result.check("no_process_leak", gone is not None)
    result.measurements["shutdown_seconds"] = round(shutdown_seconds, 3)
    return result.finish()


# --- J: shutdown matrix ---------------------------------------------------------------------


async def scenario_shutdown_matrix(
    simulator: LocalQueueSimulator,
    directory: Path,
    *,
    backend: BrowserBackendName,
    headed: bool,
) -> ScenarioResult:
    """Application shutdown under combinations of in-flight work; every one must be bounded."""

    result = ScenarioResult(
        key=f"shutdown_matrix_{backend.value}",
        title="Shutdown with monitoring, queued/operator work, headed window, restart, stuck pages",
        evidence="real operator runtime (ApplicationRunRuntime); real browser processes",
    )
    app = _AppHarness(directory, simulator, backend=backend, headed=headed, requested=6)
    baseline = main_pids(backend)
    bound = app.settings.shutdown_timeout_seconds * 3 + 15
    rows: list[dict[str, Any]] = []
    runtime = await app.start(create=True)
    try:
        acquired = await until(lambda: app.valid() >= app.requested, timeout=120, interval=0.25)
        result.check("acquisition_reached_target", acquired is not None)
        identities = identity_map(app.database)
        ids = sorted(identities)
        all_queue_ids = [str(qid) for qid in identities.values()]
        new_after = simulator.new_identities

        async def idle() -> dict[str, object]:
            await until(lambda: owned_rows(app.database)["automatic_leases"] == 0, timeout=15)
            return {}

        async def monitoring_slow() -> dict[str, object]:
            simulator.slow_seconds = 3.0
            simulator.slow_ids.update(all_queue_ids)
            mark_all_due(app.database)
            busy = await until(lambda: scheduler_or_zero(runtime) > 0, timeout=15)
            return {"automatic_checks": scheduler_or_zero(runtime), "busy": busy is not None}

        async def operator_headed_monitoring() -> dict[str, object]:
            opened = await open_when_free(runtime, ids[0])
            simulator.slow_seconds = 3.0
            simulator.slow_ids.update(all_queue_ids)
            await runtime.pause_monitoring()
            await until(
                lambda: all(app.row(sid).get("worker_id") is None for sid in ids[1:4]),
                timeout=15,
            )
            for session_id in ids[1:4]:
                with contextlib.suppress(Exception):
                    await runtime.request_action(OperatorActionKind.REFRESH, session_id)
            await until(lambda: len(runtime_actions(runtime)) >= 3, timeout=10)
            mark_all_due(app.database, ids[4:])
            await runtime.resume_monitoring()
            await until(lambda: scheduler_or_zero(runtime) > 0, timeout=15)
            statuses = Counter(action.status.value for action in runtime_actions(runtime))
            return {
                "manual_open": opened.message == "Opened in browser",
                "automatic_checks": scheduler_or_zero(runtime),
                "operator_running": statuses.get(OperatorActionStatus.RUNNING.value, 0),
                "operator_queued": statuses.get(OperatorActionStatus.REQUESTED.value, 0),
            }

        async def browser_restart_in_flight() -> dict[str, object]:
            simulator.slow_seconds = 3.0
            simulator.slow_ids.update(all_queue_ids)
            mark_all_due(app.database)
            await until(lambda: scheduler_or_zero(runtime) > 0, timeout=15)
            killed = [pid for pid in sorted(main_pids(backend) - baseline) if kill_pid(pid)]
            await asyncio.sleep(0.1)
            return {"killed_processes": len(killed), "automatic_checks": scheduler_or_zero(runtime)}

        async def stuck_navigation() -> dict[str, object]:
            simulator.slow_seconds = STUCK_PAGE_SECONDS
            simulator.slow_ids.update(all_queue_ids)
            mark_all_due(app.database, ids[:4])
            with contextlib.suppress(Exception):
                await runtime.request_action(OperatorActionKind.REFRESH, ids[5])
            await until(lambda: scheduler_or_zero(runtime) > 0, timeout=15)
            await asyncio.sleep(1.0)
            return {
                "automatic_checks": scheduler_or_zero(runtime),
                "operator_actions": len(runtime_actions(runtime)),
            }

        combos: list[tuple[str, Callable[[], Awaitable[dict[str, object]]]]] = [
            ("idle", idle),
            ("monitoring_slow_navigation", monitoring_slow),
            ("monitoring_operator_queued_headed_window", operator_headed_monitoring),
            ("browser_restart_in_flight", browser_restart_in_flight),
            ("stuck_navigation_monitoring_and_operator", stuck_navigation),
        ]
        for name, setup in combos:
            if app.runtime is None:
                runtime = await app.start(create=False)
                await until(lambda: bool(main_pids(backend) - baseline), timeout=20)
            print(f"[step] {backend.value} shutdown matrix: {name}", flush=True)
            in_flight = await setup()
            duration = await app.stop()
            simulator.slow_ids.clear()
            simulator.slow_seconds = 3.0
            gone = await until(lambda: not (main_pids(backend) - baseline), timeout=10)
            owners = owned_rows(app.database)
            rows.append(
                {
                    "combination": name,
                    "in_flight": in_flight,
                    "shutdown_seconds": duration,
                    "processes_gone_after_seconds": gone,
                }
            )
            result.check(f"{name}_shutdown_bounded", duration <= bound)
            result.check(f"{name}_zero_browser_processes", gone is not None)
            result.check(
                f"{name}_zero_leases_and_owners",
                owners == {"automatic_leases": 0, "manual_owners": 0},
            )
            result.check(f"{name}_queue_ids_intact", identity_map(app.database) == identities)
        result.check("no_identities_created", simulator.new_identities == new_after)
        result.measurements["shutdown_bound_seconds"] = bound
        result.measurements["combinations"] = rows
    finally:
        simulator.slow_ids.clear()
        simulator.slow_seconds = 3.0
        if app.runtime is not None:
            await app.stop()
    return result.finish()


# --- orchestration ---------------------------------------------------------------------------


def phase7_environment() -> dict[str, object]:
    base = environment()
    base.pop("camoufox_package", None)
    base.pop("camoufox_browser_build", None)
    for name in ("patchright", "playwright", "psutil"):
        with contextlib.suppress(Exception):
            base[name] = version(name)
    return base


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


async def _guarded(
    name: str,
    backend: BrowserBackendName,
    step: Callable[[], Awaitable[ScenarioResult | list[ScenarioResult]]],
) -> list[ScenarioResult]:
    """Run one scenario, record exceptions as FAIL, and assert no process leak afterwards."""

    baseline = main_pids(backend)
    try:
        produced = await step()
        scenarios = produced if isinstance(produced, list) else [produced]
    except Exception as exc:  # noqa: BLE001 - record and continue
        failed = ScenarioResult(
            key=f"{name}_{backend.value}",
            title=name,
            evidence="scenario raised",
            outcome=Outcome.FAIL,
            notes=[_exception_note(exc)],
        )
        failed.checks["completed_without_exception"] = False
        scenarios = [failed]
    leaked = await until(lambda: not (main_pids(backend) - baseline), timeout=10)
    scenarios[-1].check("no_browser_process_leak_after_scenario", leaked is not None)
    for scenario in scenarios:
        scenario.finish()
        _print_scenario(scenario)
    return scenarios


def add_queued_work_check(scenario: ScenarioResult) -> ScenarioResult:
    """The kill-during-monitoring scenario must also have had queued work at the time."""

    sweep = scenario.measurements.get("sweep")
    if isinstance(sweep, dict):
        depth = sweep.get("queue_depth")
        peak = depth.get("peak") if isinstance(depth, dict) else None
        scenario.check("queued_work_existed_during_failure", bool(peak))
    return scenario


def backend_summary(scenarios: Sequence[ScenarioResult], backend: BrowserBackendName) -> dict[str, Any]:
    own = [s for s in scenarios if s.key.endswith(f"_{backend.value}")]
    return {
        "scenarios_passed": sum(s.outcome is Outcome.PASS for s in own),
        "scenarios_failed": sum(s.outcome is Outcome.FAIL for s in own),
        "checks_passed": sum(sum(s.checks.values()) for s in own),
        "checks_failed": sum(len(s.checks) - sum(s.checks.values()) for s in own),
        "failed_checks": {
            s.key: [name for name, passed in s.checks.items() if not passed]
            for s in own
            if not all(s.checks.values())
        },
    }


async def run_phase7_benchmark(
    directory: Path,
    *,
    backends: Sequence[BrowserBackendName],
    single_process_levels: Sequence[int],
    multi_process_levels: Sequence[int],
    navigation_waves: int,
    hold_seconds: float,
    sample_interval_seconds: float,
    churn_levels: Sequence[int],
    churn_cycles: int,
    population_size: int,
    sweeps: int,
    processes: int,
    workers: int,
    restart_cycles: int,
    failure_cycles: int,
    monitor_population: int,
    monitor_profiles: Sequence[MonitoringProfile],
    monitor_seconds: float,
    monitor_interval: float,
    repeated_timeouts: int,
    headed: bool,
    include_concurrency: bool = True,
    only: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    simulator = LocalQueueSimulator(new_identity_prefix="sim-phase7")
    await simulator.start()
    with contextlib.suppress(NotImplementedError, RuntimeError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, _dump_tasks)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "target": "LocalQueueSimulator on 127.0.0.1 (not Queue-it)",
        "staging_status": "NOT RUN / UNKNOWN",
        "candidate": CANDIDATE.value,
        "control": CONTROL.value,
        "excluded_backends": ["camoufox"],
        "environment": phase7_environment(),
        "configuration": {
            "backends": [backend.value for backend in backends],
            "single_process_context_levels": list(single_process_levels),
            "multi_process_context_levels": list(multi_process_levels),
            "navigation_waves": navigation_waves,
            "hold_seconds": hold_seconds,
            "sample_interval_seconds": sample_interval_seconds,
            "navigation_timeout_ms": NAVIGATION_TIMEOUT_MS,
            "step_deadline_seconds": STEP_DEADLINE_SECONDS,
            "health_probe_deadline_seconds": HEALTH_PROBE_DEADLINE_SECONDS,
            "churn_worker_levels": list(churn_levels),
            "churn_cycles_per_level": churn_cycles,
            "park_reopen": {
                "population": population_size,
                "sweeps": sweeps,
                "browser_processes": processes,
                "monitor_workers": workers,
                "full_browser_restart_cycles": restart_cycles,
            },
            "repeated_kill_cycles": failure_cycles,
            "monitoring": {
                "population": monitor_population,
                "profiles": [profile.label for profile in monitor_profiles],
                "duration_seconds": monitor_seconds,
                "poll_interval_seconds": monitor_interval,
            },
            "repeated_navigation_timeouts": repeated_timeouts,
            "headed_manual_pool": headed,
            "architectural_limits": {
                "CHROME_PROCESS_COUNT_max": 4,
                "MAX_CONTEXTS_PER_BROWSER_max": 25,
                "MAX_ACTIVE_CONTEXTS_max": 100,
            },
        },
        "measurement_notes": {
            "cpu": "Process CPU percent where 100 means one logical core.",
            "ram": (
                "Summed RSS of the backend's Chrome process tree (processes named *chrome* "
                "descended from this Python process); shared pages may be counted more than "
                "once. Patchright and the control both drive installed Google Chrome."
            ),
            "throughput": (
                "checks_per_second is a local simulator page rate, not Queue-it throughput "
                "or a staging-safe level."
            ),
            "identity": (
                "Queue ID continuity is compared in memory against the persisted map; no "
                "Queue ID, session ID, transfer URL, or storage state is reported."
            ),
        },
    }
    scenarios: list[ScenarioResult] = []
    try:
        report["environment"]["browser_versions"] = await browser_versions(backends)
        if include_concurrency and (not only or "concurrency" in only):
            report["concurrency"] = await run_concurrency_matrix(
                simulator,
                directory / "concurrency",
                backends=backends,
                single_process_levels=single_process_levels,
                multi_process_levels=multi_process_levels,
                navigation_waves=navigation_waves,
                hold_seconds=hold_seconds,
                sample_interval_seconds=sample_interval_seconds,
            )
        for backend in backends:
            population = PopulationContext(
                database=directory / f"population-{backend.value}.sqlite3",
                state_directory=directory / f"population-{backend.value}-state",
            )
            foreign = foreign_for(backend)
            app_directory = directory / f"app-{backend.value}"
            matrix_directory = directory / f"shutdown-{backend.value}"
            app_directory.mkdir(parents=True, exist_ok=True)
            matrix_directory.mkdir(parents=True, exist_ok=True)

            async def failure_during_monitoring(
                backend: BrowserBackendName = backend,
                population: PopulationContext = population,
            ) -> ScenarioResult:
                return add_queued_work_check(
                    await scenario_failure_during_monitoring(
                        simulator,
                        population,
                        backend=backend,
                        processes=processes,
                        workers=workers,
                    )
                )

            steps: list[
                tuple[str, Callable[[], Awaitable[ScenarioResult | list[ScenarioResult]]]]
            ] = [
                (
                    "churn",
                    partial(
                        scenario_churn,
                        simulator,
                        backend=backend,
                        levels=churn_levels,
                        cycles_per_level=churn_cycles,
                        sample_interval_seconds=sample_interval_seconds,
                    ),
                ),
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
                        foreign_provenance=foreign,
                    ),
                ),
                (
                    "monitoring",
                    partial(
                        scenario_monitoring_workload,
                        simulator,
                        directory,
                        backend=backend,
                        size=monitor_population,
                        profiles=monitor_profiles,
                        duration_seconds=monitor_seconds,
                        poll_interval_seconds=monitor_interval,
                        sample_interval_seconds=1.0,
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
                ("failure_during_monitoring", failure_during_monitoring),
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
                    "restoration_failures",
                    partial(
                        scenario_restoration_failures,
                        simulator,
                        directory,
                        backend=backend,
                        foreign_provenance=foreign,
                    ),
                ),
                (
                    "stuck_navigation",
                    partial(
                        scenario_stuck_navigation,
                        simulator,
                        directory,
                        backend=backend,
                        repeated_timeouts=repeated_timeouts,
                    ),
                ),
                (
                    "manual",
                    partial(
                        scenario_manual_and_shutdown,
                        simulator,
                        app_directory,
                        backend=backend,
                        headed=headed,
                    ),
                ),
                (
                    "shutdown_matrix",
                    partial(
                        scenario_shutdown_matrix,
                        simulator,
                        matrix_directory,
                        backend=backend,
                        headed=headed,
                    ),
                ),
            ]
            for name, step in steps:
                if only and name not in only:
                    continue
                scenarios.extend(await _guarded(name, backend, step))
        report["scenarios"] = [scenario_dict(scenario) for scenario in scenarios]
        report["summary"] = {
            "scenarios_passed": sum(s.outcome is Outcome.PASS for s in scenarios),
            "scenarios_failed": sum(s.outcome is Outcome.FAIL for s in scenarios),
            "checks_passed": sum(sum(s.checks.values()) for s in scenarios),
            "checks_failed": sum(len(s.checks) - sum(s.checks.values()) for s in scenarios),
            "by_backend": {
                backend.value: backend_summary(scenarios, backend) for backend in backends
            },
        }
    finally:
        await simulator.close()
        report["duration_seconds"] = round(time.perf_counter() - started, 1)
        report["leftover_browser_main_processes"] = {
            backend.value: len(main_pids(backend)) for backend in backends
        }
    return report


def _parse_levels(value: str) -> tuple[int, ...]:
    levels = tuple(int(item) for item in value.split(",") if item.strip())
    if any(level < 1 or level > 100 for level in levels):
        raise argparse.ArgumentTypeError("levels must be integers between 1 and 100")
    return levels


def _parse_profiles(value: str) -> tuple[MonitoringProfile, ...]:
    """``processes x workers`` pairs, for example ``2x4,2x8``."""

    profiles = []
    for item in value.split(","):
        if not item.strip():
            continue
        processes, workers = (int(part) for part in item.lower().split("x"))
        if not 1 <= processes <= 4 or not 1 <= workers <= processes * 25:
            raise argparse.ArgumentTypeError("profiles must respect the architectural limits")
        profiles.append(MonitoringProfile(processes=processes, workers=workers))
    return tuple(profiles)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--levels", type=_parse_levels, default=DEFAULT_SINGLE_PROCESS_LEVELS)
    parser.add_argument(
        "--multi-process-levels", type=_parse_levels, default=DEFAULT_MULTI_PROCESS_LEVELS
    )
    parser.add_argument("--no-chrome", action="store_true", help="skip the Chrome control")
    parser.add_argument("--no-concurrency", action="store_true")
    parser.add_argument("--navigation-waves", type=int, default=3)
    parser.add_argument("--hold-seconds", type=float, default=5.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.5)
    parser.add_argument("--churn-levels", type=_parse_levels, default=DEFAULT_CHURN_LEVELS)
    parser.add_argument("--churn-cycles", type=int, default=300)
    parser.add_argument("--population", type=int, default=50)
    parser.add_argument("--sweeps", type=int, default=6)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--restart-cycles", type=int, default=3)
    parser.add_argument("--failure-cycles", type=int, default=20)
    parser.add_argument("--monitor-population", type=int, default=100)
    parser.add_argument("--monitor-profiles", type=_parse_profiles, default="2x4,2x8")
    parser.add_argument("--monitor-seconds", type=float, default=60.0)
    parser.add_argument("--monitor-interval", type=float, default=5.0)
    parser.add_argument("--repeated-timeouts", type=int, default=15)
    parser.add_argument("--headed", action="store_true", help="visible manual browser pool")
    parser.add_argument("--only", default="", help="comma-separated scenario names (debugging)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    backends = [CANDIDATE] if args.no_chrome else [CANDIDATE, CONTROL]
    profiles = (
        _parse_profiles(args.monitor_profiles)
        if isinstance(args.monitor_profiles, str)
        else args.monitor_profiles
    )
    with tempfile.TemporaryDirectory(prefix="phase7-patchright-") as work:
        report = asyncio.run(
            run_phase7_benchmark(
                Path(work),
                backends=backends,
                single_process_levels=args.levels,
                multi_process_levels=args.multi_process_levels,
                navigation_waves=args.navigation_waves,
                hold_seconds=args.hold_seconds,
                sample_interval_seconds=args.sample_interval_seconds,
                churn_levels=args.churn_levels,
                churn_cycles=args.churn_cycles,
                population_size=args.population,
                sweeps=args.sweeps,
                processes=args.processes,
                workers=args.workers,
                restart_cycles=args.restart_cycles,
                failure_cycles=args.failure_cycles,
                monitor_population=args.monitor_population,
                monitor_profiles=profiles,
                monitor_seconds=args.monitor_seconds,
                monitor_interval=args.monitor_interval,
                repeated_timeouts=args.repeated_timeouts,
                headed=args.headed,
                include_concurrency=not args.no_concurrency,
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
