from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.harness.phase3_monitoring import (
    _validate_configuration,
    run_synthetic_monitoring_benchmark,
)


async def test_one_thousand_session_sweeps_are_bounded_and_repeatable(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "monitoring.json"

    report = await run_synthetic_monitoring_benchmark(
        tmp_path / "monitoring.sqlite3",
        report_path=report_path,
        worker_count=10,
        queue_capacity=25,
        claim_batch_size=25,
        check_delay_seconds=0,
        repeated_sweeps=2,
        sample_interval_seconds=0.005,
    )

    assert report.configuration.persisted_sessions == 1000
    assert len(report.sweeps) == 2
    assert all(result.sessions_due == 1000 for result in report.sweeps)
    assert all(result.sessions_checked == 1000 for result in report.sweeps)
    assert all(result.successful_checks == 1000 for result in report.sweeps)
    assert all(result.failed_checks == 0 for result in report.sweeps)
    assert all(result.backlog_start == 1000 for result in report.sweeps)
    assert all(result.backlog_end == 0 for result in report.sweeps)
    assert all(result.maximum_queue_depth <= 25 for result in report.sweeps)
    assert all(result.maximum_active_workers <= 10 for result in report.sweeps)
    assert all(result.checks_per_second > 0 for result in report.sweeps)
    assert all(result.check_duration.count == 1000 for result in report.sweeps)

    assert report.staggered.sessions_checked == 1000
    assert report.staggered.failed_checks == 0
    assert report.staggered.backlog_end == 0
    assert report.staggered.distinct_next_check_times > 900
    assert report.staggered.maximum_one_second_bucket < 150
    assert report.staggered.maximum_queue_depth <= 25
    assert all(
        checkpoint.backlog_after_processing == 0
        for checkpoint in report.staggered.checkpoints
    )

    assert report.staging_status == "NOT RUN"
    assert report.active_context_peak == 0
    assert report.context_acquisition_wait_seconds is None
    assert report.restore_failures is None
    assert report.identity_mismatches is None
    assert report.browser_crashes is None
    assert report.navigation_failures is None

    payload: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["configuration"]["persisted_sessions"] == 1000
    assert payload["sweeps"][0]["sessions_checked"] == 1000
    assert payload["staggered"]["backlog_end"] == 0
    assert "synthetic-queue" not in report_path.read_text(encoding="utf-8")
    assert "Full sweep definition" in report.render_text()


@pytest.mark.parametrize(
    ("workers", "queue", "batch", "sweeps"),
    [(0, 25, 25, 2), (51, 50, 50, 2), (10, 51, 50, 2), (10, 25, 26, 2), (10, 25, 25, 1)],
)
def test_monitoring_benchmark_rejects_unbounded_or_invalid_configuration(
    workers: int,
    queue: int,
    batch: int,
    sweeps: int,
) -> None:
    with pytest.raises(ValueError):
        _validate_configuration(
            worker_count=workers,
            queue_capacity=queue,
            claim_batch_size=batch,
            lease_seconds=120,
            scheduler_tick_seconds=0.001,
            check_delay_seconds=0,
            repeated_sweeps=sweeps,
            sample_interval_seconds=0.01,
        )
