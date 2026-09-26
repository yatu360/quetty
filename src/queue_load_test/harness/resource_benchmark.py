"""Low-overhead, comparison-friendly Phase 2 resource measurements."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from queue_load_test.browser import BrowserCapacity, BrowserManager
from queue_load_test.config import Settings
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics

logger = logging.getLogger(__name__)
_HISTOGRAM_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


@dataclass(frozen=True, slots=True)
class ProcessResourceSnapshot:
    """Optional host observations; unavailable values remain explicit."""

    application_cpu_percent: float | None = None
    application_ram_bytes: int | None = None
    chrome_cpu_percent: float | None = None
    chrome_ram_bytes: int | None = None
    observed_chrome_processes: int | None = None
    open_file_descriptors: int | None = None


class ProcessResourceProbe(Protocol):
    def sample(self) -> ProcessResourceSnapshot: ...


class PsutilProcessResourceProbe:
    """Best-effort process-tree probe that remains usable without psutil."""

    def __init__(self, psutil_module: Any | None = None) -> None:
        try:
            self._psutil = psutil_module or importlib.import_module("psutil")
            self._application = self._psutil.Process(os.getpid())
            self._known_chrome: dict[int, Any] = {}
            self._application.cpu_percent(interval=None)
        except (ImportError, AttributeError, OSError):
            self._psutil = None
            self._application = None
            self._known_chrome = {}

    @property
    def available(self) -> bool:
        return self._application is not None

    def sample(self) -> ProcessResourceSnapshot:
        if self._application is None:
            return ProcessResourceSnapshot()
        try:
            application_cpu = float(self._application.cpu_percent(interval=None))
            application_ram = int(self._application.memory_info().rss)
        except Exception:  # noqa: BLE001 - process can disappear between syscalls
            return ProcessResourceSnapshot()

        open_descriptors: int | None = None
        try:
            num_fds = getattr(self._application, "num_fds", None)
            if callable(num_fds):
                open_descriptors = int(num_fds())
        except Exception:
            logger.debug("open file descriptor metric unavailable", exc_info=True)

        chrome_cpu = 0.0
        chrome_ram = 0
        chrome_count = 0
        try:
            children = self._application.children(recursive=True)
        except Exception:  # noqa: BLE001 - child enumeration is optional
            children = []
        live_pids: set[int] = set()
        for child in children:
            try:
                if "chrome" not in child.name().casefold():
                    continue
                pid = int(child.pid)
                live_pids.add(pid)
                tracked = self._known_chrome.get(pid)
                if tracked is None:
                    tracked = child
                    self._known_chrome[pid] = tracked
                    tracked.cpu_percent(interval=None)
                    cpu = 0.0
                else:
                    cpu = float(tracked.cpu_percent(interval=None))
                chrome_cpu += cpu
                chrome_ram += int(tracked.memory_info().rss)
                chrome_count += 1
            except Exception:
                logger.debug("Chrome process disappeared during resource sample", exc_info=True)
                continue
        self._known_chrome = {
            pid: process for pid, process in self._known_chrome.items() if pid in live_pids
        }
        return ProcessResourceSnapshot(
            application_cpu_percent=application_cpu,
            application_ram_bytes=application_ram,
            chrome_cpu_percent=chrome_cpu,
            chrome_ram_bytes=chrome_ram,
            observed_chrome_processes=chrome_count,
            open_file_descriptors=open_descriptors,
        )


@dataclass(frozen=True, slots=True)
class ResourceSample:
    timestamp: datetime
    application_cpu_percent: float | None
    application_ram_bytes: int | None
    chrome_cpu_percent: float | None
    chrome_ram_bytes: int | None
    observed_chrome_processes: int | None
    managed_chrome_processes: int | None
    active_browser_contexts: int | None
    open_file_descriptors: int | None
    creation_queue_depth: float | None
    monitoring_queue_depth: float | None
    due_session_backlog: float | None


@dataclass(frozen=True, slots=True)
class LatencySummary:
    count: int
    average_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None


@dataclass(frozen=True, slots=True)
class ResourceBenchmarkConfiguration:
    target_queue_ids: int
    session_mode: str
    chrome_process_count: int
    max_contexts_per_browser: int
    max_active_contexts: int
    creation_workers: int
    monitor_workers: int

    @classmethod
    def from_settings(cls, settings: Settings) -> ResourceBenchmarkConfiguration:
        return cls(
            target_queue_ids=settings.target_queue_ids,
            session_mode=settings.session_mode.value,
            chrome_process_count=settings.chrome_process_count,
            max_contexts_per_browser=settings.max_contexts_per_browser,
            max_active_contexts=settings.max_active_contexts,
            creation_workers=settings.creation_workers,
            monitor_workers=settings.monitor_workers,
        )


@dataclass(frozen=True, slots=True)
class ResourceBenchmarkReport:
    generated_at: datetime
    configuration: ResourceBenchmarkConfiguration
    test_duration_seconds: float
    sample_interval_seconds: float
    summary: dict[str, object]
    samples: tuple[ResourceSample, ...]
    staging_status: str = "COMPLETED"
    scope_warning: str = (
        "Results apply only to this authorised Phase 2 configuration and host; "
        "they do not predict Phase 3 or Phase 4 performance."
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "staging_status": self.staging_status,
            "scope_warning": self.scope_warning,
            "configuration": asdict(self.configuration),
            "test_duration_seconds": self.test_duration_seconds,
            "sample_interval_seconds": self.sample_interval_seconds,
            "summary": self.summary,
            "samples": [asdict(sample) for sample in self.samples],
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
        latencies = self.summary["latencies"]
        assert isinstance(latencies, dict)
        lines = [
            "Phase 2 resource and stability benchmark",
            "",
            self.scope_warning,
            "",
            f"Staging status: {self.staging_status}",
            f"Chrome processes configured: {self.configuration.chrome_process_count}",
            f"Test duration: {self.test_duration_seconds:.3f}s",
            f"Samples: {len(self.samples)}",
            f"Application CPU average/peak: {_pair(self.summary, 'application_cpu_percent')}",
            f"Application RAM average/peak: {_pair(self.summary, 'application_ram_bytes')}",
            f"Chrome CPU average/peak: {_pair(self.summary, 'chrome_cpu_percent')}",
            f"Chrome RAM average/peak: {_pair(self.summary, 'chrome_ram_bytes')}",
            f"Active-context peak: {self.summary['active_context_peak']}",
            f"Browser crashes: {self.summary['browser_crashes']}",
            f"Context creation failures: {self.summary['context_creation_failures']}",
            f"Navigation failures: {self.summary['navigation_failures']}",
            f"Restore failures: {self.summary['restore_failures']}",
            f"Identity mismatches: {self.summary['identity_mismatches']}",
            f"Creation throughput: {self.summary['creation_throughput_per_second']:.3f}/s",
            f"Monitoring throughput: {self.summary['monitoring_throughput_per_second']:.3f}/s",
            "",
            "Latency averages / p50 / p95",
        ]
        for name, values in latencies.items():
            assert isinstance(values, dict)
            lines.append(
                f"- {name}: {_seconds(values['average_seconds'])} / "
                f"{_seconds(values['p50_seconds'])} / {_seconds(values['p95_seconds'])}"
            )
        return "\n".join(lines) + "\n"


@dataclass(slots=True)
class ResourceBenchmarkRecorder:
    """Collect interval samples and aggregate operation timings without identity labels."""

    samples: list[ResourceSample] = field(default_factory=list)
    creation_latencies: list[float] = field(default_factory=list)
    context_creation_latencies: list[float] = field(default_factory=list)
    check_latencies: list[float] = field(default_factory=list)
    restore_latencies: list[float] = field(default_factory=list)
    creation_successes: int = 0
    completed_checks: int = 0
    creation_phase_seconds: float = 0.0
    monitoring_phase_seconds: float = 0.0

    async def capture(
        self,
        *,
        browser_manager: BrowserManager,
        metrics: PrometheusMetrics,
        process_probe: ProcessResourceProbe,
    ) -> ResourceSample:
        resources = process_probe.sample()
        capacity: BrowserCapacity | None
        try:
            capacity = await browser_manager.capacity()
        except Exception:  # noqa: BLE001 - sampling must not stop the benchmark
            capacity = None
        sample = ResourceSample(
            timestamp=datetime.now(UTC),
            application_cpu_percent=resources.application_cpu_percent,
            application_ram_bytes=resources.application_ram_bytes,
            chrome_cpu_percent=resources.chrome_cpu_percent,
            chrome_ram_bytes=resources.chrome_ram_bytes,
            observed_chrome_processes=resources.observed_chrome_processes,
            managed_chrome_processes=(capacity.connected_processes if capacity else None),
            active_browser_contexts=(capacity.active_contexts if capacity else None),
            open_file_descriptors=resources.open_file_descriptors,
            creation_queue_depth=_metric(metrics, "queue_creation_queue_depth"),
            monitoring_queue_depth=_metric(metrics, "monitoring_queue_depth"),
            due_session_backlog=_metric(metrics, "monitoring_due_backlog"),
        )
        self.samples.append(sample)
        return sample

    def record_creation(self, duration_seconds: float, *, success: bool) -> None:
        self.creation_latencies.append(duration_seconds)
        self.creation_successes += int(success)

    def record_context_creation(self, duration_seconds: float) -> None:
        self.context_creation_latencies.append(duration_seconds)

    def record_restore(self, duration_seconds: float) -> None:
        self.restore_latencies.append(duration_seconds)

    def record_check(self, duration_seconds: float) -> None:
        self.check_latencies.append(duration_seconds)
        self.completed_checks += 1

    def build_report(
        self,
        settings: Settings,
        metrics: PrometheusMetrics,
        *,
        test_duration_seconds: float,
        sample_interval_seconds: float,
        staging_status: str = "COMPLETED",
    ) -> ResourceBenchmarkReport:
        return ResourceBenchmarkReport(
            generated_at=datetime.now(UTC),
            configuration=ResourceBenchmarkConfiguration.from_settings(settings),
            test_duration_seconds=test_duration_seconds,
            sample_interval_seconds=sample_interval_seconds,
            staging_status=staging_status,
            samples=tuple(self.samples),
            summary=self.summary(metrics, test_duration_seconds),
        )

    def summary(
        self,
        metrics: PrometheusMetrics,
        test_duration_seconds: float,
    ) -> dict[str, object]:
        def values(name: str) -> list[float]:
            return [
                float(value)
                for sample in self.samples
                if (value := getattr(sample, name)) is not None
            ]

        return {
            "application_cpu_percent": _average_peak(values("application_cpu_percent")),
            "application_ram_bytes": _average_peak(values("application_ram_bytes")),
            "chrome_cpu_percent": _average_peak(values("chrome_cpu_percent")),
            "chrome_ram_bytes": _average_peak(values("chrome_ram_bytes")),
            "open_file_descriptors": _average_peak(values("open_file_descriptors")),
            "active_context_peak": max(
                _peak(values("active_browser_contexts")) or 0,
                _metric(metrics, "active_browser_contexts_peak") or 0,
            ),
            "managed_chrome_process_peak": _peak(values("managed_chrome_processes")),
            "observed_chrome_process_peak": _peak(values("observed_chrome_processes")),
            "creation_queue_depth_peak": _peak(values("creation_queue_depth")),
            "monitoring_queue_depth_peak": _peak(values("monitoring_queue_depth")),
            "due_session_backlog_peak": _peak(values("due_session_backlog")),
            "browser_crashes": _counter(metrics, "browser_crashes_total"),
            "context_creation_failures": _counter(
                metrics, "browser_context_creation_failures_total"
            ),
            "navigation_failures": _counter(metrics, "navigation_failures_total"),
            "restore_failures": _counter(metrics, "state_restore_failures_total")
            + _counter(metrics, "transfer_restore_failures_total"),
            "identity_mismatches": _counter(metrics, "identity_mismatches_total"),
            "navigation_latency": prometheus_histogram_summary(
                metrics,
                "navigation_duration_seconds",
            ),
            "creation_throughput_per_second": self.creation_successes
            / max(self.creation_phase_seconds or test_duration_seconds, 1e-9),
            "monitoring_throughput_per_second": self.completed_checks
            / max(self.monitoring_phase_seconds or test_duration_seconds, 1e-9),
            "latencies": {
                "creation": asdict(_latency_summary(self.creation_latencies)),
                "context_creation": asdict(
                    _latency_summary(self.context_creation_latencies)
                ),
                "context_acquisition": prometheus_histogram_summary(
                    metrics,
                    "browser_context_acquisition_duration_seconds",
                ),
                "check": asdict(_latency_summary(self.check_latencies)),
                "restore": asdict(_latency_summary(self.restore_latencies)),
            },
        }


async def sample_resources(
    *,
    stop_event: asyncio.Event,
    recorder: ResourceBenchmarkRecorder,
    browser_manager: BrowserManager,
    metrics: PrometheusMetrics,
    process_probe: ProcessResourceProbe,
    interval_seconds: float,
) -> None:
    """Sample one runtime at a fixed cadence using only one bounded task."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    while True:
        await recorder.capture(
            browser_manager=browser_manager,
            metrics=metrics,
            process_probe=process_probe,
        )
        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def compare_reports(
    reports: Sequence[ResourceBenchmarkReport],
) -> tuple[dict[str, object], ...]:
    """Return stable summary rows suitable for comparing one- and two-browser runs."""

    return tuple(
        {
            "chrome_process_count": report.configuration.chrome_process_count,
            "test_duration_seconds": report.test_duration_seconds,
            **report.summary,
        }
        for report in reports
    )


