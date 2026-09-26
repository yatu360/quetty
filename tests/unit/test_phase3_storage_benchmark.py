import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from queue_load_test.harness.phase3_storage import run_phase3_storage_benchmark


async def test_storage_benchmark_measures_one_thousand_files_and_restart(
    tmp_path: Path,
) -> None:
    report = await run_phase3_storage_benchmark(
        tmp_path / "sessions.sqlite3",
        tmp_path / "state",
        session_count=1000,
        delete_samples=10,
        now=datetime(2026, 9, 26, tzinfo=UTC),
    )

    assert report.session_count == 1000
    assert report.restart_session_count == 1000
    assert report.state_file_count == 1000
    assert report.directory_entry_count == 1000
    assert report.state_directory_size_bytes > 0
    assert report.database_size_bytes > 0
    assert report.save.samples == 1000
    assert report.load.samples == 1000
    assert report.replace.samples == 1000
    assert report.delete.samples == 10
    assert report.concurrent_save.samples == report.concurrent_load.samples == 1000
    assert report.concurrent_save.concurrency == 20
    assert report.state_directory_allocated_bytes >= report.state_directory_size_bytes
    assert report.directory_traversal_ms >= 0
    assert report.baseline_consistency.legacy_state_files == 0
    assert report.save.p95_ms >= 0
    assert report.load.p95_ms >= 0
    assert report.baseline_consistency.consistent
    assert report.restart_consistency.consistent
    assert report.event_loop_probe_ticks > 0

    output = tmp_path / "report.json"
    report.write_json(output)
    serialized = json.loads(output.read_text(encoding="utf-8"))
    assert serialized["session_count"] == 1000
    assert serialized["baseline_consistency"]["findings_by_kind"] == {}
    assert "Sessions/state files: 1000/1000" in report.render_text()


async def test_storage_benchmark_requires_dedicated_empty_paths(tmp_path: Path) -> None:
    database = tmp_path / "existing.sqlite3"
    database.touch()

    with pytest.raises(RuntimeError, match="absent database"):
        await run_phase3_storage_benchmark(
            database,
            tmp_path / "state",
            session_count=1,
            delete_samples=1,
        )


@pytest.mark.parametrize(
    ("session_count", "delete_samples"),
    [(0, 1), (10, 0), (10, 11)],
)
async def test_storage_benchmark_rejects_invalid_counts(
    tmp_path: Path,
    session_count: int,
    delete_samples: int,
) -> None:
    with pytest.raises(ValueError):
        await run_phase3_storage_benchmark(
            tmp_path / "sessions.sqlite3",
            tmp_path / "state",
            session_count=session_count,
            delete_samples=delete_samples,
        )


async def test_storage_benchmark_rejects_non_positive_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await run_phase3_storage_benchmark(
            tmp_path / "sessions.sqlite3",
            tmp_path / "state",
            session_count=1,
            delete_samples=1,
            concurrency=0,
        )
