from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.harness.phase4_acquisition import (
    PreflightStatus,
    _validate_phase4_profile,
    run_phase4_acquisition_benchmark,
    run_phase4_preflight,
)
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.repository import SessionRepository, SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore


def phase4_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "STAGING_URL": "https://queue.staging.test",
        "TARGET_QUEUE_IDS": 10_000,
        "SESSION_MODE": "HYBRID",
        "CHROME_PROCESS_COUNT": 2,
        "MAX_CONTEXTS_PER_BROWSER": 25,
        "MAX_ACTIVE_CONTEXTS": 50,
        "CREATION_WORKERS": 10,
        "CREATION_QUEUE_CAPACITY": 10,
        "MONITOR_WORKERS": 20,
        "MONITOR_QUEUE_CAPACITY": 50,
        "MONITOR_CLAIM_BATCH_SIZE": 50,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'acquisition.sqlite3'}",
        "STATE_DIRECTORY": tmp_path / "state",
    }
    values.update(overrides)
    return Settings(**values)


class _OwnedContext:
    def __init__(self, manager: _FakeBrowserManager) -> None:
        self._manager = manager

    async def close(self) -> None:
        self._manager.active_contexts -= 1


class _FakeBrowserManager:
    def report_navigation(self, context: object, *, responsive: bool) -> None:
        """Navigation health reports are irrelevant to this fake."""

    def __init__(self) -> None:
        self.started = False
        self.active_contexts = 0

    async def start(self) -> None:
        self.started = True

    async def create_context(self) -> _OwnedContext:
        self.active_contexts += 1
        return _OwnedContext(self)

    async def capacity(self) -> SimpleNamespace:
        return SimpleNamespace(connected_processes=2, active_contexts=self.active_contexts)

    async def shutdown(self) -> None:
        self.started = False


class _CountRepository:
    def __init__(self, count: int) -> None:
        self.count = count

    async def count_successful_queue_ids(self) -> int:
        return self.count


class _FastSuccessHandler:
    def __init__(self, repository: _CountRepository) -> None:
        self.repository = repository
        self.calls = 0

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        self.calls += 1
        self.repository.count += 1
        return CreationOutcome(
            kind=CreationOutcomeKind.SUCCESS,
            attempts=1,
            temporary_failures=0,
            duration_seconds=0.001,
        )


def test_phase4_profile_rejects_capacity_above_measured_candidate(tmp_path: Path) -> None:
    _validate_phase4_profile(phase4_settings(tmp_path))

    with pytest.raises(ValueError, match="MAX_ACTIVE_CONTEXTS"):
        _validate_phase4_profile(
            phase4_settings(
                tmp_path,
                CHROME_PROCESS_COUNT=3,
                MAX_ACTIVE_CONTEXTS=75,
            )
        )


async def test_preflight_checks_database_state_browser_metrics_and_disk(tmp_path: Path) -> None:
    settings = phase4_settings(tmp_path)
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = _FakeBrowserManager()
    metrics = PrometheusMetrics()
    try:
        report = await run_phase4_preflight(
            settings,
            repository=repository,
            state_store=state_store,
            browser_manager=cast(BrowserManager, browser_manager),
            metrics=metrics,
            minimum_free_disk_bytes=1,
        )
    finally:
        await repository.close()

    assert report.ready
    assert report.existing_successful_queue_ids == 0
    assert report.free_disk_bytes is not None and report.free_disk_bytes > 0
    assert all(check.status is PreflightStatus.PASS for check in report.checks)
    assert browser_manager.active_contexts == 0
    assert list(state_store.directory.glob("preflight-*.json")) == []

    path = tmp_path / "preflight.json"
    report.write_json(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["target_queue_ids"] == 10_000
    assert "queue.staging.test" in report.render_text()


async def test_preflight_rejects_placeholder_url_and_insufficient_disk(tmp_path: Path) -> None:
    settings = phase4_settings(tmp_path, STAGING_URL="https://staging.example.test")
    repository = SQLiteSessionRepository(settings.database_url)
    try:
        report = await run_phase4_preflight(
            settings,
            repository=repository,
            state_store=FileSystemStateStore(settings.state_directory),
            browser_manager=cast(BrowserManager, _FakeBrowserManager()),
            metrics=PrometheusMetrics(),
            minimum_free_disk_bytes=2**63,
        )
    finally:
        await repository.close()

    assert not report.ready
    failures = {check.name for check in report.checks if check.status is PreflightStatus.FAIL}
    assert failures == {"staging_url", "disk_space"}


async def test_phase4_run_requires_both_staging_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_STAGING_TESTS", raising=False)
    monkeypatch.delenv("RUN_PHASE4_ACQUISITION_BENCHMARK", raising=False)

    with pytest.raises(RuntimeError, match="RUN_STAGING_TESTS"):
        await run_phase4_acquisition_benchmark(
            phase4_settings(tmp_path),
            report_path=tmp_path / "report.json",
            preflight_report_path=tmp_path / "preflight.json",
            sample_interval_seconds=1,
            creation_timeout_seconds=1,
        )


@pytest.mark.parametrize(
    ("starting_count", "expected_calls"),
    [(0, 10_000), (7_423, 2_577), (10_000, 0)],
    ids=("fresh", "resume", "already-satisfied"),
)
async def test_bounded_controller_reaches_phase4_target_without_overshoot(
    starting_count: int,
    expected_calls: int,
) -> None:
    repository = _CountRepository(starting_count)
    handler = _FastSuccessHandler(repository)
    controller = SessionCreationController(
        repository=cast(SessionRepository, repository),
        handler=handler,
        target_queue_ids=10_000,
        worker_count=10,
        queue_capacity=10,
    )

    metrics = await controller.run()

    assert repository.count == 10_000
    assert handler.calls == expected_calls
    assert metrics.unique_ids_acquired == expected_calls
    assert sum(metrics.worker_completed.values()) == expected_calls
    assert sum(metrics.worker_successes.values()) == expected_calls
    assert sum(metrics.worker_failures.values()) == 0
