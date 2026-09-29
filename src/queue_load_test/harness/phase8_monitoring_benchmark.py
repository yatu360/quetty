"""Phase 8 Prompt 7: Direct vs Headed Window monitoring benchmark.

The two strategies run **sequentially**, never at the same time against the same
visitors. Each strategy gets a fresh, equivalent persisted population (same count,
same lifecycle stage layout, same browser backend, worker/queue/claim limits, polling
policy, and observation window) on the same host.

``local`` mode targets ``LocalQueueSimulator`` on 127.0.0.1. It is controlled
mechanism evidence and **not** Queue-it evidence; every Queue-it-specific result is
reported NOT RUN / UNKNOWN and never estimated from it.

``staging`` mode is triple-gated (``RUN_STAGING_TESTS=1``,
``RUN_PHASE8_MONITORING_BENCHMARK=1``, ``--confirm-authorized-staging``). It also
requires an authorised target, authorised status discovery, and a reviewed
``authorized_queue_it_staging`` response schema. It uses the configured production
cadence (no benchmark speed-up), observes passively, and injects no faults.

Checks are never made more frequent because direct checks are cheaper. Both
strategies use the same adaptive ``PollingPolicy``, and polling guidance a response
provides is measured and reported.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import itertools
import json
import logging
import os
import secrets
import signal
import statistics
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from queue_load_test.config import Settings
from queue_load_test.direct_monitor import (
    AUTHORIZED_STAGING_SCOPE,
    DirectCapability,
    DirectMonitorStateStore,
)
from queue_load_test.harness.local_queue_simulator import (
    STAGE_ACTIVE,
    STAGE_PRE,
    STAGE_SERVICED,
    LocalQueueSimulator,
)
from queue_load_test.harness.phase5_workflow import Database, _browser_main_pids, until
from queue_load_test.harness.phase8_direct_runtime import direct_settings
from queue_load_test.metrics.logging import JsonLogFormatter
from queue_load_test.models import BrowserBackendName, MonitoringStrategy
from queue_load_test.observation_equivalence import (
    DirectSchemaError,
    load_direct_response_schema,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.utils.instance_lock import InstanceLock
from queue_load_test.web.app import create_app
from queue_load_test.web.service import ApplicationRunRuntime

logger = logging.getLogger(__name__)

HX = {"HX-Request": "true"}
_EXPERIMENT_GATE = "RUN_PHASE8_MONITORING_BENCHMARK"
UNKNOWN = "UNKNOWN"
NOT_RUN = "NOT RUN"
LIFECYCLE_STAGES = (
    "PRE_QUEUE",
    "ACTIVE_QUEUE",
    "PAUSED",
    "SERVICED_SOON",
    "TURN_STARTED",
    "ADMITTED",
)
# Stages the local simulator can present. Everything else is UNKNOWN locally.
_SIMULATED_STAGES = {"PRE_QUEUE": STAGE_PRE, "ACTIVE_QUEUE": STAGE_ACTIVE,
                     "SERVICED_SOON": STAGE_SERVICED}
_DIRECT_ERROR_REASONS = frozenset(
    {"network", "timeout", "unexpected_http_status", "unexpected_redirect"}
)
_SCHEMA_REASONS = frozenset(
    {"schema_incompatible", "malformed_response", "unexpected_content_type"}
)
_IDENTITY_REASONS = frozenset({"identity_mismatch", "identity_ambiguity"})


@dataclass(frozen=True, slots=True)
class BenchmarkProfile:
    """Identical for both strategies; nothing here depends on the strategy."""

    sessions: int = 12
    window_seconds: float = 30.0
    monitor_workers: int = 2
    monitor_queue_capacity: int = 4
    monitor_claim_batch_size: int = 4
    poll_min_seconds: float = 2.0
    poll_max_seconds: float = 3.0
    poll_hint_seconds: float | None = 2.5
    sample_interval_seconds: float = 0.5
    recovery: bool = True

    @property
    def cadence_floor_seconds(self) -> float:
        """The fastest per-session interval the shared polling policy allows."""

        return self.poll_min_seconds


QUICK_PROFILE = BenchmarkProfile(sessions=6, window_seconds=14.0, poll_hint_seconds=2.5)


# --------------------------------------------------------------------- sampling


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no data."""

    if not values:
        return None
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-fraction * len(ordered) // 1))))
    return ordered[rank - 1]


def summarize(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _round(percentile(values, 0.5)),
        "p95": _round(percentile(values, 0.95)),
        "max": _round(max(values) if values else None),
        "mean": _round(statistics.fmean(values) if values else None),
    }


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


