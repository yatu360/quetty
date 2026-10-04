"""Operator-chosen session names: local dashboard labels for every strategy."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web_ui import FakeRuntime, settings

from queue_load_test.models import (
    MonitoringStrategy,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
    normalize_session_name,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web import create_app


def session(session_id: str, queue_id: str | None = None) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=queue_id,
        transfer_url=f"https://queue.synthetic.invalid/?q={queue_id}" if queue_id else "",
        mode=SessionMode.HYBRID,
        state_path=Path(f"state/{session_id}.json"),
        status=QueueStatus.PARKED if queue_id else QueueStatus.FAILED,
    )


def run(strategy: MonitoringStrategy = MonitoringStrategy.HEADED_WINDOW) -> RunConfig:
    return RunConfig(
        run_id="names",
        target_url="https://staging.synthetic.invalid/",
        requested_sessions=2,
        created_at=datetime.now(UTC),
        monitoring_strategy=strategy,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice's laptop", "Alice's laptop"),
        ("  spaced   out\tname ", "spaced out name"),
        ("", None),
        ("   ", None),
        (None, None),
        ("Café — 東京 🎟", "Café — 東京 🎟"),
        ("x" * 80, "x" * 80),
    ],
)
def test_names_are_normalized(raw: str | None, expected: str | None) -> None:
    assert normalize_session_name(raw) == expected


@pytest.mark.parametrize("raw", ["x" * 81, "bad\x00name", "bell\x07", "del\x7f"])
def test_invalid_names_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_session_name(raw)


async def test_rename_sets_clears_and_survives_ordinary_row_writes(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "names.sqlite3")
    stored = await repository.create(session("s1", "queue-1"))

    assert await repository.rename_session("s1", "  Front door  ")
    # A monitoring write of the full row (status, progress, schedule) keeps the name.
    stored.status = QueueStatus.PRE_QUEUE
    stored.next_check_at = datetime.now(UTC) + timedelta(minutes=2)
    await repository.update(stored, QueueProgress(session_id="s1", pre_queue=True))
    page = await repository.list_session_summaries(page=1, page_size=10)
    assert page.items[0].display_name == "Front door"
    assert page.items[0].status is QueueStatus.PRE_QUEUE

    assert await repository.rename_session("s1", "")
    page = await repository.list_session_summaries(page=1, page_size=10)
    assert page.items[0].display_name is None
    assert not await repository.rename_session("missing", "anything")
    await repository.close()


async def test_rename_needs_no_lease_and_works_while_open_in_a_browser(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "owned.sqlite3")
    await repository.create(session("s1", "queue-1"))
    now = datetime.now(UTC)
    await repository.acquire_manual_ownership(
        "s1", owner_id="manual-x", now=now, lease_until=now + timedelta(seconds=30), capacity=5
    )

    assert await repository.rename_session("s1", "Window one")
    owned = await repository.get("s1")
    assert owned is not None and owned.manual_owner_id == "manual-x"
    assert owned.queue_id == "queue-1"
    await repository.close()


async def test_search_matches_names(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "search.sqlite3")
    await repository.create(session("s1", "queue-1"))
    await repository.create(session("s2", "queue-2"))
    await repository.rename_session("s2", "Kitchen tablet")

    page = await repository.list_session_summaries(page=1, page_size=10, search="kitchen")
    assert [item.session_id for item in page.items] == ["s2"]
    await repository.close()


def test_legacy_database_gains_the_column_without_touching_rows(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE queue_sessions (session_id TEXT PRIMARY KEY, queue_id TEXT UNIQUE, "
            "transfer_url TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, "
            "state_path TEXT NOT NULL, created_at TEXT NOT NULL, last_checked_at TEXT, "
            "next_check_at TEXT, attempt_count INTEGER NOT NULL, last_error TEXT, "
            "worker_id TEXT, lease_until TEXT)"
        )
        connection.execute(
            "INSERT INTO queue_sessions VALUES ('old', 'queue-old', 'https://q.invalid/?q=x', "
            "'HYBRID', 'PARKED', 'state/old.json', '2026-01-01T00:00:00+00:00', NULL, NULL, "
            "0, NULL, NULL, NULL)"
        )

    async def check() -> None:
        repository = SQLiteSessionRepository(database)
        await repository.initialize()
        page = await repository.list_session_summaries(page=1, page_size=10)
        assert page.items[0].display_name is None
        assert await repository.rename_session("old", "Legacy")
        stored = await repository.get("old")
        assert stored is not None and stored.queue_id == "queue-old"
        await repository.close()

    asyncio.run(check())


@pytest.mark.parametrize("strategy", tuple(MonitoringStrategy))
def test_dashboard_rename_route_sets_escapes_and_clears_for_every_strategy(
    tmp_path: Path, strategy: MonitoringStrategy, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    database = tmp_path / f"route-{strategy.value}.sqlite3"
    repository = SQLiteSessionRepository(database)
    app = create_app(settings=settings(database), repository=repository, runtime=FakeRuntime())
    hostile = "<script>alert(1)</script> Bob"

    with TestClient(app) as client:
        client.portal.call(repository.create_run, run(strategy))  # type: ignore[union-attr]
        client.portal.call(repository.create, session("s1", "queue-1"))  # type: ignore[union-attr]
        initial = client.get("/partials/sessions").text
        assert 'data-rename-url="/sessions/s1/name?' in initial

        saved = client.post("/sessions/s1/name?page=1", data={"name": hostile})
        assert saved.status_code == 200
        assert "Session name saved" in saved.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt; Bob" in saved.text
        assert "<script>alert(1)</script>" not in saved.text
        assert "&lt;script&gt;" in client.get("/partials/sessions").text

        rejected = client.post("/sessions/s1/name", data={"name": "x" * 81})
        assert "at most 80 characters" in rejected.text
        missing = client.post("/sessions/nope/name", data={"name": "A"})
        assert "Session no longer exists" in missing.text

        cleared = client.post("/sessions/s1/name", data={"name": "  "})
        assert "Session name cleared" in cleared.text
        assert 'class="session-name"' not in cleared.text

    assert "alert(1)" not in caplog.text
