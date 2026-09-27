from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from queue_load_test.harness.phase4_monitoring import (
    PHASE4_POPULATION,
    _seed_sessions,
    adaptive_intervals,
    run_phase4_monitoring_benchmark,
)
from queue_load_test.harness.resource_benchmark import ProcessResourceSnapshot
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class FixedProbe:
    def sample(self) -> ProcessResourceSnapshot:
        return ProcessResourceSnapshot(
            application_cpu_percent=12.5,
            application_ram_bytes=128_000_000,
            chrome_cpu_percent=0.0,
            chrome_ram_bytes=0,
            observed_chrome_processes=0,
            open_file_descriptors=10,
        )


async def test_ten_thousand_due_rows_are_bounded_and_leases_are_disjoint(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "ten-thousand.sqlite3")
    await _seed_sessions(repository, PHASE4_POPULATION)

    summary = await repository.due_session_summary(now=NOW)
    first = await repository.claim_due_sessions(
        worker_id="worker-a",
        now=NOW,
        lease_until=NOW + timedelta(minutes=2),
        limit=50,
    )
    second = await repository.claim_due_sessions(
        worker_id="worker-b",
        now=NOW,
        lease_until=NOW + timedelta(minutes=2),
        limit=50,
    )

    assert summary.count == PHASE4_POPULATION
    assert summary.oldest_overdue_seconds(now=NOW) == 120
    assert len(first) == len(second) == 50
    assert {item.session_id for item in first}.isdisjoint(
        item.session_id for item in second
    )
    assert await repository.count_due_sessions(now=NOW) == PHASE4_POPULATION - 100
    await repository.close()


async def test_due_summary_excludes_future_leased_and_terminal_sessions(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "eligibility.sqlite3")
    for session_id, status, next_check in (
        ("due", QueueStatus.PARKED, NOW - timedelta(seconds=7)),
        ("future", QueueStatus.PARKED, NOW + timedelta(seconds=1)),
        ("admitted", QueueStatus.ADMITTED, NOW - timedelta(minutes=2)),
        ("expired", QueueStatus.EXPIRED, NOW - timedelta(minutes=2)),
        ("failed", QueueStatus.FAILED, NOW - timedelta(minutes=2)),
    ):
        await repository.create(
            QueueSession(
                session_id=session_id,
                queue_id=f"queue-{session_id}",
                transfer_url="https://synthetic.invalid/transfer",
                mode=SessionMode.TRANSFER_ONLY,
                status=status,
                state_path=Path(f"{session_id}.json"),
                created_at=NOW - timedelta(minutes=3),
                next_check_at=next_check,
            )
        )
    leased = QueueSession(
        session_id="leased",
        queue_id="queue-leased",
        transfer_url="https://synthetic.invalid/transfer",
        mode=SessionMode.TRANSFER_ONLY,
        status=QueueStatus.PARKED,
        state_path=Path("leased.json"),
        created_at=NOW - timedelta(minutes=3),
        next_check_at=NOW - timedelta(minutes=1),
        worker_id="other",
        lease_until=NOW + timedelta(minutes=1),
    )
    await repository.create(leased)

    summary = await repository.due_session_summary(now=NOW)

    assert summary.count == 1
    assert summary.oldest_due_at == NOW - timedelta(seconds=7)
    assert summary.oldest_overdue_seconds(now=NOW) == 7
    await repository.close()


async def test_repeated_sweeps_and_adaptive_scheduling_are_separate_and_bounded(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    report = await run_phase4_monitoring_benchmark(
        tmp_path / "benchmark.sqlite3",
        report_path=report_path,
        population=100,
        worker_count=5,
        queue_capacity=10,
        claim_batch_size=10,
        check_delay_seconds=0,
        sample_interval_seconds=0.001,
        process_probe=FixedProbe(),
    )

    assert len(report.scenario_a) == 2
    assert all(item.sessions_checked == 100 for item in report.scenario_a)
    assert all(item.backlog_end == 0 for item in report.scenario_a)
    assert all(item.maximum_queue_depth <= 10 for item in report.scenario_a)
    assert all(item.maximum_active_workers <= 5 for item in report.scenario_a)
    assert report.scenario_b.sessions_checked == 100
    assert report.scenario_b.backlog_end == 0
    assert report.scenario_b.maximum_queue_depth <= 10
    assert report.scenario_b.maximum_active_workers <= 5
    assert report.staging_status == "NOT RUN"
    assert report.active_context_peak == 0
    assert report.restore_attempts is None
    assert json.loads(report_path.read_text(encoding="utf-8"))["scenario_b"][
        "sessions_checked"
    ] == 100


async def test_adaptive_jitter_spreads_ten_thousand_due_times() -> None:
    intervals = adaptive_intervals(PHASE4_POPULATION)
    one_second_buckets: dict[int, int] = {}
    for interval in intervals:
        key = int(interval)
        one_second_buckets[key] = one_second_buckets.get(key, 0) + 1

    assert len(intervals) == PHASE4_POPULATION
    assert len(set(intervals)) > 9_000
    assert max(one_second_buckets.values()) < 1_000
    assert min(intervals) == 0
    assert max(intervals) <= 300