class _EventCapture(logging.Handler):
    """Keep sanitized structured contexts and formatted lines for leak checks."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonLogFormatter())
        self.events: list[tuple[float, str, dict[str, object]]] = []
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        context = getattr(record, "observability_context", {})
        self.events.append(
            (time.monotonic(), record.getMessage(), dict(context) if isinstance(context, dict)
             else {})
        )
        self.lines.append(self.format(record))

    def between(self, start: float, end: float, message: str) -> list[dict[str, object]]:
        return [
            context
            for stamp, name, context in self.events
            if name == message and start <= stamp <= end
        ]


@dataclass(slots=True)
class _Samples:
    backlog: list[int] = field(default_factory=list)
    oldest_overdue: list[float] = field(default_factory=list)
    contexts: list[int] = field(default_factory=list)
    app_cpu: list[float] = field(default_factory=list)
    app_rss: list[int] = field(default_factory=list)
    browser_cpu: list[float] = field(default_factory=list)
    browser_rss: list[int] = field(default_factory=list)


class _ResourceSampler:
    """Periodically sample backlog, BrowserContexts, and process CPU/RSS."""

    def __init__(
        self,
        *,
        repository: SQLiteSessionRepository,
        runtime: ApplicationRunRuntime,
        interval: float,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._interval = interval
        self.samples = _Samples()
        self._task: asyncio.Task[None] | None = None
        try:
            self._psutil: Any = importlib.import_module("psutil")
            self._process: Any = self._psutil.Process(os.getpid())
        except ImportError:  # pragma: no cover - benchmark extra
            self._psutil = None
            self._process = None
        self._children: dict[int, Any] = {}

    def start(self) -> None:
        if self._process is not None:
            self._process.cpu_percent(None)
        self._task = asyncio.create_task(self._loop(), name="phase8-benchmark-sampler")

    async def stop(self) -> _Samples:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        return self.samples

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            now = datetime.now(UTC)
            with contextlib.suppress(Exception):
                due = await self._repository.due_session_summary(now=now)
                self.samples.backlog.append(due.count)
                self.samples.oldest_overdue.append(due.oldest_overdue_seconds(now=now))
            with contextlib.suppress(Exception):
                self.samples.contexts.append((await self._runtime.capacity()).active_contexts)
            if self._process is not None:
                self._sample_processes()

    def _sample_processes(self) -> None:
        psutil = self._psutil
        with contextlib.suppress(psutil.Error):
            self.samples.app_cpu.append(float(self._process.cpu_percent(None)))
            self.samples.app_rss.append(int(self._process.memory_info().rss))
        cpu = 0.0
        rss = 0
        live: set[int] = set()
        try:
            children = self._process.children(recursive=True)
        except psutil.Error:
            children = []
        for child in children:
            live.add(child.pid)
            tracked = self._children.setdefault(child.pid, child)
            try:
                if tracked is child:
                    child.cpu_percent(None)  # prime; the first reading is meaningless
                    continue
                cpu += float(tracked.cpu_percent(None))
                rss += int(tracked.memory_info().rss)
            except psutil.Error:
                continue
        for pid in set(self._children) - live:
            self._children.pop(pid, None)
        self.samples.browser_cpu.append(cpu)
        self.samples.browser_rss.append(rss)


# ------------------------------------------------------------------ per strategy


@dataclass(slots=True)
class _Target:
    """What the benchmark can control: the simulator locally, nothing on staging."""

    url: str
    simulator: LocalQueueSimulator | None


def _benchmark_settings(
    directory: Path,
    *,
    database: Path,
    backend: BrowserBackendName,
    profile: BenchmarkProfile,
    base: Settings | None,
) -> Settings:
    if base is not None:
        # Staging: the configured production cadence and limits, untouched.
        return base.model_copy(
            update={
                "database_url": f"sqlite:///{database}",
                "state_directory": directory / "state",
                "direct_monitor_directory": directory / "direct-monitor",
                "status_discovery_directory": directory / "status-discovery",
                "browser_backend": backend,
            }
        )
    interval = {
        "queue_poll_seconds": profile.poll_min_seconds,
        "poll_jitter_seconds": 0.0,
        "pre_queue_poll_min_seconds": profile.poll_min_seconds,
        "pre_queue_poll_max_seconds": profile.poll_max_seconds,
        "active_early_poll_min_seconds": profile.poll_min_seconds,
        "active_early_poll_max_seconds": profile.poll_max_seconds,
        "active_mid_poll_min_seconds": profile.poll_min_seconds,
        "active_mid_poll_max_seconds": profile.poll_max_seconds,
        "serviced_soon_poll_min_seconds": profile.poll_min_seconds,
        "serviced_soon_poll_max_seconds": profile.poll_max_seconds,
    }
    return direct_settings(directory, database=database, backend=backend).model_copy(
        update={
            **interval,
            "monitor_workers": profile.monitor_workers,
            "monitor_queue_capacity": profile.monitor_queue_capacity,
            "monitor_claim_batch_size": profile.monitor_claim_batch_size,
            "max_manual_requested_sessions": max(50, profile.sessions),
            "direct_monitor_readopt_cooldown_seconds": 2.0,
        }
    )


async def run_strategy(
    strategy: MonitoringStrategy,
    directory: Path,
    *,
    target: _Target,
    backend: BrowserBackendName,
    profile: BenchmarkProfile,
    secret: str,
    base_settings: Settings | None = None,
) -> dict[str, Any]:
    """Run one strategy end to end and return aggregate, sanitized measurements."""

    directory.mkdir(parents=True, exist_ok=True)
    database_path = directory / "benchmark.sqlite3"
    db = Database(database_path)
    settings = _benchmark_settings(
        directory, database=database_path, backend=backend, profile=profile, base=base_settings
    )
    simulator = target.simulator
    capture = _EventCapture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    result: dict[str, Any] = {"strategy": strategy.value}

    def build() -> tuple[Any, ApplicationRunRuntime, SQLiteSessionRepository]:
        repository = SQLiteSessionRepository(database_path)
        runtime = ApplicationRunRuntime(
            settings=settings, repository=repository, manual_headless=True
        )
        app = create_app(
            settings=settings,
            repository=repository,
            runtime=runtime,
            instance_lock=InstanceLock.for_database(database_path),
        )
        return app, runtime, repository

    try:
        app, runtime, repository = build()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
                created_before = simulator.new_identities if simulator is not None else None
                response = await ui.post(
                    "/setup",
                    data={
                        "target_url": target.url,
                        "requested_sessions": str(profile.sessions),
                        "monitoring_strategy": strategy.value,
                    },
                )
                if response.status_code != 303:
                    raise RuntimeError(f"benchmark setup rejected ({response.status_code})")
                started = time.perf_counter()
                acquired = await until(
                    lambda: db.valid() == profile.sessions, timeout=180, interval=0.25
                )
                result["acquisition_seconds"] = round(time.perf_counter() - started, 2)
                if acquired is None:
                    raise RuntimeError("benchmark population was not acquired")
                session_ids = sorted(db.identities())
                identities_before = db.identities()
                expected = _assign_stages(simulator, db, session_ids)

                # Direct needs one legitimate browser observation per session before
                # it can go direct. This warm-up is measured and kept out of the window.
                warmup_started = time.perf_counter()
                if strategy is MonitoringStrategy.DIRECT:
                    store = DirectMonitorStateStore(settings.direct_monitor_directory)
                    await until(
                        lambda: _all_capable(store, session_ids), timeout=90, interval=0.25
                    )
                result["direct_warmup_seconds"] = (
                    round(time.perf_counter() - warmup_started, 2)
                    if strategy is MonitoringStrategy.DIRECT
                    else None
                )

                checkpoints: dict[str, list[bool]] = {}
                window = await _observe_window(
                    runtime=runtime,
                    repository=repository,
                    db=db,
                    simulator=simulator,
                    capture=capture,
                    profile=profile,
                    session_ids=session_ids,
                    expected=expected,
                    checkpoints=checkpoints,
                )
                result["window"] = window
                result["lifecycle"] = await _lifecycle(db, expected, simulator, checkpoints)
                exposition = (await ui.get("/metrics")).text
                result["state_refresh_failures"] = _sample_value(
                    exposition, "state_refresh_failures_total"
                )
                if profile.recovery and simulator is not None:
                    result["recovery"] = await _recovery(
                        strategy=strategy,
                        ui=ui,
                        runtime=runtime,
                        db=db,
                        simulator=simulator,
                        backend=backend,
                        session_ids=session_ids,
                        settings=settings,
                    )
                result["identity"] = {
                    "queue_ids_changed": sum(
                        1
                        for key, value in identities_before.items()
                        if db.identities().get(key) != value
                    ),
                    "new_identities_during_monitoring": (
                        simulator.new_identities - (created_before or 0) - profile.sessions
                        if simulator is not None
                        else None
                    ),
                }
                surfaces = [
                    exposition,
                    (await ui.get("/dashboard")).text,
                    (await ui.get("/partials/sessions")).text,
                ]
                survivors = db.identities()
        if profile.recovery:
            result.setdefault("recovery", {})["restart"] = await _restart(
                build=build, db=db, simulator=simulator, strategy=strategy, survivors=survivors
            )
        surfaces.append(database_path.read_bytes().decode("latin-1"))
        result["secret_in_surfaces"] = any(secret in text for text in surfaces)
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)
    result["secret_in_logs"] = secret in "\n".join(capture.lines)
    return result


def _assign_stages(
    simulator: LocalQueueSimulator | None, db: Database, session_ids: list[str]
) -> dict[str, str]:
    """Deterministic, strategy-independent layout using only legal transitions.

    Visitors join in PRE_QUEUE. For the first half of the window one third stays
    PRE_QUEUE and the rest are ACTIVE_QUEUE; at mid-window PRE -> ACTIVE and one
    third ACTIVE -> SERVICED_SOON (see ``_advance_stages``).
    """

    if simulator is None:
        return {}
    expected: dict[str, str] = {}
    identities = db.identities()
    for index, session_id in enumerate(session_ids):
        queue_id = identities[session_id]
        assert queue_id is not None
        stage = "PRE_QUEUE" if index % 3 == 0 else "ACTIVE_QUEUE"
        simulator.stages[queue_id] = _SIMULATED_STAGES[stage]
        simulator.progress[queue_id] = 20 + index
        expected[session_id] = stage
    return expected


def _advance_stages(
    simulator: LocalQueueSimulator, db: Database, session_ids: list[str], expected: dict[str, str]
) -> None:
    identities = db.identities()
    for index, session_id in enumerate(session_ids):
        queue_id = identities[session_id]
        assert queue_id is not None
        if index % 3 == 0:
            simulator.stages[queue_id] = STAGE_ACTIVE
            expected[session_id] = "ACTIVE_QUEUE"
        elif index % 3 == 2:
            simulator.stages[queue_id] = STAGE_SERVICED
            expected[session_id] = "SERVICED_SOON"
        simulator.progress[queue_id] = simulator.progress.get(queue_id, 20) + 30


def _all_capable(store: DirectMonitorStateStore, session_ids: list[str]) -> bool:
    for session_id in session_ids:
        try:
            text = store.path_for(session_id).read_text(encoding="utf-8")
        except OSError:
            return False
        if f'"capability":"{DirectCapability.DIRECT_CAPABLE.value}"' not in text:
            return False
    return True


async def _observe_window(
    *,
    runtime: ApplicationRunRuntime,
    repository: SQLiteSessionRepository,
    db: Database,
    simulator: LocalQueueSimulator | None,
    capture: _EventCapture,
    profile: BenchmarkProfile,
    session_ids: list[str],
    expected: dict[str, str],
    checkpoints: dict[str, list[bool]],
) -> dict[str, Any]:
    direct_before = _direct_snapshot(runtime)
    requests_before = _request_snapshot(simulator)
    times_before = (
        {key: len(value) for key, value in simulator.direct_request_times.items()}
        if simulator is not None
        else {}
    )
    sampler = _ResourceSampler(
        repository=repository, runtime=runtime, interval=profile.sample_interval_seconds
    )
    start = time.monotonic()
    sampler.start()
    await asyncio.sleep(profile.window_seconds / 2)
    if simulator is not None:
        # Checkpoint the first half's stages, then advance them for the second half.
        for session_id, stage in expected.items():
            checkpoints.setdefault(stage, []).append(
                (db.session(session_id) or {}).get("status") == stage
            )
        _advance_stages(simulator, db, session_ids, expected)
    await asyncio.sleep(profile.window_seconds / 2)
    end = time.monotonic()
    samples = await sampler.stop()
    elapsed = end - start

    checks = capture.between(start, end, "queue_check_completed")
    failures = capture.between(start, end, "queue_check_failed")
    durations = [_number(item.get("duration")) for item in checks if "duration" in item]
    direct_durations = [
        _number(item.get("duration"))
        for item in checks
        if item.get("operation") == "direct" and "duration" in item
    ]
    browser_durations = [
        _number(item.get("duration"))
        for item in checks
        if item.get("operation") != "direct" and "duration" in item
    ]
    first_seen: dict[str, float] = {}
    for stamp, name, context in capture.events:
        seen_id = context.get("session_id")
        if (
            name == "queue_check_completed"
            and start <= stamp <= end
            and isinstance(seen_id, str)
            and seen_id not in first_seen
        ):
            first_seen[seen_id] = stamp - start
    sweep = (
        max(first_seen.values()) if len(first_seen) >= len(session_ids) and first_seen else None
    )
    direct = _direct_delta(direct_before, _direct_snapshot(runtime))
    requests = _request_delta(requests_before, _request_snapshot(simulator))
    gaps = _direct_gaps(simulator, times_before)
    minutes = elapsed / 60
    sessions = max(1, len(session_ids))
    return {
        "seconds": round(elapsed, 2),
        "monitoring_attempts": len(checks) + len(failures),
        "successful_observations": sum(
            1 for item in checks if item.get("status") not in {None, "CONNECTION_LOST"}
        ),
        "checks_per_second": round(len(checks) / elapsed, 4) if elapsed else None,
        "check_duration_seconds": summarize(durations),
        "direct_check_duration_seconds": summarize(direct_durations),
        "browser_check_duration_seconds": summarize(browser_durations),
        "first_full_sweep_seconds": _round(sweep, 2),
        "due_backlog": {
            "max": max(samples.backlog, default=None),
            "mean": _round(statistics.fmean(samples.backlog) if samples.backlog else None, 2),
        },
        "oldest_overdue_seconds": {
            "max": _round(max(samples.oldest_overdue, default=0.0), 2),
            "p95": _round(percentile(samples.oldest_overdue, 0.95), 2),
        },
        "browser_context_peak": max(samples.contexts, default=None),
        "browser_context_mean": _round(
            statistics.fmean(samples.contexts) if samples.contexts else None, 3
        ),
        "app_cpu_percent": summarize(samples.app_cpu),
        "app_rss_mib_peak": _round(max(samples.app_rss, default=0) / 2**20, 1),
        "browser_cpu_percent": summarize(samples.browser_cpu),
        "browser_rss_mib_peak": _round(max(samples.browser_rss, default=0) / 2**20, 1),
        "direct": direct,
        "visitor_status_requests_per_session_per_minute": (
            {
                "direct": round(requests["direct"] / sessions / minutes, 3),
                "browser_page": round(requests["browser"] / sessions / minutes, 3),
                "page_navigations": round(requests["pages"] / sessions / minutes, 3),
            }
            if requests is not None and minutes
            else None
        ),
        "direct_request_gap_seconds": gaps,
    }


def _direct_snapshot(runtime: ApplicationRunRuntime) -> dict[str, Any] | None:
    metrics = runtime.direct_monitoring_metrics
    if metrics is None:
        return None
    return {
        "checks": metrics.checks,
        "direct_requests": metrics.direct_requests,
        "direct_successes": metrics.direct_successes,
        "browser_fallbacks": metrics.browser_fallbacks,
        "fallback_successes": metrics.fallback_successes,
        "disagreements": metrics.disagreements,
        "recipes_adopted": metrics.recipes_adopted,
        "poll_hints_observed": metrics.poll_hints_observed,
        "poll_hint_min_seconds": metrics.poll_hint_min_seconds,
        "poll_hint_max_seconds": metrics.poll_hint_max_seconds,
        "reasons": dict(metrics.fallback_reasons),
    }


def _direct_delta(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, Any] | None:
    if before is None or after is None:
        return None
    counters = (
        "checks",
        "direct_requests",
        "direct_successes",
        "browser_fallbacks",
        "fallback_successes",
        "disagreements",
        "recipes_adopted",
        "poll_hints_observed",
    )
    delta: dict[str, Any] = {name: after[name] - before[name] for name in counters}
    reasons = {
        name: after["reasons"].get(name, 0) - before["reasons"].get(name, 0)
        for name in after["reasons"]
        if after["reasons"].get(name, 0) - before["reasons"].get(name, 0)
    }
    delta["fallback_reasons"] = dict(sorted(reasons.items()))
    delta["direct_errors"] = sum(reasons.get(name, 0) for name in _DIRECT_ERROR_REASONS)
    delta["schema_failures"] = sum(reasons.get(name, 0) for name in _SCHEMA_REASONS)
    delta["identity_mismatches"] = sum(reasons.get(name, 0) for name in _IDENTITY_REASONS)
    checks = delta["checks"]
    delta["direct_success_rate"] = (
        round(delta["direct_successes"] / checks, 4) if checks else None
    )
    delta["browser_fallback_rate"] = (
        round(delta["browser_fallbacks"] / checks, 4) if checks else None
    )
    delta["browser_restores_avoided"] = delta["direct_successes"]
    delta["poll_hint_seconds"] = {
        "min": after["poll_hint_min_seconds"],
        "max": after["poll_hint_max_seconds"],
    }
    return delta


def _request_snapshot(simulator: LocalQueueSimulator | None) -> dict[str, int] | None:
    if simulator is None:
        return None
    return {
        "direct": sum(simulator.direct_status_requests.values()),
        "browser": sum(simulator.browser_status_requests.values()),
        "pages": sum(simulator.page_requests.values()),
    }


def _request_delta(
    before: dict[str, int] | None, after: dict[str, int] | None
) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    return {key: after[key] - before[key] for key in after}


def _direct_gaps(
    simulator: LocalQueueSimulator | None, before: Mapping[str, int]
) -> dict[str, float | int | None] | None:
    if simulator is None:
        return None
    gaps: list[float] = []
    for queue_id, stamps in simulator.direct_request_times.items():
        window = stamps[before.get(queue_id, 0):]
        gaps.extend(later - earlier for earlier, later in itertools.pairwise(window))
    return {"count": len(gaps), "min": _round(min(gaps, default=None) if gaps else None),
            "p50": _round(percentile(gaps, 0.5))}


async def _lifecycle(
    db: Database,
    expected: dict[str, str],
    simulator: LocalQueueSimulator | None,
    checkpoints: Mapping[str, list[bool]],
) -> dict[str, Any]:
    """Final persisted status versus the simulator's presented stage, per stage."""

    if simulator is None:
        observed = {row[0] for row in db.rows("SELECT DISTINCT status FROM queue_sessions")}
        return {
            stage: ("OBSERVED" if stage in observed else UNKNOWN) for stage in LIFECYCLE_STAGES
        }

    def converged() -> bool:
        return all(
            (db.session(session_id) or {}).get("status") == stage
            for session_id, stage in expected.items()
        )

    await until(converged, timeout=15, interval=0.25)
    by_stage: dict[str, list[bool]] = {stage: list(values) for stage, values in checkpoints.items()}
    for session_id, stage in expected.items():
        by_stage.setdefault(stage, []).append(
            (db.session(session_id) or {}).get("status") == stage
        )
    result: dict[str, Any] = {}
    for stage in LIFECYCLE_STAGES:
        outcomes = by_stage.get(stage)
        if stage not in _SIMULATED_STAGES or not outcomes:
            result[stage] = UNKNOWN
        else:
            result[stage] = "PASS" if all(outcomes) else "FAIL"
    return result


