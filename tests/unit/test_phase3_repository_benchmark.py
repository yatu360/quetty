from datetime import UTC, datetime
from pathlib import Path

from queue_load_test.harness.phase3_repository import run_phase3_repository_benchmark


async def test_synthetic_repository_benchmark_measures_one_thousand_sessions(
    tmp_path: Path,
) -> None:
    report = await run_phase3_repository_benchmark(
        tmp_path / "benchmark.sqlite3",
        session_count=1000,
        claim_batch_size=50,
        samples=3,
        now=datetime(2026, 9, 26, 12, tzinfo=UTC),
    )

    assert report.session_count == 1000
    assert report.expected_due_sessions == report.observed_due_sessions == 600
    assert report.claim_batch_size == 50
    assert report.database_size_bytes > 0
    assert any("idx_queue_sessions_due" in detail for detail in report.query_plan)
    assert all("TEMP B-TREE" not in detail for detail in report.query_plan)
    assert report.due_query.samples == 3
    assert report.claim.samples == 3
    assert report.update.samples == 3
    assert report.lease_release.samples == 3
    assert report.scheduler_iteration.samples == 3
    assert report.claim.maximum_ms >= 0
