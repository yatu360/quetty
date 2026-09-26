"""Gated Phase 4 preflight and 10,000-identity acquisition benchmark."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import statistics
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings, get_settings
from queue_load_test.harness.resource_benchmark import (
    PsutilProcessResourceProbe,
    ResourceBenchmarkRecorder,
    ResourceSample,
    prometheus_histogram_summary,
    sample_resources,
)
from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.metrics import PrometheusMetrics, configure_structured_logging
from queue_load_test.models import SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
    QueueSessionCreator,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker

_STAGING_GATE = "RUN_PHASE4_ACQUISITION_BENCHMARK"
_DEFAULT_MINIMUM_FREE_DISK_BYTES = 1_073_741_824


class PreflightStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: PreflightStatus
    detail: str


@dataclass(frozen=True, slots=True)
class Phase4PreflightReport:
    generated_at: datetime
    ready: bool
    target_queue_ids: int
    existing_successful_queue_ids: int | None
    free_disk_bytes: int | None
    checks: tuple[PreflightCheck, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        _write_json(path, self.to_dict())

    def render_text(self) -> str:
        lines = [
            "Phase 4 acquisition preflight",
            f"Ready: {self.ready}",
            f"Target Queue IDs: {self.target_queue_ids}",
            f"Existing successful Queue IDs: {self.existing_successful_queue_ids}",
            f"Free disk bytes: {self.free_disk_bytes}",
        ]
        lines.extend(
            f"- {check.name}: {check.status.value} — {check.detail}"
            for check in self.checks
        )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class LatencySummary:
    count: int
    average_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    maximum_seconds: float | None


@dataclass(frozen=True, slots=True)
class WorkerResult:
    worker_id: str
    completed: int
    successes: int
    failures: int
    successes_per_second: float


@dataclass(frozen=True, slots=True)
class PostRunVerification:
    final_active_contexts: int | None
    leased_sessions: int
    expired_leases: int
    state_consistent: bool
    state_files: int
    state_findings: dict[str, int]


@dataclass(frozen=True, slots=True)
class Phase4AcquisitionReport:
    generated_at: datetime
    staging_status: str
    status: str
    distribution_model: str
    configuration: dict[str, object]
    preflight: Phase4PreflightReport
    initial_successful_unique_ids: int
    final_successful_unique_ids: int
    target_reached: bool
    target_overshoot: int
    total_creation_attempts: int
    completed_work_items: int
    unique_queue_ids_acquired: int
    duplicates: int
    temporary_failures: int
    temporary_failure_outcomes: int
    permanent_failures: int
    retries: int
    wall_duration_seconds: float
    total_creation_duration_seconds: float
    sessions_per_second: float
    creation_latency: LatencySummary
    context_acquisition_latency: dict[str, int | float | None]
    context_acquisition_wait: dict[str, int | float | None]
    navigation_latency: dict[str, int | float | None]
    browser_crashes: int
    context_creation_failures: int
    navigation_failures: int
    state_persistence_failures: int
    cleanup_failures: int
    peak_active_contexts: int
    resources: dict[str, object]
    resource_samples: tuple[ResourceSample, ...]
    workers: tuple[WorkerResult, ...]
    worker_failures: int
    lease_conflicts: int
    lease_recoveries: int
    timed_out: bool
    interrupted: bool
    post_run: PostRunVerification
    scope_warning: str = (
        "Results apply only to this authorised host, event, and bounded configuration; "
        "they do not predict another environment or a larger scale."
    )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def write_json(self, path: Path) -> None:
        _write_json(path, self.to_dict())

    def render_text(self) -> str:
        return "\n".join(
            (
                "Phase 4 10,000 Queue ID acquisition benchmark",
                "",
                self.scope_warning,
                "",
                f"Staging status: {self.staging_status}",
                f"Result: {self.status}",
                f"Deployment: {self.distribution_model}",
                (
                    "Unique Queue IDs: "
                    f"{self.initial_successful_unique_ids} -> "
                    f"{self.final_successful_unique_ids}"
                ),
                f"Target overshoot: {self.target_overshoot}",
                f"Run duration: {self.wall_duration_seconds:.3f}s",
                f"Throughput: {self.sessions_per_second:.3f} sessions/s",
                (
                    "Creation latency p50/p95: "
                    f"{_seconds(self.creation_latency.p50_seconds)} / "
                    f"{_seconds(self.creation_latency.p95_seconds)}"
                ),
                f"Attempts / retries: {self.total_creation_attempts} / {self.retries}",
                f"Duplicates: {self.duplicates}",
                (
                    "Transient / permanent failures: "
                    f"{self.temporary_failures} / {self.permanent_failures}"
                ),
                f"Browser crashes: {self.browser_crashes}",
                f"Navigation failures: {self.navigation_failures}",
                f"State persistence failures: {self.state_persistence_failures}",
                f"Peak active contexts: {self.peak_active_contexts}",
                f"Final active contexts: {self.post_run.final_active_contexts}",
                (
                    f"Leased / expired leases: {self.post_run.leased_sessions} / "
                    f"{self.post_run.expired_leases}"
                ),
                f"State consistency: {self.post_run.state_consistent}",
                f"Timed out / interrupted: {self.timed_out} / {self.interrupted}",
            )
        ) + "\n"


class _RecordingCreator:
    def __init__(
        self,
        creator: QueueSessionCreator,
        recorder: ResourceBenchmarkRecorder,
    ) -> None:
        self._creator = creator
        self._recorder = recorder
        self.latencies: list[float] = []

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        outcome = await self._creator.create(work_item)
        self.latencies.append(outcome.duration_seconds)
        self._recorder.record_creation(
            outcome.duration_seconds,
            success=outcome.kind is CreationOutcomeKind.SUCCESS,
        )
        return outcome


async def run_phase4_preflight(
    settings: Settings,
    *,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    browser_manager: BrowserManager,
    metrics: PrometheusMetrics,
    minimum_free_disk_bytes: int = _DEFAULT_MINIMUM_FREE_DISK_BYTES,
) -> Phase4PreflightReport:
    """Check local prerequisites without navigating to the staging URL."""

    checks: list[PreflightCheck] = []
    existing_count: int | None = None
    free_disk_bytes: int | None = None

    try:
        _validate_phase4_profile(settings)
    except ValueError as exc:
        checks.append(PreflightCheck("configured_capacity", PreflightStatus.FAIL, str(exc)))
    else:
        checks.append(
            PreflightCheck(
                "configured_capacity",
                PreflightStatus.PASS,
                "bounded single-machine Phase 4 profile",
            )
        )

    hostname = urlsplit(str(settings.staging_url)).hostname
    if not hostname or hostname in {"example.test", "staging.example.test"}:
        checks.append(
            PreflightCheck(
                "staging_url",
                PreflightStatus.FAIL,
                "configure the authorised non-placeholder staging URL",
            )
        )
    else:
        checks.append(PreflightCheck("staging_url", PreflightStatus.PASS, hostname))

    recovery = None
    try:
        await repository.initialize()
        existing_count = await repository.count_successful_queue_ids()
        recovery = await repository.recovery_summary(now=datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - preflight reports instead of aborting early
        checks.append(
            PreflightCheck("database", PreflightStatus.FAIL, type(exc).__name__)
        )
    else:
        checks.append(
            PreflightCheck(
                "database",
                PreflightStatus.PASS,
                f"reachable; {existing_count} successful Queue IDs",
            )
        )
        lease_status = (
            PreflightStatus.WARN
            if recovery.leased_sessions or recovery.expired_leases
            else PreflightStatus.PASS
        )
        checks.append(
            PreflightCheck(
                "stale_leases",
                lease_status,
                (
                    f"active={recovery.leased_sessions}, "
                    f"expired_recoverable={recovery.expired_leases}"
                ),
            )
        )

    probe_session_id = f"preflight-{uuid4()}"
    try:
        await state_store.save(probe_session_id, {"cookies": [], "origins": []})
        if not await state_store.delete(probe_session_id):
            raise OSError("preflight state file could not be removed")
    except Exception as exc:  # noqa: BLE001 - sanitized preflight result
        checks.append(
            PreflightCheck("state_storage", PreflightStatus.FAIL, type(exc).__name__)
        )
    else:
        checks.append(
            PreflightCheck("state_storage", PreflightStatus.PASS, "atomic write/delete succeeded")
        )

    try:
        state_store.directory.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(state_store.directory)
        free_disk_bytes = usage.free
    except OSError as exc:
        checks.append(PreflightCheck("disk_space", PreflightStatus.FAIL, type(exc).__name__))
    else:
        status = (
            PreflightStatus.PASS
            if free_disk_bytes >= minimum_free_disk_bytes
            else PreflightStatus.FAIL
        )
        checks.append(
            PreflightCheck(
                "disk_space",
                status,
                f"free={free_disk_bytes}, required={minimum_free_disk_bytes}",
            )
        )

    try:
        await browser_manager.start()
        owned = await browser_manager.create_context()
        await owned.close()
        capacity = await browser_manager.capacity()
        if capacity.active_contexts != 0:
            raise RuntimeError("preflight context did not close")
    except Exception as exc:  # noqa: BLE001 - sanitized preflight result
        checks.append(PreflightCheck("browser_launch", PreflightStatus.FAIL, type(exc).__name__))
    else:
        checks.append(
            PreflightCheck(
                "browser_launch",
                PreflightStatus.PASS,
                f"connected_processes={capacity.connected_processes}; contexts_returned_to_zero",
            )
        )

    try:
        rendered_metrics = metrics.render()
        if b"queue_sessions_requested" not in rendered_metrics:
            raise RuntimeError("required metrics missing")
    except Exception as exc:  # noqa: BLE001 - sanitized preflight result
        checks.append(PreflightCheck("metrics", PreflightStatus.FAIL, type(exc).__name__))
    else:
        checks.append(
            PreflightCheck(
                "metrics",
                PreflightStatus.PASS,
                f"registry available; configured port={settings.prometheus_port}",
            )
        )

    checks.append(
        PreflightCheck(
            "shutdown_recovery",
            PreflightStatus.PASS if recovery is not None else PreflightStatus.FAIL,
            (
                f"shutdown_timeout={settings.shutdown_timeout_seconds}s; recovery summary available"
                if recovery is not None
                else "recovery summary unavailable"
            ),
        )
    )
    return Phase4PreflightReport(
        generated_at=datetime.now(UTC),
        ready=all(check.status is not PreflightStatus.FAIL for check in checks),
        target_queue_ids=settings.target_queue_ids,
        existing_successful_queue_ids=existing_count,
        free_disk_bytes=free_disk_bytes,
        checks=tuple(checks),
    )


async def run_phase4_acquisition_benchmark(
    settings: Settings,
    *,
    report_path: Path,
    preflight_report_path: Path,
    sample_interval_seconds: float,
    creation_timeout_seconds: float,
    minimum_free_disk_bytes: int = _DEFAULT_MINIMUM_FREE_DISK_BYTES,
    environment_gate: str = _STAGING_GATE,
) -> Phase4AcquisitionReport:
    """Acquire or safely resume toward 10,000 unique IDs under explicit gates."""

    _validate_phase4_profile(settings)
    if sample_interval_seconds <= 0 or creation_timeout_seconds <= 0:
        raise ValueError("sample interval and creation timeout must be positive")
    if os.environ.get("RUN_STAGING_TESTS") != "1":
        raise RuntimeError("Set RUN_STAGING_TESTS=1 to run against authorised staging")
    if os.environ.get(environment_gate) != "1":
        raise RuntimeError(f"Set {environment_gate}=1 to run the Phase 4 acquisition benchmark")

    metrics = PrometheusMetrics()
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = BrowserManager.from_settings(settings, observability=metrics)
    preflight = await run_phase4_preflight(
        settings,
        repository=repository,
        state_store=state_store,
        browser_manager=browser_manager,
        metrics=metrics,
        minimum_free_disk_bytes=minimum_free_disk_bytes,
    )
    preflight.write_json(preflight_report_path)
    if not preflight.ready:
        await browser_manager.shutdown()
        await repository.close()
        raise RuntimeError("Phase 4 acquisition preflight failed; inspect its report")

    recorder = ResourceBenchmarkRecorder()
    creator = _RecordingCreator(
        QueueSessionCreator(
            browser_manager=browser_manager,
            repository=repository,
            state_store=state_store,
            staging_url=str(settings.staging_url),
            state_directory=settings.state_directory,
            mode=settings.session_mode,
            observability=metrics,
        ),
        recorder,
    )
    controller = SessionCreationController.from_settings(
        settings,
        repository=repository,
        handler=creator,
        observability=metrics,
    )
    stop_event = asyncio.Event()
    installed_signals = _install_signal_handlers(stop_event)
    sampling_stop = asyncio.Event()
    sampling_task = asyncio.create_task(
        sample_resources(
            stop_event=sampling_stop,
            recorder=recorder,
            browser_manager=browser_manager,
            metrics=metrics,
            process_probe=PsutilProcessResourceProbe(),
            interval_seconds=sample_interval_seconds,
        ),
        name="phase4-acquisition-resource-sampler",
    )
    initial_count = preflight.existing_successful_queue_ids or 0
    final_count = initial_count
    timed_out = False
    interrupted = False
    controller_metrics = controller.metrics
    final_active_contexts: int | None = None
    recovery = None
    consistency = None
    run_started = time.perf_counter()
    try:
        controller_task = asyncio.create_task(
            controller.run(stop_event),
            name="phase4-acquisition-controller",
        )
        stop_task = asyncio.create_task(stop_event.wait(), name="phase4-acquisition-stop")
        done, _ = await asyncio.wait(
            {controller_task, stop_task},
            timeout=creation_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if controller_task in done:
            controller_metrics = controller_task.result()
        else:
            timed_out = stop_task not in done
            interrupted = stop_task in done
            stop_event.set()
            try:
                controller_metrics = await asyncio.wait_for(
                    controller_task,
                    timeout=settings.shutdown_timeout_seconds,
                )
            except TimeoutError:
                controller_task.cancel()
                await asyncio.gather(controller_task, return_exceptions=True)
                controller_metrics = controller.metrics
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        final_count = await repository.count_successful_queue_ids()
        capacity = await browser_manager.capacity()
        final_active_contexts = capacity.active_contexts
        recovery = await repository.recovery_summary(now=datetime.now(UTC))
        consistency = await StateConsistencyChecker(repository, state_store).check()
    finally:
        sampling_stop.set()
        await sampling_task
        _remove_signal_handlers(installed_signals)
        await browser_manager.shutdown()
        await repository.close()

    elapsed = time.perf_counter() - run_started
    recorder.creation_phase_seconds = elapsed
    if recovery is None or consistency is None:
        raise RuntimeError("Post-run verification did not complete")
    state_findings: dict[str, int] = {}
    for finding in consistency.findings:
        state_findings[finding.kind] = state_findings.get(finding.kind, 0) + 1
    workers = tuple(
        WorkerResult(
            worker_id=f"creation-{index}",
            completed=controller_metrics.worker_completed.get(index, 0),
            successes=controller_metrics.worker_successes.get(index, 0),
            failures=controller_metrics.worker_failures.get(index, 0),
            successes_per_second=(
                controller_metrics.worker_successes.get(index, 0) / max(elapsed, 1e-9)
            ),
        )
        for index in range(settings.creation_workers)
    )
    target_reached = final_count >= settings.target_queue_ids
    report = Phase4AcquisitionReport(
        generated_at=datetime.now(UTC),
        staging_status="COMPLETED" if target_reached else "PARTIAL",
        status="TARGET_REACHED" if target_reached else "TARGET_NOT_REACHED",
        distribution_model="single_machine",
        configuration=_configuration(settings),
        preflight=preflight,
        initial_successful_unique_ids=initial_count,
        final_successful_unique_ids=final_count,
        target_reached=target_reached,
        target_overshoot=max(0, final_count - settings.target_queue_ids),
        total_creation_attempts=controller_metrics.attempts,
        completed_work_items=controller_metrics.completed_work_items,
        unique_queue_ids_acquired=max(0, final_count - initial_count),
        duplicates=controller_metrics.duplicates,
        temporary_failures=controller_metrics.temporary_failures,
        temporary_failure_outcomes=controller_metrics.temporary_failure_outcomes,
        permanent_failures=controller_metrics.permanent_failures,
        retries=controller_metrics.retries,
        wall_duration_seconds=elapsed,
        total_creation_duration_seconds=controller_metrics.total_creation_duration_seconds,
        sessions_per_second=max(0, final_count - initial_count) / max(elapsed, 1e-9),
        creation_latency=_latency_summary(creator.latencies),
        context_acquisition_latency=prometheus_histogram_summary(
            metrics,
            "browser_context_acquisition_duration_seconds",
        ),
        context_acquisition_wait=prometheus_histogram_summary(
            metrics,
            "browser_context_acquisition_wait_seconds",
        ),
        navigation_latency=prometheus_histogram_summary(
            metrics,
            "navigation_duration_seconds",
        ),
        browser_crashes=_counter(metrics, "browser_crashes_total"),
        context_creation_failures=_counter(
            metrics,
            "browser_context_creation_failures_total",
        ),
        navigation_failures=_counter(metrics, "navigation_failures_total"),
        state_persistence_failures=_counter(metrics, "state_persistence_failures_total"),
        cleanup_failures=_counter(metrics, "browser_cleanup_failures_total"),
        peak_active_contexts=int(_metric(metrics, "active_browser_contexts_peak") or 0),
        resources=recorder.summary(metrics, elapsed),
        resource_samples=tuple(recorder.samples),
        workers=workers,
        worker_failures=sum(worker.failures for worker in workers),
        lease_conflicts=0,
        lease_recoveries=0,
        timed_out=timed_out,
        interrupted=interrupted,
        post_run=PostRunVerification(
            final_active_contexts=final_active_contexts,
            leased_sessions=recovery.leased_sessions,
            expired_leases=recovery.expired_leases,
            state_consistent=consistency.is_consistent,
            state_files=consistency.state_files,
            state_findings=state_findings,
        ),
    )
    report.write_json(report_path)
    return report


def _validate_phase4_profile(settings: Settings) -> None:
    expected = {
        "TARGET_QUEUE_IDS": settings.target_queue_ids == 10_000,
        "SESSION_MODE": settings.session_mode is SessionMode.HYBRID,
        "CHROME_PROCESS_COUNT": settings.chrome_process_count in {1, 2},
        "MAX_CONTEXTS_PER_BROWSER": settings.max_contexts_per_browser <= 25,
        "MAX_ACTIVE_CONTEXTS": settings.max_active_contexts <= 50,
        "CREATION_WORKERS": settings.creation_workers <= 10,
        "CREATION_QUEUE_CAPACITY": settings.creation_queue_capacity <= 10,
        "DATABASE_URL": settings.database_url.startswith("sqlite:///"),
    }
    invalid = [name for name, matches in expected.items() if not matches]
    if invalid:
        raise ValueError("Phase 4 acquisition profile mismatch: " + ", ".join(invalid))


def _configuration(settings: Settings) -> dict[str, object]:
    return {
        "target_queue_ids": settings.target_queue_ids,
        "session_mode": settings.session_mode.value,
        "chrome_process_count": settings.chrome_process_count,
        "max_contexts_per_browser": settings.max_contexts_per_browser,
        "max_active_contexts": settings.max_active_contexts,
        "creation_workers": settings.creation_workers,
        "creation_queue_capacity": settings.creation_queue_capacity,
        "database_backend": "sqlite",
    }


def _latency_summary(values: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values),
        average_seconds=statistics.fmean(values) if values else None,
        p50_seconds=percentile(values, 50),
        p95_seconds=percentile(values, 95),
        maximum_seconds=max(values) if values else None,
    )


def _metric(metrics: PrometheusMetrics, name: str) -> float | None:
    value = metrics.registry.get_sample_value(name)
    return float(value) if value is not None else None


def _counter(metrics: PrometheusMetrics, name: str) -> int:
    return int(_metric(metrics, name) or 0)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _seconds(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value:.3f}s"


def _install_signal_handlers(stop_event: asyncio.Event) -> tuple[signal.Signals, ...]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signal_number)
    return tuple(installed)


def _remove_signal_handlers(installed: tuple[signal.Signals, ...]) -> None:
    loop = asyncio.get_running_loop()
    for signal_number in installed:
        loop.remove_signal_handler(signal_number)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight or run the opt-in Phase 4 10,000-ID acquisition benchmark"
    )
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("phase4-acquisition-benchmark.json"),
    )
    parser.add_argument(
        "--preflight-report",
        type=Path,
        default=Path("phase4-acquisition-preflight.json"),
    )
    parser.add_argument("--sample-interval-seconds", type=float, default=5.0)
    parser.add_argument("--creation-timeout-seconds", type=float, default=86_400.0)
    parser.add_argument(
        "--minimum-free-disk-bytes",
        type=int,
        default=_DEFAULT_MINIMUM_FREE_DISK_BYTES,
    )
    return parser.parse_args()


async def _run_preflight_only(settings: Settings, args: argparse.Namespace) -> None:
    metrics = PrometheusMetrics()
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = BrowserManager.from_settings(settings, observability=metrics)
    try:
        report = await run_phase4_preflight(
            settings,
            repository=repository,
            state_store=state_store,
            browser_manager=browser_manager,
            metrics=metrics,
            minimum_free_disk_bytes=args.minimum_free_disk_bytes,
        )
        report.write_json(args.preflight_report)
        print(report.render_text())
        if not report.ready:
            raise SystemExit(2)
    finally:
        await browser_manager.shutdown()
        await repository.close()


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    configure_structured_logging()
    if args.preflight_only:
        asyncio.run(_run_preflight_only(settings, args))
        return
    if not args.confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable staging traffic")
    report = asyncio.run(
        run_phase4_acquisition_benchmark(
            settings,
            report_path=args.report,
            preflight_report_path=args.preflight_report,
            sample_interval_seconds=args.sample_interval_seconds,
            creation_timeout_seconds=args.creation_timeout_seconds,
            minimum_free_disk_bytes=args.minimum_free_disk_bytes,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
