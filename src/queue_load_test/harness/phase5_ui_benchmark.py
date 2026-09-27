"""Phase 5 operator-UI sanity benchmark over a local synthetic 10,000-row database.

This measures only the dashboard's SQLite reads and HTML rendering. It creates no
browser, contacts no Queue-it environment, and says nothing about Queue-it
throughput. Every Queue ID is a synthetic local value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from queue_load_test.harness.restore_benchmark import percentile
from queue_load_test.models import (
    BrowserRuntimeState,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web.actions import OperatorAction, OperatorActionKind
from queue_load_test.web.manual import ManualOpenResult
from queue_load_test.web.service import DashboardService, RuntimeCapacity

PHASE5_POPULATION = 10_000
PAGE_SIZE = 50
_STATUSES = (
    QueueStatus.PRE_QUEUE,
    QueueStatus.ACTIVE_QUEUE,
    QueueStatus.PARKED,
    QueueStatus.SERVICED_SOON,
    QueueStatus.CONNECTION_LOST,
)
_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


@dataclass(frozen=True, slots=True)
class Latency:
    samples: int
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    statements_per_call: float
    writes_per_call: float


@dataclass(slots=True)
class StatementCounter:
    """Count SQL statements through sqlite3's trace hook (includes BEGIN/COMMIT)."""

    statements: int = 0
    writes: int = 0
    text: list[str] = field(default_factory=list)

    def __call__(self, statement: str) -> None:
        self.statements += 1
        normalized = statement.lstrip().upper()
        if normalized.startswith(_WRITE_PREFIXES):
            self.writes += 1
            self.text.append(statement.strip().split("\n", 1)[0][:80])


class ReadOnlyRuntime:
    """A RunRuntime that fails loudly if a dashboard read ever reaches browser work."""

    browser_calls = 0

    async def start_run(self, run: RunConfig) -> None:
        return None

    async def capacity(self) -> RuntimeCapacity:
        return RuntimeCapacity(maximum_active_contexts=50)

    def error(self) -> str | None:
        return None

    async def pause_monitoring(self) -> None:
        raise AssertionError("pause is measured through the repository")

    async def resume_monitoring(self) -> None:
        raise AssertionError("resume is measured through the repository")

    async def open_session(self, session_id: str) -> ManualOpenResult:
        self.browser_calls += 1
        raise AssertionError("dashboard reads must never open Chrome")

    async def close_session(self, session_id: str) -> bool:
        self.browser_calls += 1
        raise AssertionError("dashboard reads must never close Chrome")

    async def request_action(
        self,
        kind: OperatorActionKind,
        session_id: str | None = None,
        *,
        request_token: str | None = None,
    ) -> OperatorAction:
        self.browser_calls += 1
        raise AssertionError("dashboard reads must never queue operator work")

    def stop_accepting(self) -> None:
        return None

    def session_action(self, session_id: str) -> OperatorAction | None:
        return None

    def latest_add_action(self) -> OperatorAction | None:
        return None

    async def close(self) -> None:
        return None


def _synthetic_rows(now: datetime) -> list[tuple[QueueSession, QueueProgress | None]]:
    rows: list[tuple[QueueSession, QueueProgress | None]] = []
    for index in range(PHASE5_POPULATION):
        session_id = f"ui-session-{index:05d}"
        status = _STATUSES[index % len(_STATUSES)]
        leased = index % 97 == 0
        manual = index % 2_000 == 1
        session = QueueSession(
            session_id=session_id,
            queue_id=f"synthetic-queue-{index:05d}",
            transfer_url="https://synthetic.invalid/transfer",
            mode=SessionMode.HYBRID if index % 2 else SessionMode.TRANSFER_ONLY,
            status=status,
            state_path=Path(f"state/{session_id}.json"),
            created_at=now - timedelta(seconds=PHASE5_POPULATION - index),
            next_check_at=now + timedelta(seconds=(index % 600) - 300),
            worker_id="synthetic-monitor" if leased else None,
            lease_until=now + timedelta(seconds=60) if leased else None,
            manual_owner_id="synthetic-manual" if manual else None,
            manual_lease_until=now + timedelta(seconds=30) if manual else None,
        )
        progress = (
            QueueProgress(session_id=session_id, progress_percentage=float(index % 100))
            if index % 3
            else None
        )
        rows.append((session, progress))
    return rows


async def _measure(
    counter: StatementCounter,
    operation: Callable[[], Awaitable[object]],
    *,
    iterations: int,
) -> Latency:
    await operation()  # warm the page cache and statement cache
    counter.statements = counter.writes = 0
    durations: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        await operation()
        durations.append((time.perf_counter() - started) * 1_000)
    return Latency(
        samples=iterations,
        p50_ms=_round(percentile(durations, 50)),
        p95_ms=_round(percentile(durations, 95)),
        max_ms=_round(max(durations)),
        statements_per_call=round(counter.statements / iterations, 2),
        writes_per_call=round(counter.writes / iterations, 2),
    )


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


