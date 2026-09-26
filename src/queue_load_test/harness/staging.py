"""Explicitly gated 10-session staging acceptance run."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserManager
from queue_load_test.browser.manager import ContextStorageState
from queue_load_test.config import Settings, get_settings
from queue_load_test.harness.report import Phase1AcceptanceRecorder, Phase1AcceptanceReport
from queue_load_test.metrics import PrometheusMetrics, configure_structured_logging
from queue_load_test.models import QueueProgress, QueueSession, SessionMode
from queue_load_test.queue_monitor import AdmissionDetector, QueueItLiveStateExtractor
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationWorkItem,
    QueueSessionCreator,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueItTransferExtractor, QueueSessionRestorer, RestoreMethod


@dataclass(slots=True)
class _ResourceSample:
    cpu_started: float
    ram_started: int | None


class _RecordingCreator:
    def __init__(
        self,
        creator: QueueSessionCreator,
        recorder: Phase1AcceptanceRecorder,
    ) -> None:
        self._creator = creator
        self._recorder = recorder

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        outcome = await self._creator.create(work_item)
        self._recorder.measurements.session_creation_seconds.append(outcome.duration_seconds)
        queue_id = outcome.session.queue_id if outcome.session is not None else None
        self._recorder.record_fresh_identity(queue_id)
        self._recorder.record_transfer_extraction(
            success=outcome.session is not None and bool(outcome.session.transfer_url)
        )
        if outcome.progress is not None:
            _record_progress(self._recorder, outcome.progress)
            if outcome.progress.pre_queue is True and queue_id is not None:
                self._recorder.pre_queue_with_identity += 1
        return outcome


async def run_staging_acceptance(
    settings: Settings,
    *,
    report_path: Path,
    observe_seconds: float = 0,
    creation_timeout_seconds: float = 600,
) -> Phase1AcceptanceReport:
    """Run the exact Phase 1 profile after an explicit external safety gate."""

    _validate_phase1_profile(settings)
    if creation_timeout_seconds <= 0:
        raise ValueError("creation_timeout_seconds must be positive")
    if os.environ.get("RUN_STAGING_TESTS") != "1":
        raise RuntimeError("Set RUN_STAGING_TESTS=1 to run against authorised staging")

    recorder = Phase1AcceptanceRecorder(target_queue_ids=10)
    resource_start = _resource_start()
    metrics = PrometheusMetrics()
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = BrowserManager.from_settings(settings, observability=metrics)
    extractor = QueueItLiveStateExtractor()
    creator = QueueSessionCreator(
        browser_manager=browser_manager,
        repository=repository,
        state_store=state_store,
        staging_url=str(settings.staging_url),
        state_directory=settings.state_directory,
        mode=settings.session_mode,
        live_extractor=extractor,
        observability=metrics,
    )
    recording_creator = _RecordingCreator(creator, recorder)
    controller = SessionCreationController(
        repository=repository,
        handler=recording_creator,
        target_queue_ids=10,
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
    previous_progress: dict[str, QueueProgress] = {}
    pre_queue_identities: set[str] = set()

    try:
        if await repository.count_successful_queue_ids() != 0:
            raise RuntimeError("Use a dedicated empty SQLite database for an acceptance run")
        await browser_manager.start()
        await _measure_context_creation(browser_manager, recorder)
        try:
            async with asyncio.timeout(creation_timeout_seconds):
                await controller.run()
        except TimeoutError:
            # Continue to produce a FAIL/UNKNOWN report from whatever evidence exists.
            pass
        sessions = [item for item in await repository.list() if item.queue_id is not None]
        for session in sessions:
            progress = await repository.get_progress(session.session_id)
            if progress is not None:
                previous_progress[session.session_id] = progress
                if progress.pre_queue is True and session.queue_id is not None:
                    pre_queue_identities.add(session.queue_id)

        await _probe_storage_restores(
            settings,
            browser_manager,
            state_store,
            sessions,
            recorder,
        )

        deadline = asyncio.get_running_loop().time() + max(0, observe_seconds)
        first_pass = True
        while first_pass or asyncio.get_running_loop().time() < deadline:
            first_pass = False
            for session in sessions:
                started = time.perf_counter()
                result = await restorer.restore(session)
                duration = time.perf_counter() - started
                recorder.measurements.restore_seconds.append(duration)
                recorder.measurements.monitoring_seconds.append(duration)
                transfer_attempt = next(
                    (
                        attempt
                        for attempt in result.attempts
                        if attempt.method is RestoreMethod.TRANSFER
                    ),
                    None,
                )
                if transfer_attempt is not None:
                    recorder.record_transfer_restore(
                        success=transfer_attempt.success,
                        identity_match=transfer_attempt.identity_match,
                    )
                    if (
                        transfer_attempt.identity_match is False
                        and session.queue_id in pre_queue_identities
                    ):
                        recorder.pre_queue_to_active_mismatch += 1
                if result.progress is not None:
                    _record_progress(recorder, result.progress)
                    prior = previous_progress.get(session.session_id)
                    if (
                        prior is not None
                        and prior.last_updated_at is not None
                        and result.progress.last_updated_at is not None
                        and prior.last_updated_at != result.progress.last_updated_at
                    ):
                        recorder.last_updated_changes += 1
                    previous_progress[session.session_id] = result.progress
                    if (
                        result.progress.active_queue is True
                        and session.queue_id in pre_queue_identities
                    ):
                        recorder.pre_queue_to_active_same_identity += 1
                recorder.admitted_observed |= result.admitted
            if asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(settings.queue_poll_seconds)

        expected_count = await repository.count_successful_queue_ids()
        await repository.close()
        restarted_repository = SQLiteSessionRepository(settings.database_url)
        try:
            recorder.restart_preserved = (
                await restarted_repository.count_successful_queue_ids() == expected_count == 10
            )
        finally:
            await restarted_repository.close()
    finally:
        await browser_manager.shutdown()
        await repository.close()

    _resource_finish(recorder, resource_start)
    recorder.measurements.browser_crashes = int(
        metrics.registry.get_sample_value("browser_crashes_total") or 0
    )
    recorder.measurements.navigation_failures += int(
        metrics.registry.get_sample_value("navigation_timeouts_total") or 0
    )
    report = recorder.build_report()
    report.write_json(report_path)
    return report


async def _measure_context_creation(
    browser_manager: BrowserManager,
    recorder: Phase1AcceptanceRecorder,
) -> None:
    for _ in range(5):
        started = time.perf_counter()
        owned = await browser_manager.create_context()
        recorder.measurements.context_creation_seconds.append(time.perf_counter() - started)
        await owned.close()


async def _probe_storage_restores(
    settings: Settings,
    browser_manager: BrowserManager,
    state_store: FileSystemStateStore,
    sessions: list[QueueSession],
    recorder: Phase1AcceptanceRecorder,
) -> None:
    admission_detector = AdmissionDetector.from_urls(str(settings.staging_url))
    extractor = QueueItLiveStateExtractor()
    for session in sessions:
        state = await state_store.load(session.session_id)
        recorder.measurements.storage_state_restore_attempts += 1
        if state is None or session.queue_id is None:
            recorder.measurements.storage_state_restore_failures += 1
            continue
        try:
            started = time.perf_counter()
            async with browser_manager.context(
                storage_state=cast(ContextStorageState, state)
            ) as context:
                page = await context.new_page()
                navigation_started = time.perf_counter()
                await page.goto(
                    str(settings.staging_url),
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                recorder.measurements.navigation_seconds.append(
                    time.perf_counter() - navigation_started
                )
                progress = await extractor.extract(page, session_id=session.session_id)
                _record_progress(recorder, progress)
                transfer = await QueueItTransferExtractor(page.url).extract(
                    page,
                    expected_queue_id=session.queue_id,
                )
                identity_match = transfer.identity_mismatch is False and (
                    transfer.observed_queue_id == session.queue_id
                )
                if not await admission_detector.detect(page):
                    # Admission alone cannot independently prove which Queue ID was restored.
                    recorder.measurements.storage_state_restore_successes += int(identity_match)
                    recorder.measurements.storage_state_restore_failures += int(not identity_match)
                    recorder.measurements.identity_mismatches += int(transfer.identity_mismatch)
            recorder.measurements.restore_seconds.append(time.perf_counter() - started)
        except Exception:  # noqa: BLE001 - record the probe and continue the controlled run
            recorder.measurements.storage_state_restore_failures += 1
            recorder.measurements.navigation_failures += 1


def _record_progress(recorder: Phase1AcceptanceRecorder, progress: QueueProgress) -> None:
    recorder.progress_observations += int(progress.progress_percentage is not None)
    recorder.serviced_soon_observed |= progress.serviced_soon is True
    recorder.turn_started_observed |= progress.turn_started is True


def _validate_phase1_profile(settings: Settings) -> None:
    expected = {
        "TARGET_QUEUE_IDS": settings.target_queue_ids == 10,
        "SESSION_MODE": settings.session_mode is SessionMode.HYBRID,
        "CHROME_PROCESS_COUNT": settings.chrome_process_count == 1,
        "MAX_CONTEXTS_PER_BROWSER": settings.max_contexts_per_browser == 5,
        "MAX_ACTIVE_CONTEXTS": settings.max_active_contexts == 5,
        "QUEUE_POLL_SECONDS": settings.queue_poll_seconds == 30,
        "DATABASE_URL": settings.database_url.startswith("sqlite:///"),
    }
    invalid = [name for name, matches in expected.items() if not matches]
    if invalid:
        raise ValueError("Phase 1 controlled profile mismatch: " + ", ".join(invalid))


def _resource_start() -> _ResourceSample:
    return _ResourceSample(cpu_started=time.process_time(), ram_started=_process_ram_bytes())


def _resource_finish(
    recorder: Phase1AcceptanceRecorder,
    started: _ResourceSample,
) -> None:
    recorder.measurements.controller_cpu_seconds = time.process_time() - started.cpu_started
    finished_ram = _process_ram_bytes()
    observed_ram = [value for value in (started.ram_started, finished_ram) if value is not None]
    recorder.measurements.controller_ram_observation_bytes = max(observed_ram, default=None)


def _process_ram_bytes() -> int | None:
    try:
        psutil: Any = importlib.import_module("psutil")
        return int(psutil.Process().memory_info().rss)
    except (ImportError, AttributeError, OSError):
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the opt-in Phase 1 staging acceptance")
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("phase1-acceptance.json"))
    parser.add_argument("--observe-seconds", type=float, default=0)
    parser.add_argument("--creation-timeout-seconds", type=float, default=600)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable browser traffic")
    configure_structured_logging()
    report = asyncio.run(
        run_staging_acceptance(
            get_settings(),
            report_path=args.report,
            observe_seconds=args.observe_seconds,
            creation_timeout_seconds=args.creation_timeout_seconds,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
