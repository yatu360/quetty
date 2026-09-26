import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from queue_load_test.harness.phase3_recovery import run_phase3_recovery_benchmark

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


async def test_recovery_benchmark_preserves_one_thousand_session_identities(
    tmp_path: Path,
) -> None:
    report = await run_phase3_recovery_benchmark(
        tmp_path / "sessions.sqlite3",
        tmp_path / "state",
        restart_repetitions=3,
        now=NOW,
    )

    assert report.session_count == 1000
    assert report.initial_summary.valid_queue_ids == 960
    assert report.initial_summary.leased_sessions == 50
    assert report.initial_summary.expired_leases == 50
    assert report.initial_summary.sessions_due == 800
    assert report.initial_summary.sessions_requiring_retry == 50
    assert report.initial_summary.terminal_sessions == 100
    assert report.initial_summary.missing_state_files == 0
    assert report.initial_summary.corrupt_state_files == 0
    assert report.initial_summary.state_scan_performed
    assert report.expired_leases_recovered == 50
    assert report.resumed_checks == 50
    assert report.final_summary.valid_queue_ids == 960
    assert report.final_summary.expired_leases == 0
    assert report.final_summary.sessions_due == 750
    assert report.identity_preserved
    assert report.terminal_states_preserved
    assert report.state_associations_consistent
    assert report.scheduler_worker_tasks == 5
    assert report.scheduler_queue_peak <= report.scheduler_queue_capacity == 50
    assert report.restart_duration_p95_ms >= 0

    output = tmp_path / "recovery.json"
    report.write_json(output)
    serialized = json.loads(output.read_text(encoding="utf-8"))
    assert serialized["session_count"] == 1000
    assert serialized["identity_preserved"] is True
    assert "Expired leases recovered / resumed checks: 50 / 50" in report.render_text()


async def test_recovery_benchmark_requires_repeated_restarts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        await run_phase3_recovery_benchmark(
            tmp_path / "sessions.sqlite3",
            tmp_path / "state",
            restart_repetitions=1,
            now=NOW,
        )


async def test_recovery_benchmark_requires_dedicated_paths(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    database.touch()
    with pytest.raises(RuntimeError, match="absent database"):
        await run_phase3_recovery_benchmark(
            database,
            tmp_path / "state",
            restart_repetitions=2,
            now=NOW,
        )
