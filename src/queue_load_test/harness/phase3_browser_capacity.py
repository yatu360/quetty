"""Controlled Phase 3 BrowserContext capacity benchmark."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from queue_load_test.browser import BrowserCapacity, BrowserManager
from queue_load_test.config import get_settings
from queue_load_test.harness.resource_benchmark import (
    ProcessResourceProbe,
    ProcessResourceSnapshot,
    PsutilProcessResourceProbe,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics

_LOCAL_NAVIGATION_URL = "data:text/html,<title>capacity-probe</title><p>ready</p>"
_STAGING_GATE = "RUN_PHASE3_BROWSER_BENCHMARK"


class NavigationMode(StrEnum):
    LOCAL = "LOCAL"
    STAGING = "STAGING"


@dataclass(frozen=True, slots=True)
class BrowserCapacityCase:
    name: str
    active_contexts: int
    chrome_process_count: int
    max_contexts_per_browser: int = 25

    def __post_init__(self) -> None:
        if not self.name or any(
            character not in "-_abcdefghijklmnopqrstuvwxyz0123456789"
            for character in self.name
        ):
            raise ValueError("case name must use lowercase letters, digits, '-' or '_'")
        if not 1 <= self.active_contexts <= 100:
            raise ValueError("active_contexts must be between 1 and 100")
        if not 1 <= self.chrome_process_count <= 4:
            raise ValueError("chrome_process_count must be between 1 and 4")
        if not 1 <= self.max_contexts_per_browser <= 25:
            raise ValueError("max_contexts_per_browser must be between 1 and 25")
        if (
            self.chrome_process_count * self.max_contexts_per_browser
            < self.active_contexts
        ):
            raise ValueError("browser capacity cannot satisfy active_contexts")


def generate_phase3_browser_cases() -> tuple[BrowserCapacityCase, ...]:
    """Return the small, proportional Phase 3 candidate set."""

    return (
        BrowserCapacityCase("p2-c50", 50, 2),
        BrowserCapacityCase("p3-c75", 75, 3),
        BrowserCapacityCase("p4-c100", 100, 4),
    )


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
class BrowserResourceSample:
    timestamp: datetime
    application_cpu_percent: float | None
    application_ram_bytes: int | None
    chrome_cpu_percent: float | None
    chrome_ram_bytes: int | None
    observed_chrome_processes: int | None
    managed_chrome_processes: int
    active_contexts: int


@dataclass(frozen=True, slots=True)
class BrowserResourceSummary:
    application_cpu_percent: AveragePeak
    application_ram_bytes: AveragePeak
    chrome_cpu_percent: AveragePeak
    chrome_ram_bytes: AveragePeak
    observed_chrome_processes: AveragePeak


def summarize_browser_resources(
    samples: Sequence[BrowserResourceSample],
) -> BrowserResourceSummary:
    return BrowserResourceSummary(
        application_cpu_percent=_average_peak(
            [sample.application_cpu_percent for sample in samples]
        ),
        application_ram_bytes=_average_peak(
            [sample.application_ram_bytes for sample in samples]
        ),
        chrome_cpu_percent=_average_peak([sample.chrome_cpu_percent for sample in samples]),
        chrome_ram_bytes=_average_peak([sample.chrome_ram_bytes for sample in samples]),
        observed_chrome_processes=_average_peak(
            [sample.observed_chrome_processes for sample in samples]
        ),
    )


@dataclass(frozen=True, slots=True)
class BrowserCapacityCaseResult:
    case: BrowserCapacityCase
    status: str
    navigation_mode: NavigationMode
    duration_seconds: float
    achieved_active_contexts: int
    connected_browser_processes: int
    contexts_per_browser: tuple[int, ...]
    context_creation: LatencySummary
    context_acquisition: LatencySummary
    context_acquisition_wait: LatencySummary
    navigation: LatencySummary
    resources: BrowserResourceSummary
    samples: tuple[BrowserResourceSample, ...]
    browser_crashes: int
    context_creation_failures: int
    navigation_failures: int
    cleanup_failures: int
    cleanup_complete: bool
    error_category: str | None = None

    @property
    def clean_execution(self) -> bool:
        return (
            self.status == "COMPLETED"
            and self.achieved_active_contexts == self.case.active_contexts
            and self.connected_browser_processes == self.case.chrome_process_count
            and self.browser_crashes == 0
            and self.context_creation_failures == 0
            and self.navigation_failures == 0
            and self.cleanup_failures == 0
            and self.cleanup_complete
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "navigation_mode": self.navigation_mode.value,
            "clean_execution": self.clean_execution,
        }


@dataclass(frozen=True, slots=True)
class CapacitySymptom:
    code: str
    case: str
    previous_case: str | None
    observed: float | int | str
    threshold: float | int | str
    detail: str


@dataclass(frozen=True, slots=True)
class CapacityThresholds:
    adjacent_latency_ratio: float = 1.5
    adjacent_ram_ratio: float = 1.5
    failure_rate_increase: float = 0.02
    sustained_cpu_fraction: float = 0.9
    host_ram_fraction: float = 0.8
    acquisition_wait_seconds: float = 2.0


@dataclass(frozen=True, slots=True)
class BrowserCapacityBenchmarkReport:
    generated_at: datetime
    navigation_mode: NavigationMode
    staging_status: str
    cases: tuple[BrowserCapacityCaseResult, ...]
    symptoms: tuple[CapacitySymptom, ...]
    host_logical_cpu_count: int
    host_ram_bytes: int | None
    scope_warning: str = (
        "Results apply only to these Phase 3 browser-capacity cases and this host. "
        "They do not select an optimal value or predict 10,000-session behavior."
    )

    @classmethod
    def build(
        cls,
        cases: Sequence[BrowserCapacityCaseResult],
        *,
        navigation_mode: NavigationMode,
        thresholds: CapacityThresholds | None = None,
    ) -> BrowserCapacityBenchmarkReport:
        case_tuple = tuple(cases)
        logical_cpu_count = os.cpu_count() or 1
        host_ram_bytes = _host_ram_bytes()
        return cls(
            generated_at=datetime.now(UTC),
            navigation_mode=navigation_mode,
            staging_status=(
                "COMPLETED" if navigation_mode is NavigationMode.STAGING else "NOT RUN"
            ),
            cases=case_tuple,
            symptoms=calculate_capacity_symptoms(
                case_tuple,
                thresholds,
                logical_cpu_count=logical_cpu_count,
                host_ram_bytes=host_ram_bytes,
            ),
            host_logical_cpu_count=logical_cpu_count,
            host_ram_bytes=host_ram_bytes,
        )

    def comparison_rows(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "case": result.case.name,
                "status": result.status,
                "configured_contexts": result.case.active_contexts,
                "achieved_contexts": result.achieved_active_contexts,
                "browser_processes": result.connected_browser_processes,
                "contexts_per_browser": list(result.contexts_per_browser),
                "p95_context_creation_seconds": result.context_creation.p95_seconds,
                "p95_context_acquisition_seconds": result.context_acquisition.p95_seconds,
                "p95_acquisition_wait_seconds": (
                    result.context_acquisition_wait.p95_seconds
                ),
                "p95_navigation_seconds": result.navigation.p95_seconds,
                "chrome_cpu_average": result.resources.chrome_cpu_percent.average,
                "chrome_cpu_peak": result.resources.chrome_cpu_percent.peak,
                "chrome_ram_average_bytes": result.resources.chrome_ram_bytes.average,
                "chrome_ram_peak_bytes": result.resources.chrome_ram_bytes.peak,
                "browser_crashes": result.browser_crashes,
                "context_creation_failures": result.context_creation_failures,
                "navigation_failures": result.navigation_failures,
                "cleanup_failures": result.cleanup_failures,
                "cleanup_complete": result.cleanup_complete,
                "clean_execution": result.clean_execution,
            }
            for result in self.cases
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "navigation_mode": self.navigation_mode.value,
            "staging_status": self.staging_status,
            "scope_warning": self.scope_warning,
            "host_logical_cpu_count": self.host_logical_cpu_count,
            "host_ram_bytes": self.host_ram_bytes,
            "measurement_notes": {
                "cpu": "Aggregate process CPU where 100 percent represents one logical core.",
                "ram": "Summed process RSS; shared Chrome pages may be counted more than once.",
            },
            "cases": [result.to_dict() for result in self.cases],
            "comparison": list(self.comparison_rows()),
            "symptoms": [asdict(symptom) for symptom in self.symptoms],
        }

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
            "Phase 3 browser-capacity benchmark",
            "",
            self.scope_warning,
            "",
            f"Navigation mode: {self.navigation_mode.value}",
            f"Staging status: {self.staging_status}",
            f"Host logical CPUs: {self.host_logical_cpu_count}",
            f"Host RAM bytes: {self.host_ram_bytes or 'unknown'}",
            (
                "case | proc | contexts | p95 create | p95 wait | p95 nav | "
                "Chrome CPU avg/peak | Chrome RAM avg/peak | failures | clean"
            ),
        ]
        for row in self.comparison_rows():
            failure_total = sum(
                _integer(row[name])
                for name in (
                    "browser_crashes",
                    "context_creation_failures",
                    "navigation_failures",
                    "cleanup_failures",
                )
            )
            lines.append(
                f"{row['case']} | {row['browser_processes']} | "
                f"{row['achieved_contexts']}/{row['configured_contexts']} | "
                f"{_seconds(row['p95_context_creation_seconds'])} | "
                f"{_seconds(row['p95_acquisition_wait_seconds'])} | "
                f"{_seconds(row['p95_navigation_seconds'])} | "
                f"{_pair(row['chrome_cpu_average'], row['chrome_cpu_peak'])} | "
                f"{_pair(row['chrome_ram_average_bytes'], row['chrome_ram_peak_bytes'])} | "
                f"{failure_total} | {row['clean_execution']}"
            )
        lines.extend(("", f"Symptoms: {len(self.symptoms)}"))
        lines.extend(f"- {item.code}: {item.case} — {item.detail}" for item in self.symptoms)
        return "\n".join(lines) + "\n"


class OwnedContextHandle(Protocol):
    @property
    def context(self) -> BrowserContext: ...

    @property
    def browser_id(self) -> int: ...

    @property
    def closed(self) -> bool: ...

    @property
    def creation_duration_seconds(self) -> float: ...

    @property
    def acquisition_wait_seconds(self) -> float: ...

    async def close(self) -> None: ...


class BrowserManagerHandle(Protocol):
    async def start(self) -> None: ...

    async def create_context(self) -> OwnedContextHandle: ...

    async def capacity(self) -> BrowserCapacity: ...

    async def shutdown(self) -> None: ...


type ManagerFactory = Callable[
    [BrowserCapacityCase, PrometheusMetrics], BrowserManagerHandle
]


def _default_manager_factory(
    case: BrowserCapacityCase,
    metrics: PrometheusMetrics,
) -> BrowserManagerHandle:
    return BrowserManager(
        chrome_process_count=case.chrome_process_count,
        max_contexts_per_browser=case.max_contexts_per_browser,
        max_active_contexts=case.active_contexts,
        headless=True,
        observability=metrics,
    )


async def run_browser_capacity_case(
    case: BrowserCapacityCase,
    *,
    navigation_url: str = _LOCAL_NAVIGATION_URL,
    navigation_mode: NavigationMode = NavigationMode.LOCAL,
    hold_seconds: float = 2.0,
    sample_interval_seconds: float = 0.25,
    allocation_workers: int = 10,
    navigation_timeout_ms: float = 30_000,
    manager_factory: ManagerFactory = _default_manager_factory,
    process_probe: ProcessResourceProbe | None = None,
) -> BrowserCapacityCaseResult:
    """Run one bounded case and always clean up contexts and Chrome processes."""

    if hold_seconds < 0:
        raise ValueError("hold_seconds cannot be negative")
    if sample_interval_seconds <= 0 or allocation_workers < 1:
        raise ValueError("sample interval and allocation_workers must be positive")
    metrics = PrometheusMetrics()
    manager = manager_factory(case, metrics)
    probe = process_probe or PsutilProcessResourceProbe()
    samples: list[BrowserResourceSample] = []
    contexts: list[OwnedContextHandle] = []
    context_creation: list[float] = []
    context_acquisition: list[float] = []
    acquisition_wait: list[float] = []
    navigation: list[float] = []
    context_creation_failures = 0
    navigation_failures = 0
    direct_cleanup_failures = 0
    achieved_capacity: BrowserCapacity | None = None
    cleanup_complete = False
    error_category: str | None = None
    status = "COMPLETED"
    sampling_stop = asyncio.Event()
    sampling_task: asyncio.Task[None] | None = None
    started = time.perf_counter()
    started_manager = False

    try:
        await manager.start()
        started_manager = True
        await _capture_resource_sample(samples, manager, probe)
        sampling_task = asyncio.create_task(
            _sample_resources(
                samples,
                manager,
                probe,
                sampling_stop,
                sample_interval_seconds,
            ),
            name=f"capacity-sampler-{case.name}",
        )
        work_queue: asyncio.Queue[int] = asyncio.Queue(maxsize=case.active_contexts)
        for index in range(case.active_contexts):
            work_queue.put_nowait(index)

        async def worker() -> None:
            nonlocal context_creation_failures, navigation_failures
            while True:
                try:
                    work_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                acquisition_started = time.perf_counter()
                try:
                    owned = await manager.create_context()
                except Exception:  # noqa: BLE001 - record the bounded case failure
                    context_creation_failures += 1
                    work_queue.task_done()
                    continue
                context_acquisition.append(time.perf_counter() - acquisition_started)
                context_creation.append(owned.creation_duration_seconds)
                acquisition_wait.append(owned.acquisition_wait_seconds)
                contexts.append(owned)
                navigation_started = time.perf_counter()
                try:
                    page = await owned.context.new_page()
                    response = await page.goto(
                        navigation_url,
                        wait_until="domcontentloaded",
                        timeout=navigation_timeout_ms,
                    )
                    if response is not None and response.status >= 400:
                        navigation_failures += 1
                except (PlaywrightTimeoutError, PlaywrightError, OSError):
                    navigation_failures += 1
                finally:
                    navigation.append(time.perf_counter() - navigation_started)
                    work_queue.task_done()

        workers = [
            asyncio.create_task(worker(), name=f"capacity-worker-{case.name}-{index}")
            for index in range(min(allocation_workers, case.active_contexts))
        ]
        await asyncio.gather(*workers)
        achieved_capacity = await manager.capacity()
        await _capture_resource_sample(samples, manager, probe)
        if hold_seconds:
            await asyncio.sleep(hold_seconds)
        await _capture_resource_sample(samples, manager, probe)
    except Exception as exc:  # noqa: BLE001 - preserve objective failure output
        status = "FAILED"
        error_category = type(exc).__name__
    finally:
        sampling_stop.set()
        if sampling_task is not None:
            await sampling_task
        direct_cleanup_failures += await _close_contexts(contexts, allocation_workers)
        if started_manager:
            try:
                remaining = await manager.capacity()
                cleanup_complete = remaining.active_contexts == 0
            except Exception:  # noqa: BLE001
                direct_cleanup_failures += 1
            try:
                await manager.shutdown()
            except Exception:  # noqa: BLE001
                direct_cleanup_failures += 1

    metric_cleanup_failures = _metric_int(metrics, "browser_cleanup_failures_total")
    cleanup_failures = max(direct_cleanup_failures, metric_cleanup_failures)
    crashes = _metric_int(metrics, "browser_crashes_total")
    if achieved_capacity is None:
        achieved_capacity = BrowserCapacity(0, 0, 0, 0, case.active_contexts, ())
    return BrowserCapacityCaseResult(
        case=case,
        status=status,
        navigation_mode=navigation_mode,
        duration_seconds=time.perf_counter() - started,
        achieved_active_contexts=achieved_capacity.active_contexts,
        connected_browser_processes=achieved_capacity.connected_processes,
        contexts_per_browser=tuple(
            process.active_contexts for process in achieved_capacity.processes
        ),
        context_creation=_latency_summary(context_creation),
        context_acquisition=_latency_summary(context_acquisition),
        context_acquisition_wait=_latency_summary(acquisition_wait),
        navigation=_latency_summary(navigation),
        resources=summarize_browser_resources(samples),
        samples=tuple(samples),
        browser_crashes=crashes,
        context_creation_failures=context_creation_failures,
        navigation_failures=navigation_failures,
        cleanup_failures=cleanup_failures,
        cleanup_complete=cleanup_complete,
        error_category=error_category,
    )


async def run_phase3_browser_capacity_benchmark(
    *,
    cases: Sequence[BrowserCapacityCase] | None = None,
    navigation_url: str = _LOCAL_NAVIGATION_URL,
    navigation_mode: NavigationMode = NavigationMode.LOCAL,
    hold_seconds: float = 2.0,
    sample_interval_seconds: float = 0.25,
    allocation_workers: int = 10,
) -> BrowserCapacityBenchmarkReport:
    selected_cases = tuple(cases or generate_phase3_browser_cases())
    if not selected_cases:
        raise ValueError("at least one benchmark case is required")
    results = []
    for case in selected_cases:
        results.append(
            await run_browser_capacity_case(
                case,
                navigation_url=navigation_url,
                navigation_mode=navigation_mode,
                hold_seconds=hold_seconds,
                sample_interval_seconds=sample_interval_seconds,
                allocation_workers=allocation_workers,
            )
        )
    return BrowserCapacityBenchmarkReport.build(results, navigation_mode=navigation_mode)


def calculate_capacity_symptoms(
    results: Sequence[BrowserCapacityCaseResult],
    thresholds: CapacityThresholds | None = None,
    *,
    logical_cpu_count: int | None = None,
    host_ram_bytes: int | None = None,
) -> tuple[CapacitySymptom, ...]:
    limits = thresholds or CapacityThresholds()
    cpu_capacity = 100.0 * (logical_cpu_count or os.cpu_count() or 1)
    cpu_threshold = cpu_capacity * limits.sustained_cpu_fraction
    symptoms: list[CapacitySymptom] = []
    for result in results:
        failure_count = (
            result.browser_crashes
            + result.context_creation_failures
            + result.navigation_failures
            + result.cleanup_failures
        )
        if result.achieved_active_contexts != result.case.active_contexts:
            symptoms.append(
                CapacitySymptom(
                    "CONTEXT_SHORTFALL",
                    result.case.name,
                    None,
                    result.achieved_active_contexts,
                    result.case.active_contexts,
                    "The case did not reach its configured active-context count.",
                )
            )
        if failure_count:
            symptoms.append(
                CapacitySymptom(
                    "FAILURES_OBSERVED",
                    result.case.name,
                    None,
                    failure_count,
                    0,
                    "Browser, context, navigation, or cleanup failures were observed.",
                )
            )
        cpu_average = max(
            result.resources.application_cpu_percent.average or 0,
            result.resources.chrome_cpu_percent.average or 0,
        )
        if cpu_average >= cpu_threshold:
            symptoms.append(
                CapacitySymptom(
                    "CPU_NEAR_SATURATION",
                    result.case.name,
                    None,
                    cpu_average,
                    cpu_threshold,
                    "Average application or aggregate Chrome CPU reached 90% of host capacity.",
                )
            )
        combined_peak_ram = _combined_peak_ram(result.resources)
        if (
            host_ram_bytes is not None
            and combined_peak_ram is not None
            and combined_peak_ram >= host_ram_bytes * limits.host_ram_fraction
        ):
            symptoms.append(
                CapacitySymptom(
                    "RAM_PRESSURE_INDICATOR",
                    result.case.name,
                    None,
                    combined_peak_ram,
                    host_ram_bytes * limits.host_ram_fraction,
                    "Summed peak process RSS reached 80% of host RAM; shared pages may overlap.",
                )
            )
        wait = result.context_acquisition_wait.p95_seconds
        if wait is not None and wait >= limits.acquisition_wait_seconds:
            symptoms.append(
                CapacitySymptom(
                    "CONTEXT_ALLOCATION_STALL",
                    result.case.name,
                    None,
                    wait,
                    limits.acquisition_wait_seconds,
                    "p95 BrowserManager lock wait reached the stall threshold.",
                )
            )

    ordered = sorted(results, key=lambda result: result.case.active_contexts)
    for previous, current in pairwise(ordered):
        for field, code in (
            ("context_creation", "CONTEXT_CREATION_LATENCY_RISE"),
            ("context_acquisition", "CONTEXT_ACQUISITION_LATENCY_RISE"),
            ("navigation", "NAVIGATION_LATENCY_RISE"),
        ):
            earlier = getattr(previous, field).p95_seconds
            later = getattr(current, field).p95_seconds
            ratio = _ratio(later, earlier)
            if ratio is not None and ratio >= limits.adjacent_latency_ratio:
                symptoms.append(
                    CapacitySymptom(
                        code,
                        current.case.name,
                        previous.case.name,
                        ratio,
                        limits.adjacent_latency_ratio,
                        f"p95 {field.replace('_', ' ')} rose between adjacent cases.",
                    )
                )
        earlier_ram = _combined_peak_ram(previous.resources)
        later_ram = _combined_peak_ram(current.resources)
        ram_ratio = _ratio(later_ram, earlier_ram)
        if ram_ratio is not None and ram_ratio >= limits.adjacent_ram_ratio:
            symptoms.append(
                CapacitySymptom(
                    "RAM_GROWTH",
                    current.case.name,
                    previous.case.name,
                    ram_ratio,
                    limits.adjacent_ram_ratio,
                    "Combined application and Chrome peak RAM rose between cases.",
                )
            )
        earlier_failures = _failure_rate(previous)
        later_failures = _failure_rate(current)
        if later_failures - earlier_failures >= limits.failure_rate_increase:
            symptoms.append(
                CapacitySymptom(
                    "FAILURE_RATE_INCREASE",
                    current.case.name,
                    previous.case.name,
                    later_failures - earlier_failures,
                    limits.failure_rate_increase,
                    "Aggregate failure rate increased between adjacent cases.",
                )
            )
    return tuple(symptoms)


async def _capture_resource_sample(
    samples: list[BrowserResourceSample],
    manager: BrowserManagerHandle,
    probe: ProcessResourceProbe,
) -> None:
    try:
        resource = probe.sample()
    except Exception:  # noqa: BLE001 - optional resource collection cannot abort a case
        resource = ProcessResourceSnapshot()
    capacity = await manager.capacity()
    samples.append(
        BrowserResourceSample(
            timestamp=datetime.now(UTC),
            application_cpu_percent=resource.application_cpu_percent,
            application_ram_bytes=resource.application_ram_bytes,
            chrome_cpu_percent=resource.chrome_cpu_percent,
            chrome_ram_bytes=resource.chrome_ram_bytes,
            observed_chrome_processes=resource.observed_chrome_processes,
            managed_chrome_processes=capacity.chrome_processes,
            active_contexts=capacity.active_contexts,
        )
    )


async def _sample_resources(
    samples: list[BrowserResourceSample],
    manager: BrowserManagerHandle,
    probe: ProcessResourceProbe,
    stop_event: asyncio.Event,
    interval_seconds: float,
) -> None:
    while not stop_event.is_set():
        try:
            await _capture_resource_sample(samples, manager, probe)
        except Exception:  # noqa: BLE001 - failed sampling is not a benchmark crash
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def _close_contexts(
    contexts: Sequence[OwnedContextHandle],
    worker_count: int,
) -> int:
    queue: asyncio.Queue[OwnedContextHandle] = asyncio.Queue(maxsize=max(1, len(contexts)))
    for context in contexts:
        queue.put_nowait(context)
    failures = 0

    async def worker() -> None:
        nonlocal failures
        while True:
            try:
                context = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                failures += 1
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(worker(), name=f"capacity-cleanup-{index}")
        for index in range(min(worker_count, max(1, len(contexts))))
    ]
    await asyncio.gather(*workers)
    return failures


def _latency_summary(values: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values),
        average_seconds=statistics.fmean(values) if values else None,
        p50_seconds=percentile(values, 50),
        p95_seconds=percentile(values, 95),
        maximum_seconds=max(values) if values else None,
    )


def _average_peak(values: Sequence[float | int | None]) -> AveragePeak:
    available = [float(value) for value in values if value is not None]
    return AveragePeak(
        average=statistics.fmean(available) if available else None,
        peak=max(available) if available else None,
    )


def _metric_int(metrics: PrometheusMetrics, name: str) -> int:
    value = metrics.registry.get_sample_value(name)
    return int(value or 0)


def _ratio(later: float | None, earlier: float | None) -> float | None:
    if later is None or earlier is None or earlier <= 0:
        return None
    return later / earlier


def _combined_peak_ram(resources: BrowserResourceSummary) -> float | None:
    values = [
        value
        for value in (
            resources.application_ram_bytes.peak,
            resources.chrome_ram_bytes.peak,
        )
        if value is not None
    ]
    return sum(values) if values else None


def _failure_rate(result: BrowserCapacityCaseResult) -> float:
    attempts = result.case.active_contexts * 2
    failures = (
        result.browser_crashes
        + result.context_creation_failures
        + result.navigation_failures
        + result.cleanup_failures
    )
    return failures / attempts


def _seconds(value: object) -> str:
    return "n/a" if value is None else f"{_number(value):.4f}s"


def _pair(average: object, peak: object) -> str:
    if average is None and peak is None:
        return "n/a"
    return f"{average}/{peak}"


def _number(value: object) -> float:
    if not isinstance(value, int | float):
        raise TypeError(f"Expected numeric value, got {type(value).__name__}")
    return float(value)


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"Expected integer value, got {type(value).__name__}")
    return value


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _host_ram_bytes() -> int | None:
    try:
        psutil = importlib.import_module("psutil")
        return int(psutil.virtual_memory().total)
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Phase 3 browser-capacity benchmark")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("phase3-browser-capacity-benchmark.json"),
    )
    parser.add_argument("--staging", action="store_true")
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=0.25)
    parser.add_argument("--allocation-workers", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    navigation_mode = NavigationMode.STAGING if args.staging else NavigationMode.LOCAL
    navigation_url = _LOCAL_NAVIGATION_URL
    if navigation_mode is NavigationMode.STAGING:
        if not args.confirm_authorized_staging:
            raise SystemExit("Pass --confirm-authorized-staging to enable staging traffic")
        if os.environ.get("RUN_STAGING_TESTS") != "1" or os.environ.get(_STAGING_GATE) != "1":
            raise SystemExit(
                f"Set RUN_STAGING_TESTS=1 and {_STAGING_GATE}=1 to enable staging traffic"
            )
        navigation_url = str(get_settings().staging_url)
    report = asyncio.run(
        run_phase3_browser_capacity_benchmark(
            navigation_url=navigation_url,
            navigation_mode=navigation_mode,
            hold_seconds=args.hold_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            allocation_workers=args.allocation_workers,
        )
    )
    report.write_json(args.report)
    print(report.render_text())


if __name__ == "__main__":
    main()
