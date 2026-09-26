from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.config import Settings
from queue_load_test.harness.phase3_acquisition import (
    AcquisitionResourceSample,
    _build_report,
    _validate_phase3_profile,
    run_phase3_acquisition_benchmark,
)
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.scheduler import CreationMetrics


def phase3_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "STAGING_URL": "https://staging.example.test",
        "TARGET_QUEUE_IDS": 1000,
        "SESSION_MODE": "HYBRID",
        "CHROME_PROCESS_COUNT": 2,
        "MAX_CONTEXTS_PER_BROWSER": 25,
        "MAX_ACTIVE_CONTEXTS": 50,
        "CREATION_WORKERS": 20,
        "CREATION_QUEUE_CAPACITY": 20,
        "MONITOR_WORKERS": 1,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'acquisition.sqlite3'}",
        "STATE_DIRECTORY": tmp_path / "state",
    }
    values.update(overrides)
    return Settings(**values)


def test_phase3_acquisition_profile_uses_conservative_measured_candidate(
    tmp_path: Path,
) -> None:
    _validate_phase3_profile(phase3_settings(tmp_path))

    with pytest.raises(ValueError, match="MAX_ACTIVE_CONTEXTS"):
        _validate_phase3_profile(
            phase3_settings(
                tmp_path,
                CHROME_PROCESS_COUNT=3,
                MAX_ACTIVE_CONTEXTS=75,
            )
        )


async def test_phase3_acquisition_requires_both_staging_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_STAGING_TESTS", raising=False)
    monkeypatch.delenv("RUN_PHASE3_ACQUISITION_BENCHMARK", raising=False)

    with pytest.raises(RuntimeError, match="RUN_STAGING_TESTS"):
        await run_phase3_acquisition_benchmark(
            phase3_settings(tmp_path),
            report_path=tmp_path / "report.json",
            sample_interval_seconds=1,
            creation_timeout_seconds=1,
        )


def test_acquisition_report_serializes_counts_latencies_and_resources(
    tmp_path: Path,
) -> None:
    metrics = PrometheusMetrics()
    metrics.set_browser_capacity(active_contexts=20, processes=2)
    metrics.browser_crashes_total.inc(1)
    metrics.navigation_failures_total.inc(2)
    controller_metrics = CreationMetrics(
        attempts=1005,
        completed_work_items=1003,
        initial_successful_unique_ids=0,
        successful_unique_ids=1000,
        unique_ids_acquired=1000,
        duplicates=1,
        temporary_failures=3,
        temporary_failure_outcomes=1,
        permanent_failures=1,
        retries=2,
        maximum_concurrent_creating=20,
        maximum_queue_depth=20,
        total_creation_duration_seconds=125.0,
        elapsed_seconds=10.0,
    )
    now = datetime.now(UTC)
    samples = (
        AcquisitionResourceSample(now, 10.0, 100, 50.0, 1_000, 8, 2, 10, 5.0),
        AcquisitionResourceSample(now, 20.0, 200, 70.0, 2_000, 9, 2, 20, 10.0),
    )

    report = _build_report(
        settings=phase3_settings(tmp_path),
        controller_metrics=controller_metrics,
        latencies=(0.1, 0.2, 0.3),
        samples=samples,
        metrics=metrics,
        initial_count=0,
        final_count=1000,
        elapsed_seconds=10,
        timed_out=False,
    )
    report_path = tmp_path / "report.json"
    report.write_json(report_path)
    payload: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))

    assert report.target_reached
    assert report.failed_attempts == 5
    assert report.sessions_per_second == 100
    assert report.creation_latency.p50_seconds == 0.2
    assert report.creation_latency.p95_seconds == pytest.approx(0.29)
    assert report.peak_active_contexts == 20
    assert report.resources.chrome_ram_bytes.average == 1500
    assert payload["configuration"]["target_queue_ids"] == 1000
    assert payload["duplicates"] == 1
    assert payload["navigation_failures"] == 2
    assert "Queue ID acquisition benchmark" in report.render_text()
    assert "same-queue" not in report_path.read_text(encoding="utf-8")


def test_partial_resume_report_keeps_initial_and_final_counts(tmp_path: Path) -> None:
    report = _build_report(
        settings=phase3_settings(tmp_path),
        controller_metrics=CreationMetrics(
            initial_successful_unique_ids=613,
            successful_unique_ids=700,
            unique_ids_acquired=87,
        ),
        latencies=(),
        samples=(),
        metrics=PrometheusMetrics(),
        initial_count=613,
        final_count=700,
        elapsed_seconds=5,
        timed_out=True,
    )

    assert report.status == "TARGET_NOT_REACHED"
    assert report.staging_status == "PARTIAL"
    assert report.initial_successful_unique_ids == 613
    assert report.final_successful_unique_ids == 700
    assert report.unique_queue_ids_acquired == 87
    assert report.timed_out