async def run_benchmark(database: Path, *, iterations: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    seed = SQLiteSessionRepository(database)
    await seed.initialize()
    await seed.create_run(
        RunConfig(
            run_id="phase5-ui-benchmark",
            target_url="https://synthetic.invalid/queue",
            requested_sessions=PHASE5_POPULATION,
            created_at=now,
        )
    )
    await seed.create_many(_synthetic_rows(now))
    await seed.close()

    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    connection = repository._connect()  # benchmark-only instrumentation hook
    counter = StatementCounter()
    connection.set_trace_callback(counter)
    runtime = ReadOnlyRuntime()
    dashboard = DashboardService(repository, runtime)
    run = await repository.get_active_run()
    assert run is not None
    last_page = (PHASE5_POPULATION + PAGE_SIZE - 1) // PAGE_SIZE

    def page(**kwargs: Any) -> Callable[[], Awaitable[object]]:
        return lambda: repository.list_session_summaries(page_size=PAGE_SIZE, **kwargs)

    measurements = {
        "dashboard_first_page": await _measure(counter, page(page=1), iterations=iterations),
        "pagination_middle_page": await _measure(
            counter, page(page=last_page // 2), iterations=iterations
        ),
        "pagination_last_page": await _measure(
            counter, page(page=last_page), iterations=iterations
        ),
        "search_exact_queue_id": await _measure(
            counter, page(page=1, search="synthetic-queue-07777"), iterations=iterations
        ),
        "search_substring": await _measure(
            counter, page(page=1, search="-0777"), iterations=iterations
        ),
        "search_miss": await _measure(
            counter, page(page=1, search="does-not-exist"), iterations=iterations
        ),
        "status_filter": await _measure(
            counter, page(page=1, status=QueueStatus.ACTIVE_QUEUE), iterations=iterations
        ),
        "runtime_filter_parked": await _measure(
            counter,
            page(page=1, runtime_state=BrowserRuntimeState.PARKED),
            iterations=iterations,
        ),
        "runtime_filter_open_in_chrome": await _measure(
            counter,
            page(page=1, runtime_state=BrowserRuntimeState.OPEN_IN_CHROME),
            iterations=iterations,
        ),
        "summary_aggregate": await _measure(
            counter, lambda: dashboard.summary(run), iterations=iterations
        ),
    }
    http = await _measure_http(repository, runtime, counter, iterations=iterations)

    plan = [
        str(row[3])
        for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT s.session_id FROM queue_sessions AS s "
            "LEFT JOIN queue_progress AS p ON p.session_id = s.session_id "
            "ORDER BY s.created_at, s.session_id LIMIT 50 OFFSET 9950"
        ).fetchall()
    ]

    # Polling: one summary + one sessions poll per tab every 1-2 seconds.
    poll_ms = (measurements["summary_aggregate"].p95_ms or 0) + (
        measurements["dashboard_first_page"].p95_ms or 0
    )
    tasks_before = len(asyncio.all_tasks())
    changes_before = connection.total_changes
    polls = 0
    for _ in range(iterations):
        await asyncio.gather(dashboard.summary(run), page(page=1)())
        polls += 1
    polling = {
        "polls": polls,
        "p95_ms_per_poll_pair": round(poll_ms, 3),
        "duty_cycle_one_tab_1s_percent": round(poll_ms / 10, 3),
        "duty_cycle_one_tab_2s_percent": round(poll_ms / 20, 3),
        "rows_written_during_polling": connection.total_changes - changes_before,
        "asyncio_tasks_before": tasks_before,
        "asyncio_tasks_after": len(asyncio.all_tasks()),
    }

    changes_before = connection.total_changes
    await repository.set_monitoring_paused(True)
    pause_rows = connection.total_changes - changes_before
    changes_before = connection.total_changes
    await repository.set_monitoring_paused(False)
    resume_rows = connection.total_changes - changes_before

    connection.set_trace_callback(None)
    total = await repository.recovery_summary(now=datetime.now(UTC))
    await repository.close()
    return {
        "population": total.total_persisted_sessions,
        "page_size": PAGE_SIZE,
        "iterations": iterations,
        "repository": {name: asdict(value) for name, value in measurements.items()},
        "http": http,
        "deep_page_query_plan": plan,
        "polling": polling,
        "pause_rows_written": pause_rows,
        "resume_rows_written": resume_rows,
        "browser_calls": runtime.browser_calls,
        "note": (
            "Local synthetic SQLite/dashboard measurement only; not Queue-it throughput."
        ),
    }


async def _measure_http(
    repository: SQLiteSessionRepository,
    runtime: ReadOnlyRuntime,
    counter: StatementCounter,
    *,
    iterations: int,
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the test extra
        return {"skipped": "httpx is not installed"}
    from queue_load_test.config import Settings
    from queue_load_test.web.app import create_app

    settings = Settings(_env_file=None, DATABASE_URL="sqlite:///:memory:")  # type: ignore[call-arg]
    app = create_app(settings=settings, repository=repository, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(transport=transport, base_url="http://ui.local") as client:
        for name, path in (
            ("sessions_partial_first_page", "/partials/sessions?page=1"),
            ("sessions_partial_search", "/partials/sessions?search=-0777"),
            ("sessions_partial_status", "/partials/sessions?status=ACTIVE_QUEUE"),
            ("sessions_partial_last_page", "/partials/sessions?page=200"),
            ("summary_partial", "/partials/summary"),
        ):

            async def request(path: str = path) -> object:
                response = await client.get(path, headers={"HX-Request": "true"})
                if response.status_code != 200:
                    raise AssertionError(f"{path} returned {response.status_code}")
                return response

            results[name] = asdict(await _measure(counter, request, iterations=iterations))
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="phase5-ui-") as directory:
        result = asyncio.run(
            run_benchmark(Path(directory) / "ui.sqlite3", iterations=args.iterations)
        )
    result["measured_at"] = datetime.now(UTC).isoformat()
    result["sqlite_version"] = sqlite3.sqlite_version
    text = json.dumps(result, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
