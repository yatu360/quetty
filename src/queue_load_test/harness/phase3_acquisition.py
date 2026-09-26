"""Explicitly gated Phase 3 acquisition benchmark for 1,000 Queue IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings, get_settings
from queue_load_test.harness.resource_benchmark import (
    ProcessResourceProbe,
    ProcessResourceSnapshot,
    PsutilProcessResourceProbe,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics, configure_structured_logging
from queue_load_test.models import SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationMetrics,
    CreationOutcome,
    CreationWorkItem,
    QueueSessionCreator,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore

_STAGING_GATE = "RUN_PHASE3_ACQUISITION_BENCHMARK"


@dataclass(frozen=True, slots=True)
class AcquisitionConfiguration:
    target_queue_ids: int
    session_mode: str
    chrome_process_count: int
    max_contexts_per_browser: int
    max_active_contexts: int
    creation_workers: int
    creation_queue_capacity: int

    @classmethod
    def from_settings(cls, settings: Settings) -> AcquisitionConfiguration:
        return cls(
            target_queue_ids=settings.target_queue_ids,
            session_mode=settings.session_mode.value,
            chrome_process_count=settings.chrome_process_count,
            max_contexts_per_browser=settings.max_contexts_per_browser,
            max_active_contexts=settings.max_active_contexts,
            creation_workers=settings.creation_workers,
            creation_queue_capacity=settings.creation_queue_capacity,
        )


@dataclass(frozen=True, slots=True)
class AveragePeak:
    average: float | None
    peak: float | None


@dataclass(frozen=True, slots=True)
class LatencySummary:
    count: int
    average_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    maximum_seconds: float | None


@dataclass(frozen=True, slots=True)
class AcquisitionResourceSample:
    timestamp: datetime
    application_cpu_percent: float | None
    application_ram_bytes: int | None
    chrome_cpu_percent: float | None
    chrome_ram_bytes: int | None
    observed_chrome_processes: int | None
    managed_chrome_processes: int | None
    active_contexts: int | None
    creation_queue_depth: float | None


@dataclass(frozen=True, slots=True)
class AcquisitionResourceSummary:
    application_cpu_percent: AveragePeak
    application_ram_bytes: AveragePeak
    chrome_cpu_percent: AveragePeak
    chrome_ram_bytes: AveragePeak
    observed_chrome_processes: AveragePeak
    managed_chrome_processes: AveragePeak
    active_contexts: AveragePeak
    creation_queue_depth: AveragePeak


@dataclass(frozen=True, slots=True)
class Phase3AcquisitionReport:
    generated_at: datetime
    staging_status: str
    status: str
    configuration: AcquisitionConfiguration
    initial_successful_unique_ids: int
    final_successful_unique_ids: int
    target_reached: bool
    total_creation_attempts: int
    completed_work_items: int
    unique_queue_ids_acquired: int
    duplicates: int
    failed_attempts: int
    temporary_failures: int
    temporary_failure_outcomes: int
    permanent_failures: int
    retry_count: int
    wall_duration_seconds: float
    total_creation_duration_seconds: float
    sessions_per_second: float
    creation_latency: LatencySummary
    navigation_failures: int
    browser_crashes: int
    context_creation_failures: int
    cleanup_failures: int
    peak_active_contexts: int
    resources: AcquisitionResourceSummary
    samples: tuple[AcquisitionResourceSample, ...]
    timed_out: bool
    scope_warning: str = (
        "Results apply only to this authorised Phase 3 host, event, and configuration; "
        "they do not predict 10,000-session behavior."
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
        return "\n".join(
            (
                "Phase 3 Queue ID acquisition benchmark",
                "",
                self.scope_warning,
                "",
                f"Staging status: {self.staging_status}",
                f"Result: {self.status}",
                (
                    "Unique Queue IDs: "
                    f"{self.initial_successful_unique_ids} -> "
                    f"{self.final_successful_unique_ids} / "
                    f"{self.configuration.target_queue_ids}"
                ),
                f"New unique IDs: {self.unique_queue_ids_acquired}",
                f"Creation attempts: {self.total_creation_attempts}",
                f"Duplicates: {self.duplicates}",
                f"Failed attempts: {self.failed_attempts}",
                f"Retries: {self.retry_count}",
                f"Throughput: {self.sessions_per_second:.3f} sessions/s",
                (
                    "Creation latency p50/p95: "
                    f"{_seconds(self.creation_latency.p50_seconds)} / "
                    f"{_seconds(self.creation_latency.p95_seconds)}"
                ),
                f"Peak active contexts: {self.peak_active_contexts}",
                f"Navigation failures: {self.navigation_failures}",
                f"Browser crashes: {self.browser_crashes}",
                f"Timed out: {self.timed_out}",
            )
        ) + "\n"


class _RecordingCreator:
    def __init__(self, creator: QueueSessionCreator) -> None:
        self._creator = creator
        self.latencies: list[float] = []

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        outcome = await self._creator.create(work_item)
        self.latencies.append(outcome.duration_seconds)
        return outcome


async def run_phase3_acquisition_benchmark(
    settings: Settings,
    *,
    report_path: Path,
    sample_interval_seconds: float,
    creation_timeout_seconds: float,
    process_probe: ProcessResourceProbe | None = None,
    environment_gate: str = _STAGING_GATE,
) -> Phase3AcquisitionReport:
    """Acquire or resume toward 1,000 unique IDs under explicit staging gates."""

    _validate_phase3_profile(settings)
    if sample_interval_seconds <= 0 or creation_timeout_seconds <= 0:
        raise ValueError("sample interval and creation timeout must be positive")
    if os.environ.get("RUN_STAGING_TESTS") != "1":
        raise RuntimeError("Set RUN_STAGING_TESTS=1 to run against authorised staging")
    if os.environ.get(environment_gate) != "1":
        raise RuntimeError(f"Set {environment_gate}=1 to run the Phase 3 acquisition benchmark")

    metrics = PrometheusMetrics()
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = BrowserManager.from_settings(settings, observability=metrics)
    creator = _RecordingCreator(
        QueueSessionCreator(
            browser_manager=browser_manager,
            repository=repository,
            state_store=state_store,
            staging_url=str(settings.staging_url),
            state_directory=settings.state_directory,
            mode=settings.session_mode,
            observability=metrics,
        )
    )
    controller = SessionCreationController.from_settings(
        settings,
        repository=repository,
        handler=creator,
        observability=metrics,
    )
    probe = process_probe or PsutilProcessResourceProbe()
    samples: list[AcquisitionResourceSample] = []
    sampling_stop = asyncio.Event()
    sampling_task: asyncio.Task[None] | None = None
    initial_count = 0
    final_count = initial_count
    timed_out = False
    controller_metrics = controller.metrics
    run_started = time.perf_counter()
    try:
        initial_count = await repository.count_successful_queue_ids()
        final_count = initial_count
        if initial_count < settings.target_queue_ids:
            await browser_manager.start()
            sampling_task = asyncio.create_task(
                _sample_resources(
                    samples,
                    browser_manager,
                    metrics,
                    probe,
                    sampling_stop,
                    sample_interval_seconds,
                ),
                name="phase3-acquisition-resource-sampler",
            )
        try:
            async with asyncio.timeout(creation_timeout_seconds):
                controller_metrics = await controller.run()
        except TimeoutError:
            timed_out = True
            controller_metrics = controller.metrics
        final_count = await repository.count_successful_queue_ids()
        if browser_manager.started:
            await _capture_resource_sample(samples, browser_manager, metrics, probe)
    finally:
        sampling_stop.set()
        if sampling_task is not None:
            await sampling_task
        if browser_manager.started:
            await browser_manager.shutdown()
        await repository.close()

    elapsed = time.perf_counter() - run_started
    report = _build_report(
        settings=settings,
        controller_metrics=controller_metrics,
        latencies=creator.latencies,
        samples=samples,
        metrics=metrics,
        initial_count=initial_count,
        final_count=final_count,
        elapsed_seconds=elapsed,
        timed_out=timed_out,
    )
    report.write_json(report_path)
    return report


def _build_report(
    *,
    settings: Settings,
    controller_metrics: CreationMetrics,
    latencies: Sequence[float],
    samples: Sequence[AcquisitionResourceSample],
    metrics: PrometheusMetrics,
    initial_count: int,
    final_count: int,
    elapsed_seconds: float,
    timed_out: bool,
) -> Phase3AcquisitionReport:
    target_reached = final_count >= settings.target_queue_ids
    unique_ids_acquired = max(0, final_count - initial_count)
    total_attempts = max(
        controller_metrics.attempts,
        _counter(metrics, "queue_creation_attempts_total"),
    )
    duplicates = max(
        controller_metrics.duplicates,
        _counter(metrics, "queue_creation_duplicates_total"),
    )
    temporary_failures = max(
        controller_metrics.temporary_failures,
        _counter(metrics, "queue_creation_transient_failures_total"),
    )
    permanent_failures = max(
        controller_metrics.permanent_failures,
        _counter(metrics, "queue_creation_permanent_failures_total"),
    )
    retries = max(
        controller_metrics.retries,
        _counter(metrics, "queue_creation_retries_total"),
    )
    peak_from_metrics = int(
        metrics.registry.get_sample_value("active_browser_contexts_peak") or 0
    )
    peak_from_samples = max(
        (sample.active_contexts or 0 for sample in samples),
        default=0,
    )
    return Phase3AcquisitionReport(
        generated_at=datetime.now(UTC),
        staging_status="COMPLETED" if target_reached else "PARTIAL",
        status="TARGET_REACHED" if target_reached else "TARGET_NOT_REACHED",
        configuration=AcquisitionConfiguration.from_settings(settings),
        initial_successful_unique_ids=initial_count,
        final_successful_unique_ids=final_count,
        target_reached=target_reached,
        total_creation_attempts=total_attempts,
        completed_work_items=controller_metrics.completed_work_items,
        unique_queue_ids_acquired=unique_ids_acquired,
        duplicates=duplicates,
        failed_attempts=max(0, total_attempts - unique_ids_acquired),
        temporary_failures=temporary_failures,
        temporary_failure_outcomes=controller_metrics.temporary_failure_outcomes,
        permanent_failures=permanent_failures,
        retry_count=retries,
        wall_duration_seconds=elapsed_seconds,
        total_creation_duration_seconds=(
            controller_metrics.total_creation_duration_seconds
        ),
        sessions_per_second=(
            unique_ids_acquired / max(elapsed_seconds, 1e-9)
        ),
        creation_latency=_latency_summary(latencies),
        navigation_failures=_counter(metrics, "navigation_failures_total"),
        browser_crashes=_counter(metrics, "browser_crashes_total"),
        context_creation_failures=_counter(
            metrics, "browser_context_creation_failures_total"
        ),
        cleanup_failures=_counter(metrics, "browser_cleanup_failures_total"),
        peak_active_contexts=max(peak_from_metrics, peak_from_samples),
        resources=_summarize_resources(samples),
        samples=tuple(samples),
        timed_out=timed_out,
    )


async def _sample_resources(
    samples: list[AcquisitionResourceSample],
    browser_manager: BrowserManager,
    metrics: PrometheusMetrics,
    probe: ProcessResourceProbe,
    stop_event: asyncio.Event,
    interval_seconds: float,
) -> None:
    while True:
        await _capture_resource_sample(samples, browser_manager, metrics, probe)
        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def _capture_resource_sample(
    samples: list[AcquisitionResourceSample],
    browser_manager: BrowserManager,
    metrics: PrometheusMetrics,
    probe: ProcessResourceProbe,
) -> None:
    try:
        resource = probe.sample()
    except Exception:  # noqa: BLE001 - optional resource sampling must not stop acquisition
        resource = ProcessResourceSnapshot()
    try:
        capacity = await browser_manager.capacity()
    except Exception:  # noqa: BLE001 - a disappearing process remains observable in counters
        capacity = None
    samples.append(
        AcquisitionResourceSample(
            timestamp=datetime.now(UTC),
            application_cpu_percent=resource.application_cpu_percent,
            application_ram_bytes=resource.application_ram_bytes,
            chrome_cpu_percent=resource.chrome_cpu_percent,
            chrome_ram_bytes=resource.chrome_ram_bytes,
            observed_chrome_processes=resource.observed_chrome_processes,
            managed_chrome_processes=(capacity.connected_processes if capacity else None),
            active_contexts=(capacity.active_contexts if capacity else None),
            creation_queue_depth=_metric(metrics, "queue_creation_queue_depth"),
        )
    )


def _summarize_resources(
    samples: Sequence[AcquisitionResourceSample],
) -> AcquisitionResourceSummary:
    def values(name: str) -> list[float]:
        return [
            float(value)
            for sample in samples
            if (value := getattr(sample, name)) is not None
        ]

    return AcquisitionResourceSummary(
        application_cpu_percent=_average_peak(values("application_cpu_percent")),
        application_ram_bytes=_average_peak(values("application_ram_bytes")),
        chrome_cpu_percent=_average_peak(values("chrome_cpu_percent")),
        chrome_ram_bytes=_average_peak(values("chrome_ram_bytes")),
        observed_chrome_processes=_average_peak(values("observed_chrome_processes")),
        managed_chrome_processes=_average_peak(values("managed_chrome_processes")),
        active_contexts=_average_peak(values("active_contexts")),
        creation_queue_depth=_average_peak(values("creation_queue_depth")),
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


def _counter(metrics: PrometheusMetrics, name: str) -> int:
    return int(_metric(metrics, name) or 0)


def _validate_phase3_profile(settings: Settings) -> None:
    expected = {
        "TARGET_QUEUE_IDS": settings.target_queue_ids == 1000,
        "SESSION_MODE": settings.session_mode is SessionMode.HYBRID,
        "CHROME_PROCESS_COUNT": settings.chrome_process_count == 2,
        "MAX_CONTEXTS_PER_BROWSER": settings.max_contexts_per_browser == 25,
        "MAX_ACTIVE_CONTEXTS": settings.max_active_contexts == 50,
        "DATABASE_URL": settings.database_url.startswith("sqlite:///"),
    }
    invalid = [name for name, matches in expected.items() if not matches]
    if invalid:
        raise ValueError("Phase 3 acquisition profile mismatch: " + ", ".join(invalid))


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}s"


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the opt-in Phase 3 1,000-ID acquisition benchmark"
    )
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("phase3-acquisition-benchmark.json"),
    )
    parser.add_argument("--sample-interval-seconds", type=float, default=5.0)
    parser.add_argument("--creation-timeout-seconds", type=float, default=14_400.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable staging traffic")
    configure_structured_logging()
    report = asyncio.run(
        run_phase3_acquisition_benchmark(
            get_settings(),
            report_path=args.report,
            sample_interval_seconds=args.sample_interval_seconds,
            creation_timeout_seconds=args.creation_timeout_seconds,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
