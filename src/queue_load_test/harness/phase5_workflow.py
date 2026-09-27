"""Phase 5 acceptance: the complete operator workflow against a local simulator.

This runs the real operator stack end to end: the FastAPI app and lifespan, the
persisted RunConfig, bounded creation, the scheduler/monitor/lifecycle evaluator,
operator actions, the headed-Chrome manager, SQLite, local state files, and
installed Google Chrome. Requests are the same HTTP calls the HTMX dashboard makes.

The only substitute is the target: ``LocalQueueSimulator`` on 127.0.0.1 serves
Queue-it-*like* pages with synthetic identities. This is **not** Queue-it and no
staging environment is contacted, so nothing here is evidence of Queue-it behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from queue_load_test.config import Settings
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.utils.instance_lock import InstanceLock
from queue_load_test.web.app import create_app
from queue_load_test.web.service import ApplicationRunRuntime

REQUESTED = 4


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Recorder:
    checks: list[Check] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)

    def check(self, name: str, passed: bool, **detail: Any) -> bool:
        detail["t_seconds"] = round(time.perf_counter() - self.started, 2)
        self.checks.append(Check(name, bool(passed), detail))
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {name} {json.dumps(detail, default=str)}", flush=True)
        return bool(passed)


def _chrome_processes() -> int:
    try:
        psutil: Any = importlib.import_module("psutil")
    except ImportError:  # pragma: no cover - benchmark extra
        return -1
    count = 0
    for process in psutil.Process(os.getpid()).children(recursive=True):
        try:
            arguments = process.cmdline()
        except psutil.Error:
            continue
        if "--remote-debugging-pipe" in arguments and not any(
            argument.startswith("--type=") for argument in arguments
        ):
            count += 1
    return count


class Database:
    """Read-only evidence queries through an independent SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def rows(self, sql: str, parameters: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.path) as connection:
            return list(connection.execute(sql, parameters))

    def value(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        return self.rows(sql, parameters)[0][0]

    def identities(self) -> dict[str, str | None]:
        return dict(self.rows("SELECT session_id, queue_id FROM queue_sessions"))

    def valid(self) -> int:
        return int(
            self.value(
                "SELECT COUNT(DISTINCT queue_id) FROM queue_sessions "
                "WHERE queue_id IS NOT NULL AND status != 'FAILED'"
            )
        )

    def owners(self) -> int:
        return int(
            self.value(
                "SELECT COUNT(*) FROM queue_sessions "
                "WHERE worker_id IS NOT NULL OR manual_owner_id IS NOT NULL"
            )
        )

    def checked_at(self, session_id: str) -> str | None:
        rows = self.rows(
            "SELECT last_checked_at FROM queue_sessions WHERE session_id = ?", (session_id,)
        )
        return rows[0][0] if rows else None

    def session(self, session_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM queue_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def progress(self, session_id: str) -> float | None:
        rows = self.rows(
            "SELECT progress_percentage FROM queue_progress WHERE session_id = ?", (session_id,)
        )
        return None if not rows or rows[0][0] is None else float(rows[0][0])


async def until(
    predicate: Callable[[], Awaitable[bool] | bool],
    *,
    timeout: float,
    interval: float = 0.1,
) -> float | None:
    """Return elapsed seconds when ``predicate`` holds, or None on timeout."""

    started = time.perf_counter()
    while True:
        outcome = predicate()
        if isinstance(outcome, Awaitable):
            outcome = await outcome
        if outcome:
            return round(time.perf_counter() - started, 2)
        if time.perf_counter() - started > timeout:
            return None
        await asyncio.sleep(interval)


def workflow_settings(directory: Path, *, database: Path) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": f"sqlite:///{database}",
        "STATE_DIRECTORY": directory / "state",
        "SESSION_MODE": "HYBRID",
        "CHROME_PROCESS_COUNT": 1,
        "MAX_CONTEXTS_PER_BROWSER": 5,
        "MAX_ACTIVE_CONTEXTS": 5,
        "MAX_MANUAL_OPEN_SESSIONS": 2,
        "MANUAL_OPEN_LEASE_SECONDS": 6,
        "OPERATOR_WORKERS": 2,
        "CREATION_WORKERS": 2,
        "MONITOR_WORKERS": 2,
        "MONITOR_SCHEDULER_TICK_SECONDS": 0.2,
        "MONITOR_RETRY_MAX_ATTEMPTS": 2,
        "ADMISSION_WAIT_SECONDS": 0,
        "SHUTDOWN_TIMEOUT_SECONDS": 10,
        "QUEUE_POLL_SECONDS": 2,
        "POLL_JITTER_SECONDS": 0,
        "PRE_QUEUE_POLL_MIN_SECONDS": 1.5,
        "PRE_QUEUE_POLL_MAX_SECONDS": 2,
        "ACTIVE_EARLY_POLL_MIN_SECONDS": 1.5,
        "ACTIVE_EARLY_POLL_MAX_SECONDS": 2,
        "ACTIVE_MID_POLL_MIN_SECONDS": 1.5,
        "ACTIVE_MID_POLL_MAX_SECONDS": 2,
        "SERVICED_SOON_POLL_MIN_SECONDS": 1.5,
        "SERVICED_SOON_POLL_MAX_SECONDS": 2,
        "MAX_MANUAL_REQUESTED_SESSIONS": 50,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type, call-arg]


def _session_ids(html: str) -> list[str]:
    return re.findall(r'<td class="mono">([0-9a-f-]{36})</td>', html)


def _dd(html: str, label: str) -> str | None:
    match = re.search(rf"<dt>{re.escape(label)}</dt><dd>([^<]*)</dd>", html)
    return match.group(1) if match else None


async def run_workflow(directory: Path, *, headed: bool) -> dict[str, Any]:
    record = Recorder()
    simulator = LocalQueueSimulator(new_identity_prefix="sim-accept")
    await simulator.start()
    database_path = directory / "operator.sqlite3"
    db = Database(database_path)
    settings = workflow_settings(directory, database=database_path)
    target = simulator.entry_url
    evidence: dict[str, Any] = {"headed_manual_chrome": headed, "requested": REQUESTED}

    def build() -> tuple[Any, ApplicationRunRuntime]:
        repository = SQLiteSessionRepository(database_path)
        runtime = ApplicationRunRuntime(
            settings=settings, repository=repository, manual_headless=not headed
        )
        app = create_app(
            settings=settings,
            repository=repository,
            runtime=runtime,
            instance_lock=InstanceLock.for_database(database_path),
        )
        return app, runtime

    try:
        # ------------------------------------------------------------ first boot
        app, runtime = build()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
                await _first_boot(ui, record, db, target, evidence)
                await _dashboard(ui, record, db, simulator, evidence)
                await _pause_resume(ui, record, db, evidence)
                await _open_close(ui, record, db, simulator, runtime, evidence)
                await _refresh(ui, record, db, simulator, evidence)
                await _add_replace_delete(ui, record, db, directory, evidence)
                # Persist a PAUSED control so the restart has something to recover.
                await ui.post("/monitoring/pause", headers={"HX-Request": "true"})
                before_shutdown = db.identities()
                adjustment = db.value(
                    "SELECT operator_population_adjustment FROM runtime_control"
                )
        record.check(
            "shutdown_zero_contexts_owners_and_chrome",
            db.owners() == 0 and _chrome_processes() == 0,
            owners=db.owners(),
            chrome_processes=_chrome_processes(),
        )
        record.check(
            "shutdown_deleted_or_replaced_nothing",
            db.identities() == before_shutdown,
            sessions=len(before_shutdown),
        )

        # --------------------------------------------------------------- restart
        app, runtime = build()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
                root = await ui.get("/")
                setup = await ui.get("/setup")
                summary = (await ui.get("/partials/summary")).text
                record.check(
                    "restart_skips_setup",
                    root.headers.get("location") == "/dashboard"
                    and setup.headers.get("location") == "/dashboard",
                )
                record.check(
                    "restart_preserves_run_config",
                    _dd(summary, "Requested Sessions") == str(REQUESTED)
                    and target in summary,
                )
                retarget = await ui.post(
                    "/setup",
                    data={"target_url": "https://other.example.test/", "requested_sessions": "9"},
                )
                record.check(
                    "target_cannot_be_changed_for_existing_population",
                    retarget.headers.get("location") == "/dashboard"
                    and db.rows("SELECT target_url, requested_sessions FROM run_config")
                    == [(target, REQUESTED)],
                )
                record.check(
                    "restart_preserves_paused_state",
                    "PAUSED" in summary and "Resume Monitoring" in summary,
                )
                await asyncio.sleep(1.5)
                record.check(
                    "restart_preserves_sessions_and_queue_ids",
                    db.identities() == before_shutdown,
                    sessions=len(before_shutdown),
                )
                record.check(
                    "restart_preserves_operator_population_adjustment",
                    db.value("SELECT operator_population_adjustment FROM runtime_control")
                    == adjustment,
                    adjustment=adjustment,
                )
                record.check(
                    "restart_creates_no_refill_after_add_and_delete",
                    simulator.new_identities == evidence["identities_created_before_restart"],
                    new_identities=simulator.new_identities,
                )
                resumed = await ui.post("/monitoring/resume", headers={"HX-Request": "true"})
                any_session = next(iter(before_shutdown))
                checked = db.checked_at(any_session)
                elapsed = await until(lambda: db.checked_at(any_session) != checked, timeout=15)
                record.check(
                    "restart_resume_monitoring_checks_again",
                    "RUNNING" in resumed.text and elapsed is not None,
                    seconds=elapsed,
                )
        record.check(
            "final_shutdown_clean",
            db.owners() == 0 and _chrome_processes() == 0,
            owners=db.owners(),
        )
        await _partial_resume(directory / "partial", simulator, record, evidence, headed=headed)
    finally:
        await simulator.close()

    passed = sum(check.passed for check in record.checks)
    evidence.pop("session_ids", None)  # synthetic, but identifiers stay out of reports
    return {
        "measured_at": datetime.now().astimezone().isoformat(),
        "target": "LocalQueueSimulator on 127.0.0.1 (not Queue-it)",
        "passed": passed,
        "failed": len(record.checks) - passed,
        "evidence": evidence,
        "checks": [asdict(check) for check in record.checks],
    }


async def _partial_resume(
    directory: Path,
    simulator: LocalQueueSimulator,
    record: Recorder,
    evidence: dict[str, Any],
    *,
    headed: bool,
) -> None:
    """Stop during initial acquisition, restart, and resume only the deficit."""

    requested = 6
    directory.mkdir(parents=True, exist_ok=True)
    database_path = directory / "partial.sqlite3"
    db = Database(database_path)
    settings = workflow_settings(directory, database=database_path).model_copy(
        update={"creation_workers": 1}
    )
    # Slow each new identity so shutdown lands mid-acquisition deterministically.
    simulator.slow_seconds = 0.4

    def build() -> Any:
        repository = SQLiteSessionRepository(database_path)
        runtime = ApplicationRunRuntime(
            settings=settings, repository=repository, manual_headless=not headed
        )
        return create_app(
            settings=settings,
            repository=repository,
            runtime=runtime,
            instance_lock=InstanceLock.for_database(database_path),
        )

    app = build()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
            original = simulator.new_identity_prefix
            simulator.new_identity_prefix = "sim-partial"
            simulator.slow_ids.update(f"sim-partial-{index:05d}" for index in range(1, 200))
            await ui.post(
                "/setup",
                data={"target_url": simulator.entry_url, "requested_sessions": str(requested)},
            )
            await until(lambda: db.valid() >= 2, timeout=30, interval=0.05)
    interrupted = db.identities()
    record.check(
        "partial_acquisition_interrupted",
        0 < len(interrupted) < requested and db.owners() == 0,
        valid_at_shutdown=db.valid(),
    )
    app = build()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
            skipped = (await ui.get("/")).headers.get("location") == "/dashboard"
            elapsed = await until(lambda: db.valid() >= requested, timeout=60)
            await asyncio.sleep(1.0)
    final = db.identities()
    record.check(
        "partial_restart_resumes_only_deficit",
        skipped
        and elapsed is not None
        and db.valid() == requested
        and len(final) == requested
        and all(final.get(key) == value for key, value in interrupted.items()),
        valid_before_restart=len(interrupted),
        valid_after_restart=db.valid(),
    )
    evidence["partial_resume"] = {
        "requested": requested,
        "valid_at_shutdown": len(interrupted),
        "valid_after_restart": db.valid(),
    }
    simulator.new_identity_prefix = original
    simulator.slow_ids.clear()


async def _first_boot(
    ui: httpx.AsyncClient,
    record: Recorder,
    db: Database,
    target: str,
    evidence: dict[str, Any],
) -> None:
    root = await ui.get("/")
    setup = await ui.get("/setup")
    record.check(
        "first_boot_shows_setup",
        root.headers.get("location") == "/setup"
        and 'name="target_url"' in setup.text
        and 'name="requested_sessions"' in setup.text,
    )
    for name, url, count in (
        ("invalid_url_rejected", "not-a-url", "3"),
        ("credential_url_rejected", "https://user:secret@example.test/q", "3"),
        ("zero_count_rejected", target, "0"),
        ("non_integer_count_rejected", target, "three"),
        ("over_limit_count_rejected", target, "51"),
    ):
        response = await ui.post(
            "/setup", data={"target_url": url, "requested_sessions": count}
        )
        record.check(name, response.status_code == 422, status=response.status_code)
    record.check("invalid_setup_persists_nothing", db.value("SELECT COUNT(*) FROM run_config") == 0)

    tasks_before = len(asyncio.all_tasks())
    started = time.perf_counter()
    response = await ui.post(
        "/setup", data={"target_url": target, "requested_sessions": str(REQUESTED)}
    )
    tasks_after = len(asyncio.all_tasks())
    record.check(
        "valid_setup_persists_and_redirects",
        response.status_code == 303
        and response.headers.get("location") == "/dashboard"
        and db.value("SELECT requested_sessions FROM run_config") == REQUESTED,
    )
    record.check(
        "setup_tasks_bounded",
        tasks_after - tasks_before < 20,
        task_delta=tasks_after - tasks_before,
    )
    samples: list[int] = []
    rows_seen: list[int] = []

    async def complete() -> bool:
        samples.append(db.valid())
        page = await ui.get("/partials/sessions")
        rows_seen.append(len(_session_ids(page.text)))
        return samples[-1] >= REQUESTED

    elapsed = await until(complete, timeout=60, interval=0.25)
    evidence["acquisition_seconds"] = round(time.perf_counter() - started, 2)
    evidence["valid_samples"] = sorted(set(samples))
    evidence["dashboard_rows_seen"] = sorted(set(rows_seen))
    record.check(
        "bounded_acquisition_reaches_target",
        elapsed is not None and db.valid() == REQUESTED,
        seconds=evidence["acquisition_seconds"],
        chrome_processes=_chrome_processes(),
    )
    record.check(
        "dashboard_shows_sessions_as_they_appear",
        len(evidence["dashboard_rows_seen"]) >= 2,
        rows_seen=evidence["dashboard_rows_seen"],
    )
    record.check(
        "chrome_processes_bounded_during_acquisition",
        0 <= _chrome_processes() <= 1,
        chrome_processes=_chrome_processes(),
    )


async def _dashboard(
    ui: httpx.AsyncClient,
    record: Recorder,
    db: Database,
    simulator: LocalQueueSimulator,
    evidence: dict[str, Any],
) -> None:
    sessions = db.rows("SELECT session_id, queue_id FROM queue_sessions ORDER BY created_at")
    evidence["session_ids"] = [row[0] for row in sessions]
    for _, queue_id in sessions:
        simulator.progress[queue_id] = 44
    first_id, first_queue = sessions[0]

    async def progress_visible() -> bool:
        page = await ui.get("/partials/sessions")
        return "44.0%" in page.text

    elapsed = await until(progress_visible, timeout=20, interval=0.3)
    page = (await ui.get("/partials/sessions")).text
    record.check(
        "auto_refresh_shows_monitored_progress",
        elapsed is not None,
        seconds=elapsed,
    )
    record.check(
        "dashboard_shows_ids_status_progress",
        first_id in page and first_queue in page and "ACTIVE_QUEUE" in page and "%" in page,
    )
    record.check(
        "dashboard_hides_transfer_and_state",
        "?q=" not in page and ".json" not in page and "storage" not in page.lower(),
    )
    search_session = await ui.get(f"/partials/sessions?search={first_id[:13]}")
    search_queue = await ui.get(f"/partials/sessions?search={first_queue}")
    record.check(
        "search_by_session_and_queue_id",
        _session_ids(search_session.text) == [first_id]
        and _session_ids(search_queue.text) == [first_id],
    )
    active = await ui.get("/partials/sessions?status=ACTIVE_QUEUE")
    failed = await ui.get("/partials/sessions?status=FAILED")
    record.check(
        "lifecycle_filter",
        len(_session_ids(active.text)) == REQUESTED and _session_ids(failed.text) == [],
    )
    summary = (await ui.get("/partials/summary")).text
    record.check(
        "summary_aggregates_match_database",
        _dd(summary, "Valid Managed Sessions") == str(db.valid())
        and _dd(summary, "Total persisted sessions")
        == str(db.value("SELECT COUNT(*) FROM queue_sessions"))
        and _dd(summary, "Remaining To Initial Target") == "0"
        and "COMPLETE" in summary,
    )
    changes_before = db.value("SELECT COUNT(*) FROM queue_sessions")
    contexts_before = _dd(summary, "Active BrowserContexts")
    for _ in range(10):
        await ui.get("/partials/sessions")
        await ui.get("/partials/summary")
    record.check(
        "polling_is_read_only",
        db.value("SELECT COUNT(*) FROM queue_sessions") == changes_before,
        active_contexts_while_idle=contexts_before,
    )


async def _pause_resume(
    ui: httpx.AsyncClient,
    record: Recorder,
    db: Database,
    evidence: dict[str, Any],
) -> None:
    identities = db.identities()
    statuses = dict(db.rows("SELECT session_id, status FROM queue_sessions"))
    paused = await ui.post("/monitoring/pause", headers={"HX-Request": "true"})
    record.check(
        "pause_persisted",
        "PAUSED" in paused.text
        and db.value("SELECT monitoring_paused FROM runtime_control") == 1,
    )
    await asyncio.sleep(1.0)  # let any check already in flight finish
    snapshot = dict(db.rows("SELECT session_id, last_checked_at FROM queue_sessions"))
    await asyncio.sleep(4.5)  # more than two polling intervals
    later = dict(db.rows("SELECT session_id, last_checked_at FROM queue_sessions"))
    summary = (await ui.get("/partials/summary")).text
    due = int(_dd(summary, "Due backlog") or "0")
    record.check("pause_stops_new_claims", snapshot == later)
    record.check("due_backlog_visible_while_paused", due > 0, due_backlog=due)
    record.check(
        "pause_keeps_queue_ids_and_status",
        db.identities() == identities
        and dict(db.rows("SELECT session_id, status FROM queue_sessions")) == statuses,
    )
    resumed = await ui.post("/monitoring/resume", headers={"HX-Request": "true"})
    first = evidence["session_ids"][0]
    elapsed = await until(lambda: db.checked_at(first) != later[first], timeout=15)
    record.check(
        "resume_restarts_monitoring",
        "RUNNING" in resumed.text and elapsed is not None,
        seconds=elapsed,
    )


async def _open_close(
    ui: httpx.AsyncClient,
    record: Recorder,
    db: Database,
    simulator: LocalQueueSimulator,
    runtime: ApplicationRunRuntime,
    evidence: dict[str, Any],
) -> None:
    session_a, session_b = evidence["session_ids"][:2]
    queue_a = db.identities()[session_a]
    assert queue_a is not None
    await until(lambda: db.session(session_a)["worker_id"] is None, timeout=10)  # type: ignore[index]
    # Simulator pages are static, so the value is set before Open; the close-time
    # inspection of the still-open page is what persists it (see the checks below).
    simulator.progress[queue_a] = 58
    opened = await ui.post(f"/sessions/{session_a}/open", headers={"HX-Request": "true"})
    row = db.session(session_a) or {}
    record.check(
        "open_in_chrome_restores_existing_identity",
        "Opened in Chrome" in opened.text
        and "OPEN IN CHROME" in opened.text
        and row.get("manual_owner_id") is not None
        and row.get("queue_id") == queue_a,
        headed=evidence["headed_manual_chrome"],
        chrome_processes=_chrome_processes(),
    )
    record.check(
        "open_in_chrome_is_not_queue_status",
        row.get("status") != "OPEN_IN_CHROME" and "ACTIVE_QUEUE" in opened.text,
        persisted_status=row.get("status"),
    )
    checked_a = db.checked_at(session_a)
    next_while_open = (db.session(session_a) or {}).get("next_check_at")
    checked_b = db.checked_at(session_b)
    await until(lambda: db.checked_at(session_b) != checked_b, timeout=15)
    await asyncio.sleep(2.5)
    record.check(
        "scheduler_skips_open_session",
        db.checked_at(session_a) == checked_a and db.checked_at(session_b) != checked_b,
    )
    refresh_busy = await ui.post(
        f"/sessions/{session_a}/refresh", headers={"HX-Request": "true"}
    )
    delete_busy = await ui.post(f"/sessions/{session_a}/delete", headers={"HX-Request": "true"})
    record.check(
        "refresh_and_delete_refused_while_open",
        "Close the Chrome session" in refresh_busy.text
        and "Close the Chrome session" in delete_busy.text
        and db.session(session_a) is not None,
    )
    capacity = await runtime.capacity()
    record.check(
        "manual_contexts_bounded",
        capacity.active_contexts <= 5,
        active_contexts=capacity.active_contexts,
    )
    closed = await ui.post(f"/sessions/{session_a}/close", headers={"HX-Request": "true"})
    row = db.session(session_a) or {}
    record.check(
        "close_releases_ownership_and_persists_latest_state",
        "Chrome session closed" in closed.text
        and row.get("manual_owner_id") is None
        and db.progress(session_a) == 58.0
        and row.get("next_check_at") != next_while_open
        and row.get("queue_id") == queue_a,
        progress_after_close=db.progress(session_a),
        rescheduled_by_close_inspection=row.get("next_check_at") != next_while_open,
    )
    checked_after_close = db.checked_at(session_a)
    elapsed = await until(lambda: db.checked_at(session_a) != checked_after_close, timeout=15)
    record.check("closed_session_returns_to_monitoring", elapsed is not None, seconds=elapsed)

    # Identity mismatch: the page now shows a different Queue ID. Automatic monitoring
    # is paused so only the Open path observes the mismatch.
    await ui.post("/monitoring/pause", headers={"HX-Request": "true"})
    simulator.mismatch_ids.add(queue_a)
    await until(lambda: db.session(session_a)["worker_id"] is None, timeout=10)  # type: ignore[index]
    mismatch = await ui.post(f"/sessions/{session_a}/open", headers={"HX-Request": "true"})
    row = db.session(session_a) or {}
    record.check(
        "identity_mismatch_preserves_expected_queue_id",
        "Opened in Chrome" not in mismatch.text
        and row.get("queue_id") == queue_a
        and row.get("manual_owner_id") is None
        and simulator.new_identities == REQUESTED,
        message=re.findall(r'role="alert">([^<]*)<', mismatch.text),
        persisted_status=row.get("status"),
    )
    simulator.mismatch_ids.discard(queue_a)
    await ui.post("/monitoring/resume", headers={"HX-Request": "true"})


async def _refresh(
    ui: httpx.AsyncClient,
    record: Recorder,
    db: Database,
    simulator: LocalQueueSimulator,
    evidence: dict[str, Any],
) -> None:
    session_b = evidence["session_ids"][1]
    queue_b = db.identities()[session_b]
    assert queue_b is not None
    await ui.post("/monitoring/pause", headers={"HX-Request": "true"})
    await until(lambda: db.session(session_b)["worker_id"] is None, timeout=10)  # type: ignore[index]
    simulator.progress[queue_b] = 77
    response = await ui.post(
        f"/sessions/{session_b}/refresh?token=accept-refresh", headers={"HX-Request": "true"}
    )
    elapsed = await until(lambda: db.progress(session_b) == 77.0, timeout=20)
    record.check(
        "refresh_now_works_while_paused",
        "Refresh requested" in response.text and elapsed is not None,
        seconds=elapsed,
    )
    record.check(
        "refresh_keeps_queue_id",
        db.identities()[session_b] == queue_b and db.session(session_b)["worker_id"] is None,  # type: ignore[index]
    )
    await ui.post("/monitoring/resume", headers={"HX-Request": "true"})


async def _add_replace_delete(
    ui: httpx.AsyncClient,
    record: Recorder,
    db: Database,
    directory: Path,
    evidence: dict[str, Any],
) -> None:
    before = db.identities()
    duplicate = await asyncio.gather(
        ui.post("/sessions/new?token=accept-add", headers={"HX-Request": "true"}),
        ui.post("/sessions/new?token=accept-add", headers={"HX-Request": "true"}),
    )
    elapsed = await until(lambda: db.valid() == REQUESTED + 1, timeout=30)
    await asyncio.sleep(1.0)
    added = set(db.identities()) - set(before)
    record.check(
        "add_creates_exactly_one_despite_duplicate_submit",
        elapsed is not None and len(added) == 1 and db.valid() == REQUESTED + 1,
        statuses=[response.status_code for response in duplicate],
    )
    record.check(
        "add_keeps_requested_target",
        db.value("SELECT requested_sessions FROM run_config") == REQUESTED
        and db.value("SELECT operator_population_adjustment FROM runtime_control") == 1,
    )
    summary = (await ui.get("/partials/summary")).text
    record.check(
        "managed_count_may_exceed_requested",
        _dd(summary, "Valid Managed Sessions") == str(REQUESTED + 1)
        and _dd(summary, "Remaining To Initial Target") == "0",
    )

    session_c = evidence["session_ids"][2]
    old_queue_c = before[session_c]
    await until(lambda: (db.session(session_c) or {}).get("worker_id") is None, timeout=10)
    snapshot = db.identities()
    replace = await ui.post(
        f"/sessions/{session_c}/replace?token=accept-replace", headers={"HX-Request": "true"}
    )
    elapsed = await until(lambda: db.session(session_c) is None, timeout=30)
    after = db.identities()
    replacement = set(after) - set(snapshot)
    new_queue = after[next(iter(replacement))] if replacement else None
    record.check(
        "replace_creates_new_independent_identity_then_removes_old",
        "Replace requested" in replace.text
        and elapsed is not None
        and len(replacement) == 1
        and new_queue is not None
        and new_queue != old_queue_c
        and new_queue not in snapshot.values(),
    )
    record.check(
        "replace_preserves_population_size",
        len(after) == len(snapshot)
        and db.value("SELECT operator_population_adjustment FROM runtime_control") == 1,
    )
    record.check(
        "replace_leaves_unrelated_sessions",
        all(after.get(key) == value for key, value in snapshot.items() if key != session_c),
    )

    session_d = evidence["session_ids"][3]
    state_file = directory / "state" / f"{session_d}.json"
    had_state = state_file.exists()
    await until(lambda: (db.session(session_d) or {}).get("worker_id") is None, timeout=10)
    snapshot = db.identities()
    delete = await ui.post(
        f"/sessions/{session_d}/delete?token=accept-delete", headers={"HX-Request": "true"}
    )
    elapsed = await until(
        lambda: db.session(session_d) is None and not state_file.exists(), timeout=15
    )
    after = db.identities()
    record.check(
        "delete_removes_session_progress_and_state",
        "Delete requested" in delete.text
        and elapsed is not None
        and db.value("SELECT COUNT(*) FROM queue_progress WHERE session_id = ?", (session_d,))
        == 0
        and not state_file.exists(),
        state_file_existed=had_state,
    )
    record.check(
        "delete_leaves_unrelated_sessions",
        all(after.get(key) == value for key, value in snapshot.items() if key != session_d)
        and len(after) == len(snapshot) - 1,
    )
    repeated = await ui.post(f"/sessions/{session_d}/delete", headers={"HX-Request": "true"})
    record.check("repeat_delete_is_idempotent", "already deleted" in repeated.text)
    # REQUESTED initial identities + 1 Add + 1 Replace; the mismatch Open created none.
    evidence["identities_created_before_restart"] = REQUESTED + 2


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="show the operator Chrome window")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="phase5-workflow-") as directory:
        result = asyncio.run(run_workflow(Path(directory), headed=args.headed))
    text = json.dumps(result, indent=2, default=str)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(f"{result['passed']} passed, {result['failed']} failed")
    raise SystemExit(0 if result["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