async def _recovery(
    *,
    strategy: MonitoringStrategy,
    ui: httpx.AsyncClient,
    runtime: ApplicationRunRuntime,
    db: Database,
    simulator: LocalQueueSimulator,
    backend: BrowserBackendName,
    session_ids: list[str],
    settings: Settings,
) -> dict[str, Any]:
    identities = db.identities()
    first, second, third = session_ids[0], session_ids[1], session_ids[2]
    result: dict[str, Any] = {}

    # Pause/resume: nothing automatic while paused; measure resume latency.
    await ui.post("/monitoring/pause", headers=HX)
    await until(lambda: db.owners() == 0, timeout=20)
    counts = _request_snapshot(simulator)
    checked = {sid: db.checked_at(sid) for sid in session_ids}
    await asyncio.sleep(3.0)
    quiet = _request_snapshot(simulator) == counts and all(
        db.checked_at(sid) == checked[sid] for sid in session_ids
    )
    resumed = time.perf_counter()
    await ui.post("/monitoring/resume", headers=HX)
    latency = await until(
        lambda: any(db.checked_at(sid) != checked[sid] for sid in session_ids), timeout=20
    )
    result["pause_resume"] = {
        "no_checks_while_paused": quiet,
        "resume_to_first_check_seconds": latency,
        "resume_measured_seconds": round(time.perf_counter() - resumed, 2),
    }

    # Manual Open coexistence: the open session is fenced, the others keep going.
    await until(lambda: (db.session(first) or {}).get("worker_id") is None, timeout=20)
    await ui.post(f"/sessions/{first}/open", headers=HX)
    opened = await until(
        lambda: (db.session(first) or {}).get("manual_owner_id") is not None, timeout=30
    )
    queue_first = identities[first]
    assert queue_first is not None
    direct_first = simulator.direct_status_requests[queue_first]
    others = {sid: db.checked_at(sid) for sid in session_ids[1:]}
    await asyncio.sleep(4.0)
    result["manual_open"] = {
        "opened": opened is not None,
        "open_session_not_polled_directly": (
            simulator.direct_status_requests[queue_first] == direct_first
        ),
        "other_sessions_checked": sum(
            1 for sid, value in others.items() if db.checked_at(sid) != value
        ),
    }
    await ui.post(f"/sessions/{first}/close", headers=HX)
    await until(lambda: (db.session(first) or {}).get("manual_owner_id") is None, timeout=30)

    # Browser failure during a check (a fallback check under Direct).
    queue_second = identities[second]
    assert queue_second is not None
    if strategy is MonitoringStrategy.DIRECT:
        simulator.status_faults[queue_second] = "http_500"
    before_pages = simulator.page_requests[queue_second]
    await until(lambda: simulator.page_requests[queue_second] > before_pages, timeout=30)
    victims = _browser_main_pids(backend)
    for pid in victims:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    killed_at = time.perf_counter()
    mark = db.checked_at(second)
    recovered = await until(
        lambda: db.checked_at(second) != mark
        and (db.session(second) or {}).get("status") not in {"CONNECTION_LOST", "FAILED"},
        timeout=60,
        interval=0.25,
    )
    simulator.status_faults.pop(queue_second, None)
    result["browser_failure"] = {
        "killed_processes": len(victims),
        "recovered_seconds": recovered,
        "measured_seconds": round(time.perf_counter() - killed_at, 2),
        "identity_preserved": db.identities()[second] == queue_second,
    }

    if strategy is MonitoringStrategy.DIRECT:
        store = DirectMonitorStateStore(settings.direct_monitor_directory)
        queue_third = identities[third]
        assert queue_third is not None
        # Direct-state expiry: the server rejects the visitor state for direct only.
        simulator.status_faults[queue_third] = "rejected"
        rejected = await until(
            lambda: not _all_capable(store, [third]), timeout=30, interval=0.25
        )
        simulator.status_faults.pop(queue_third, None)
        refreshed_at = time.perf_counter()
        refreshed = await until(
            lambda: _all_capable(store, [third]), timeout=60, interval=0.25
        )
        direct_mark = simulator.direct_status_requests[queue_third]
        direct_again = await until(
            lambda: simulator.direct_status_requests[queue_third] > direct_mark, timeout=30
        )
        result["direct_state_expiry"] = {
            "detected": rejected is not None,
            "recipe_refreshed_seconds": refreshed,
            "measured_seconds": round(time.perf_counter() - refreshed_at, 2),
            "direct_resumed": direct_again is not None,
            "readopt_cooldown_seconds": settings.direct_monitor_readopt_cooldown_seconds,
        }
    return result