def _latency_summary(values: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values),
        average_seconds=statistics.fmean(values) if values else None,
        p50_seconds=percentile(values, 50),
        p95_seconds=percentile(values, 95),
    )


def _average_peak(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "average": statistics.fmean(values) if values else None,
        "peak": max(values) if values else None,
    }


def _peak(values: Sequence[float]) -> float | None:
    return max(values) if values else None


def _metric(metrics: PrometheusMetrics, name: str) -> float | None:
    value = metrics.registry.get_sample_value(name)
    return float(value) if value is not None else None


def _counter(metrics: PrometheusMetrics, name: str) -> int:
    return int(_metric(metrics, name) or 0)


def prometheus_histogram_summary(
    metrics: PrometheusMetrics,
    name: str,
) -> dict[str, int | float | None]:
    count = int(_metric(metrics, f"{name}_count") or 0)
    total = _metric(metrics, f"{name}_sum") or 0.0
    return {
        "count": count,
        "average_seconds": total / count if count else None,
        "p50_seconds": _histogram_percentile(metrics, name, count, 0.50),
        "p95_seconds": _histogram_percentile(metrics, name, count, 0.95),
    }


def _histogram_percentile(
    metrics: PrometheusMetrics,
    name: str,
    count: int,
    quantile: float,
) -> float | None:
    """Return the first matching Prometheus bucket boundary (an upper-bound estimate)."""

    if count == 0:
        return None
    target = count * quantile
    for boundary in _HISTOGRAM_BUCKETS:
        cumulative = metrics.registry.get_sample_value(
            f"{name}_bucket",
            {"le": str(float(boundary))},
        )
        if cumulative is not None and cumulative >= target:
            return float(boundary)
    return None


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _pair(summary: dict[str, object], name: str) -> str:
    value = summary[name]
    assert isinstance(value, dict)
    return f"{value['average']} / {value['peak']}"


def _seconds(value: object) -> str:
    return f"{value:.3f}s" if isinstance(value, (int, float)) else "UNKNOWN"
