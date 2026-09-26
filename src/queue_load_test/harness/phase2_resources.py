"""Explicitly gated Phase 2 resource and stability benchmark."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings, get_settings
from queue_load_test.harness.resource_benchmark import (
    PsutilProcessResourceProbe,
    ResourceBenchmarkRecorder,
    ResourceBenchmarkReport,
    sample_resources,
)
from queue_load_test.metrics import PrometheusMetrics, configure_structured_logging
from queue_load_test.models import QueueSession, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
    MonitoringOutcome,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionCreator,
    QueueSessionMonitor,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer, SessionRestoreResult


class _RecordingCreator:
    def __init__(
        self,
        creator: QueueSessionCreator,
        recorder: ResourceBenchmarkRecorder,
    ) -> None:
        self._creator = creator
        self._recorder = recorder

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        outcome = await self._creator.create(work_item)
        self._recorder.record_creation(
            outcome.duration_seconds,
            success=outcome.kind is CreationOutcomeKind.SUCCESS,
        )
        return outcome


class _RecordingRestorer:
    def __init__(
        self,
        restorer: QueueSessionRestorer,
        recorder: ResourceBenchmarkRecorder,
    ) -> None:
        self._restorer = restorer
        self._recorder = recorder

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        started = time.perf_counter()
        try:
            return await self._restorer.restore(session)
        finally:
            self._recorder.record_restore(time.perf_counter() - started)


class _RecordingMonitor:
    def __init__(
        self,
        monitor: QueueSessionMonitor,
        recorder: ResourceBenchmarkRecorder,
    ) -> None:
        self._monitor = monitor
        self._recorder = recorder

    async def check(self, session: QueueSession) -> MonitoringOutcome:
        started = time.perf_counter()
        try:
            return await self._monitor.check(session)
        finally:
            self._recorder.record_check(time.perf_counter() - started)


async def run_phase2_resource_benchmark(
    settings: Settings,
    *,
    report_path: Path,
    monitoring_seconds: float,
    sample_interval_seconds: float,
    creation_timeout_seconds: float,
) -> ResourceBenchmarkReport:
    """Acquire 100 sessions then observe bounded monitoring under explicit gates."""

    _validate_phase2_profile(settings)
    if monitoring_seconds < 0:
        raise ValueError("monitoring_seconds cannot be negative")
    if sample_interval_seconds <= 0 or creation_timeout_seconds <= 0:
        raise ValueError("sample interval and creation timeout must be positive")
    if os.environ.get("RUN_STAGING_TESTS") != "1":
        raise RuntimeError("Set RUN_STAGING_TESTS=1 to run against authorised staging")
    if os.environ.get("RUN_PHASE2_RESOURCE_BENCHMARK") != "1":
        raise RuntimeError(
            "Set RUN_PHASE2_RESOURCE_BENCHMARK=1 to run the Phase 2 resource benchmark"
        )

    recorder = ResourceBenchmarkRecorder()
    metrics = PrometheusMetrics()
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = BrowserManager.from_settings(settings, observability=metrics)
    creator = QueueSessionCreator(
        browser_manager=browser_manager,
        repository=repository,
        state_store=state_store,
        staging_url=str(settings.staging_url),
        state_directory=settings.state_directory,
        mode=settings.session_mode,
        observability=metrics,
    )
    controller = SessionCreationController(
        repository=repository,
        handler=_RecordingCreator(creator, recorder),
        target_queue_ids=settings.target_queue_ids,
        worker_count=settings.creation_workers,
        queue_capacity=settings.max_active_contexts,
        observability=metrics,
    )
    restorer = QueueSessionRestorer.from_settings(
        settings,
        browser_manager=browser_manager,
        repository=repository,
        state_store=state_store,
        observability=metrics,
    )
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=_RecordingRestorer(restorer, recorder),
        polling_policy=PollingPolicy.from_settings(settings),
        observability=metrics,
    )
    scheduler = ParkedSessionScheduler.from_settings(
        settings,
        repository=repository,
        handler=_RecordingMonitor(monitor, recorder),
        observability=metrics,
    )
    sampling_stop = asyncio.Event()
    sampling_task: asyncio.Task[None] | None = None
    process_probe = PsutilProcessResourceProbe()
    run_started = time.perf_counter()
    try:
        if await repository.count_successful_queue_ids() != 0:
            raise RuntimeError("Use a dedicated empty SQLite database for a resource benchmark")
        await browser_manager.start()
        await _measure_context_creation(browser_manager, recorder)
        sampling_task = asyncio.create_task(
            sample_resources(
                stop_event=sampling_stop,
                recorder=recorder,
                browser_manager=browser_manager,
                metrics=metrics,
                process_probe=process_probe,
                interval_seconds=sample_interval_seconds,
            ),
            name="phase2-resource-sampler",
        )

        phase_started = time.perf_counter()
        async with asyncio.timeout(creation_timeout_seconds):
            await controller.run()
        recorder.creation_phase_seconds = time.perf_counter() - phase_started

        if monitoring_seconds > 0:
            monitoring_stop = asyncio.Event()
            phase_started = time.perf_counter()
            scheduler_task = asyncio.create_task(
                scheduler.run(monitoring_stop),
                name="phase2-monitoring-benchmark",
            )
            try:
                await asyncio.sleep(monitoring_seconds)
            finally:
                monitoring_stop.set()
                await scheduler_task
                recorder.monitoring_phase_seconds = time.perf_counter() - phase_started

        await recorder.capture(
            browser_manager=browser_manager,
            metrics=metrics,
            process_probe=process_probe,
        )
        report = recorder.build_report(
            settings,
            metrics,
            test_duration_seconds=time.perf_counter() - run_started,
            sample_interval_seconds=sample_interval_seconds,
        )
        report.write_json(report_path)
        return report
    finally:
        sampling_stop.set()
        if sampling_task is not None:
            await sampling_task
        await browser_manager.shutdown()
        await repository.close()


async def _measure_context_creation(
    browser_manager: BrowserManager,
    recorder: ResourceBenchmarkRecorder,
) -> None:
    """Take a small fixed warm-up sample without retaining visitor contexts."""

    for _ in range(5):
        started = time.perf_counter()
        owned = await browser_manager.create_context()
        recorder.record_context_creation(time.perf_counter() - started)
        await owned.close()


def _validate_phase2_profile(settings: Settings) -> None:
    expected = {
        "TARGET_QUEUE_IDS": settings.target_queue_ids == 100,
        "SESSION_MODE": settings.session_mode is SessionMode.HYBRID,
        "CHROME_PROCESS_COUNT": settings.chrome_process_count in {1, 2},
        "MAX_ACTIVE_CONTEXTS": settings.max_active_contexts == 25,
        "DATABASE_URL": settings.database_url.startswith("sqlite:///"),
    }
    invalid = [name for name, matches in expected.items() if not matches]
    if invalid:
        raise ValueError("Phase 2 resource profile mismatch: " + ", ".join(invalid))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the opt-in Phase 2 resource and stability benchmark"
    )
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("phase2-resource-benchmark.json"))
    parser.add_argument("--monitoring-seconds", type=float, default=600)
    parser.add_argument("--sample-interval-seconds", type=float, default=5)
    parser.add_argument("--creation-timeout-seconds", type=float, default=3600)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable browser traffic")
    configure_structured_logging()
    report = asyncio.run(
        run_phase2_resource_benchmark(
            get_settings(),
            report_path=args.report,
            monitoring_seconds=args.monitoring_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            creation_timeout_seconds=args.creation_timeout_seconds,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