async def _restart(
    *,
    build: Callable[[], tuple[Any, ApplicationRunRuntime, SQLiteSessionRepository]],
    db: Database,
    simulator: LocalQueueSimulator | None,
    strategy: MonitoringStrategy,
    survivors: dict[str, str | None],
) -> dict[str, Any]:
    app, _runtime, _repository = build()
    pages_before = sum(simulator.page_requests.values()) if simulator is not None else None
    direct_before = sum(simulator.direct_status_requests.values()) if simulator is not None else 0
    checked = {sid: db.checked_at(sid) for sid in survivors}
    started = time.perf_counter()
    async with app.router.lifespan_context(app):
        first_check: float | None = None
        first_direct: float | None = None
        wants_direct = strategy is MonitoringStrategy.DIRECT and simulator is not None
        deadline = started + 60
        while time.perf_counter() < deadline and (
            first_check is None or (wants_direct and first_direct is None)
        ):
            elapsed = round(time.perf_counter() - started, 2)
            if first_check is None and any(
                db.checked_at(sid) != checked[sid] for sid in survivors
            ):
                first_check = elapsed
            if (
                wants_direct
                and first_direct is None
                and simulator is not None
                and sum(simulator.direct_status_requests.values()) > direct_before
            ):
                first_direct = elapsed
            await asyncio.sleep(0.05)
        strategy_kept = db.value("SELECT monitoring_strategy FROM run_config") == strategy.value
        await asyncio.sleep(1.0)
    return {
        "restart_to_first_check_seconds": first_check,
        "restart_to_first_direct_request_seconds": first_direct,
        "measured_seconds": round(time.perf_counter() - started, 2),
        "strategy_kept": strategy_kept,
        "identities_preserved": db.identities() == survivors,
        "page_navigations_after_restart": (
            sum(simulator.page_requests.values()) - pages_before
            if simulator is not None and pages_before is not None
            else None
        ),
    }


def _sample_value(exposition: str, name: str) -> float:
    for line in exposition.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[1])
    return 0.0


# ---------------------------------------------------------------------- report


def build_report(
    *,
    mode: str,
    backend: str,
    profile: BenchmarkProfile,
    headed: Mapping[str, Any],
    direct: Mapping[str, Any],
    staging: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministically derive comparisons and gates from two strategy results."""

    hw, dw = headed.get("window", {}), direct.get("window", {})
    dd = dw.get("direct") or {}
    gates = {
        "direct_identity_safety": _gate_identity(headed, direct),
        "observation_equivalence": _gate_equivalence(headed, direct, dd),
        "fallback_reliability": _gate_fallback(direct, dd),
        "direct_request_stability": _gate_stability(dd),
        "restart_continuity": _gate_restart(headed, direct),
        "throughput_improvement": _compare_lower(
            _dig(hw, "check_duration_seconds", "p95"),
            _dig(dw, "check_duration_seconds", "p95"),
            "p95 check duration (s)",
        ),
        "browser_context_reduction": _compare_lower(
            hw.get("browser_context_mean"), dw.get("browser_context_mean"), "mean contexts"
        ),
        "cpu_ram_effect": _gate_resources(hw, dw),
        "request_cadence_safety": _gate_cadence(dw, dd, profile),
        "sensitive_data_safety": _gate_secrets(headed, direct),
    }
    scope = "local_simulator_only" if mode == "local" else "authorized_queue_it_staging"
    return {
        "schema_version": 1,
        "scope": scope,
        "queue_it_contacted": mode == "staging",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "browser_backend": backend,
        "profile": {
            "sessions_per_strategy": profile.sessions,
            "window_seconds": profile.window_seconds,
            "monitor_workers": profile.monitor_workers,
            "monitor_queue_capacity": profile.monitor_queue_capacity,
            "monitor_claim_batch_size": profile.monitor_claim_batch_size,
            "poll_interval_seconds": [profile.poll_min_seconds, profile.poll_max_seconds],
            "response_poll_hint_seconds": profile.poll_hint_seconds,
            "strategies_run": "sequential, separate equivalent populations",
        },
        "headed_window": dict(headed),
        "direct": dict(direct),
        "gates": gates,
        "queue_it_staging": dict(staging),
        "default_strategy_changed": False,
    }


def _dig(mapping: Mapping[str, Any], *keys: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _gate(result: str, basis: str, **values: Any) -> dict[str, Any]:
    return {"result": result, "basis": basis, **values}


def _gate_identity(headed: Mapping[str, Any], direct: Mapping[str, Any]) -> dict[str, Any]:
    changed = [_dig(item, "identity", "queue_ids_changed") for item in (headed, direct)]
    created = [_dig(item, "identity", "new_identities_during_monitoring") for item in (headed, direct)]
    restart = [_dig(item, "recovery", "restart", "identities_preserved") for item in (headed, direct)]
    if any(value is None for value in changed):
        return _gate(UNKNOWN, "identity counts unavailable")
    safe = all(value == 0 for value in changed) and all(
        value in (0, None) for value in created
    ) and all(value in (True, None) for value in restart)
    return _gate(
        "PASS" if safe else "FAIL",
        "no Queue ID changed or was reacquired during monitoring, recovery, or restart",
        queue_ids_changed=changed,
        new_identities=created,
    )


def _gate_equivalence(
    headed: Mapping[str, Any], direct: Mapping[str, Any], dd: Mapping[str, Any]
) -> dict[str, Any]:
    h, d = headed.get("lifecycle", {}), direct.get("lifecycle", {})
    stages = {stage: {"headed": h.get(stage, UNKNOWN), "direct": d.get(stage, UNKNOWN)}
              for stage in LIFECYCLE_STAGES}
    known = [value for value in stages.values() if UNKNOWN not in value.values()]
    if not known:
        return _gate(UNKNOWN, "no lifecycle stage observable in both strategies", stages=stages)
    agree = all(value["headed"] == value["direct"] == "PASS" for value in known)
    disagreements = dd.get("disagreements", 0) or 0
    return _gate(
        "PASS" if agree and not disagreements else "FAIL",
        "final persisted lifecycle matches the presented stage for every observable stage; "
        "unobservable stages stay UNKNOWN",
        stages=stages,
        direct_browser_disagreements=disagreements,
    )


def _gate_fallback(direct: Mapping[str, Any], dd: Mapping[str, Any]) -> dict[str, Any]:
    fallbacks, successes = dd.get("browser_fallbacks"), dd.get("fallback_successes")
    recovery = direct.get("recovery", {})
    expiry = recovery.get("direct_state_expiry", {})
    browser_failure = recovery.get("browser_failure", {})
    if not fallbacks and not expiry:
        return _gate(UNKNOWN, "no fallback exercised")
    ok = (
        (not fallbacks or successes == fallbacks)
        and expiry.get("direct_resumed", True) is True
        and browser_failure.get("recovered_seconds", 0) is not None
    )
    return _gate(
        "PASS" if ok else "FAIL",
        "every window fallback produced a browser observation; expiry and browser failure "
        "recovered",
        window_fallbacks=fallbacks,
        window_fallback_successes=successes,
    )


def _gate_stability(dd: Mapping[str, Any]) -> dict[str, Any]:
    rate = dd.get("direct_success_rate")
    if rate is None:
        return _gate(UNKNOWN, "no direct checks in the window")
    errors = (dd.get("direct_errors") or 0) + (dd.get("schema_failures") or 0)
    return _gate(
        "PASS" if rate >= 0.95 and errors == 0 else "FAIL",
        "steady-state window without injected faults: success rate >= 0.95 and no errors",
        direct_success_rate=rate,
        errors=errors,
    )


def _gate_restart(headed: Mapping[str, Any], direct: Mapping[str, Any]) -> dict[str, Any]:
    h = _dig(headed, "recovery", "restart") or {}
    d = _dig(direct, "recovery", "restart") or {}
    if not h or not d:
        return _gate(UNKNOWN, "restart not exercised")
    ok = all(
        item.get("strategy_kept") and item.get("identities_preserved")
        and item.get("restart_to_first_check_seconds") is not None
        for item in (h, d)
    ) and d.get("restart_to_first_direct_request_seconds") is not None
    return _gate(
        "PASS" if ok else "FAIL",
        "both strategies resume from persisted state; Direct resumes direct without "
        "rediscovery",
        headed_first_check_seconds=h.get("restart_to_first_check_seconds"),
        direct_first_direct_request_seconds=d.get("restart_to_first_direct_request_seconds"),
    )


def _compare_lower(headed: Any, direct: Any, measure: str) -> dict[str, Any]:
    if headed is None or direct is None:
        return _gate(UNKNOWN, f"{measure} unavailable")
    result = "IMPROVED" if direct < headed else "NOT_IMPROVED"
    ratio = round(direct / headed, 4) if headed else None
    return _gate(result, f"{measure}: lower is better", headed=headed, direct=direct,
                 direct_to_headed_ratio=ratio)


def _gate_resources(hw: Mapping[str, Any], dw: Mapping[str, Any]) -> dict[str, Any]:
    values = {
        "browser_cpu_percent_mean": (
            _dig(hw, "browser_cpu_percent", "mean"),
            _dig(dw, "browser_cpu_percent", "mean"),
        ),
        "browser_rss_mib_peak": (hw.get("browser_rss_mib_peak"), dw.get("browser_rss_mib_peak")),
        "app_cpu_percent_mean": (
            _dig(hw, "app_cpu_percent", "mean"),
            _dig(dw, "app_cpu_percent", "mean"),
        ),
        "app_rss_mib_peak": (hw.get("app_rss_mib_peak"), dw.get("app_rss_mib_peak")),
    }
    if any(pair[0] is None or pair[1] is None for pair in values.values()):
        return _gate(UNKNOWN, "resource samples unavailable (psutil)")
    browser_cpu = values["browser_cpu_percent_mean"]
    return _gate(
        "REDUCED" if browser_cpu[1] < browser_cpu[0] else "NOT_REDUCED",
        "mean browser CPU during the window; RSS and app values reported alongside",
        **{name: {"headed": pair[0], "direct": pair[1]} for name, pair in values.items()},
    )


def _gate_cadence(
    dw: Mapping[str, Any], dd: Mapping[str, Any], profile: BenchmarkProfile
) -> dict[str, Any]:
    gaps = dw.get("direct_request_gap_seconds") or {}
    minimum = gaps.get("min")
    if minimum is None:
        return _gate(UNKNOWN, "no per-session direct request gaps measured")
    # Direct uses the same PollingPolicy as the browser path; the floor allows for
    # scheduler tick granularity only, never a faster configured cadence.
    floor = profile.cadence_floor_seconds * 0.8
    hint = _dig(dd, "poll_hint_seconds", "min")
    faster_than_hint = hint is not None and minimum < hint * 0.8
    return _gate(
        "PASS" if minimum >= floor else "FAIL",
        "minimum per-session direct request gap versus the shared polling floor; response "
        "polling guidance is reported, never used to poll faster",
        min_gap_seconds=minimum,
        p50_gap_seconds=gaps.get("p50"),
        policy_floor_seconds=profile.cadence_floor_seconds,
        response_poll_hint_min_seconds=hint,
        direct_faster_than_response_hint=faster_than_hint,
    )


def _gate_secrets(headed: Mapping[str, Any], direct: Mapping[str, Any]) -> dict[str, Any]:
    flags = [
        item.get(name) for item in (headed, direct)
        for name in ("secret_in_logs", "secret_in_surfaces")
    ]
    if any(flag is None for flag in flags):
        return _gate(UNKNOWN, "secret checks not run")
    return _gate(
        "PASS" if not any(flags) else "FAIL",
        "a seeded fake secret never reached logs, metrics, dashboard, or SQLite",
    )


# ------------------------------------------------------------------- staging


def staging_readiness(
    settings: Settings, *, environment: Mapping[str, str], confirmed: bool
) -> tuple[bool, str]:
    """Return whether the authorised staging benchmark may run, and why not."""

    if not confirmed:
        return False, "--confirm-authorized-staging was not passed"
    if environment.get("RUN_STAGING_TESTS") != "1" or environment.get(_EXPERIMENT_GATE) != "1":
        return False, f"RUN_STAGING_TESTS=1 and {_EXPERIMENT_GATE}=1 are required"
    if settings.staging_url is None:
        return False, "STAGING_URL is not configured"
    if not (
        settings.status_discovery_enabled
        and settings.status_discovery_scope == AUTHORIZED_STAGING_SCOPE
        and settings.status_discovery_confirm_authorized_staging
    ):
        return False, "authorised status discovery is not configured"
    path = settings.direct_monitor_schema_path
    if path is None:
        return False, "DIRECT_MONITOR_SCHEMA_PATH is not configured"
    try:
        schema = load_direct_response_schema(path)
    except DirectSchemaError:
        return False, "no reviewed authorized_queue_it_staging response schema"
    if schema.source_scope != AUTHORIZED_STAGING_SCOPE:
        return False, "response schema is not authorised staging evidence"
    return True, "ready"


def staging_not_run(reason: str) -> dict[str, Any]:
    return {
        "status": NOT_RUN,
        "reason": reason,
        "lifecycle": dict.fromkeys(LIFECYCLE_STAGES, UNKNOWN),
        "gates": dict.fromkeys(
            (
                "direct_identity_safety",
                "observation_equivalence",
                "fallback_reliability",
                "direct_request_stability",
                "restart_continuity",
                "throughput_improvement",
                "browser_context_reduction",
                "cpu_ram_effect",
                "request_cadence_safety",
                "sensitive_data_safety",
            ),
            UNKNOWN,
        ),
    }


# ------------------------------------------------------------------ entrypoint


async def run_local_benchmark(
    directory: Path,
    *,
    backend: BrowserBackendName = BrowserBackendName.CHROME,
    profile: BenchmarkProfile | None = None,
    staging_reason: str = "no authorised Queue-it staging environment configured",
) -> dict[str, Any]:
    profile = profile or BenchmarkProfile()
    secret = f"BENCHSECRET{secrets.token_hex(8)}"
    results: dict[MonitoringStrategy, dict[str, Any]] = {}
    for strategy in (MonitoringStrategy.HEADED_WINDOW, MonitoringStrategy.DIRECT):
        simulator = LocalQueueSimulator(
            new_identity_prefix=f"bench-{strategy.value}",
            status_enabled=True,
            secret_token=secret,
            poll_after_seconds=profile.poll_hint_seconds,
            # Visitors join in PRE_QUEUE so the stage layout only ever moves forward.
            initial_stage=STAGE_PRE,
        )
        await simulator.start()
        try:
            results[strategy] = await run_strategy(
                strategy,
                directory / strategy.value,
                target=_Target(url=simulator.entry_url, simulator=simulator),
                backend=backend,
                profile=profile,
                secret=secret,
            )
        finally:
            await simulator.close()
    report = build_report(
        mode="local",
        backend=backend.value,
        profile=profile,
        headed=results[MonitoringStrategy.HEADED_WINDOW],
        direct=results[MonitoringStrategy.DIRECT],
        staging=staging_not_run(staging_reason),
    )
    report["report_contains_secret"] = secret in json.dumps(report, default=str)
    return report


async def run_staging_benchmark(
    directory: Path,
    *,
    settings: Settings,
    profile: BenchmarkProfile,
) -> dict[str, Any]:
    """Authorised staging: production cadence, passive observation, no fault injection."""

    assert settings.staging_url is not None
    secret = f"BENCHSECRET{secrets.token_hex(8)}"  # never present upstream; must stay absent
    passive = BenchmarkProfile(
        sessions=profile.sessions,
        window_seconds=profile.window_seconds,
        monitor_workers=settings.monitor_workers,
        monitor_queue_capacity=settings.monitor_queue_capacity,
        monitor_claim_batch_size=settings.monitor_claim_batch_size,
        poll_min_seconds=settings.active_early_poll_min_seconds,
        poll_max_seconds=settings.active_early_poll_max_seconds,
        poll_hint_seconds=None,
        recovery=False,
    )
    results: dict[MonitoringStrategy, dict[str, Any]] = {}
    for strategy in (MonitoringStrategy.HEADED_WINDOW, MonitoringStrategy.DIRECT):
        results[strategy] = await run_strategy(
            strategy,
            directory / strategy.value,
            target=_Target(url=str(settings.staging_url), simulator=None),
            backend=settings.browser_backend,
            profile=passive,
            secret=secret,
            base_settings=settings,
        )
    return build_report(
        mode="staging",
        backend=settings.browser_backend.value,
        profile=passive,
        headed=results[MonitoringStrategy.HEADED_WINDOW],
        direct=results[MonitoringStrategy.DIRECT],
        staging={"status": "RUN", "reason": "authorised staging gates satisfied"},
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--mode", choices=("local", "staging"), default="local")
    parser.add_argument(
        "--backend",
        choices=tuple(backend.value for backend in BrowserBackendName),
        default=BrowserBackendName.PATCHRIGHT.value,
    )
    defaults = BenchmarkProfile()
    parser.add_argument("--sessions", type=int, default=defaults.sessions)
    parser.add_argument("--window-seconds", type=float, default=defaults.window_seconds)
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if not 1 <= args.sessions <= 50 or args.window_seconds <= 0:
        raise SystemExit("--sessions must be 1-50 and --window-seconds positive")
    profile = BenchmarkProfile(sessions=max(3, args.sessions), window_seconds=args.window_seconds)
    with tempfile.TemporaryDirectory(prefix="phase8-monitoring-benchmark-") as directory:
        if args.mode == "staging":
            settings = Settings()
            ready, reason = staging_readiness(
                settings, environment=os.environ, confirmed=args.confirm_authorized_staging
            )
            if not ready:
                raise SystemExit(f"Staging benchmark NOT RUN: {reason}")
            report = asyncio.run(
                run_staging_benchmark(Path(directory), settings=settings, profile=profile)
            )
        else:
            report = asyncio.run(
                run_local_benchmark(
                    Path(directory),
                    backend=BrowserBackendName.parse(args.backend),
                    profile=profile,
                )
            )
    text = json.dumps(report, indent=2, default=str)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({name: gate["result"] for name, gate in report["gates"].items()}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
