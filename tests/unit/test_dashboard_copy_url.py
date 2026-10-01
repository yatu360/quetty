"""The per-session Copy URL action: explicit, read-only, and URL-free elsewhere."""

import asyncio
import hashlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web_ui import FakeRuntime, settings

from queue_load_test.models import (
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web import create_app
from queue_load_test.web.app import static_url_builder

# Synthetic only; never a real Queue-it identity.
TRANSFER_URL = "https://queue.synthetic.invalid/?c=fake&e=copy-url&q=fake-queue-0001&t=fake-token"
REPLACEMENT_URL = "https://queue.synthetic.invalid/?c=fake&e=copy-url&q=fake-queue-0002&t=other"
STATE_PATH = "synthetic-browser-state-copy-url.json"

# Repository methods that would mutate rows, take leases, or claim ownership.
_FORBIDDEN = (
    "create",
    "create_many",
    "update",
    "save_progress",
    "record_proxy_ip",
    "acquire_operator_lease",
    "renew_operator_lease",
    "acquire_manual_ownership",
    "renew_manual_ownership",
    "release_manual_ownership",
    "claim_due_sessions",
    "release_lease",
    "delete_owned_session",
    "delete_unowned_session",
    "set_monitoring_paused",
    "adjust_operator_population",
    "reset_all",
)


def _seed(database: Path) -> None:
    repository = SQLiteSessionRepository(database)

    async def seed() -> None:
        await repository.initialize()
        await repository.create_run(
            RunConfig(
                run_id="copy-url-run",
                target_url="https://staging.synthetic.invalid/queue",
                requested_sessions=3,
                created_at=datetime.now(UTC),
            )
        )
        await repository.create(
            QueueSession(
                session_id="with-url",
                queue_id="fake-queue-0001",
                transfer_url=TRANSFER_URL,
                mode=SessionMode.HYBRID,
                state_path=Path(STATE_PATH),
                status=QueueStatus.ACTIVE_QUEUE,
            ),
            QueueProgress(session_id="with-url", progress_percentage=12.5),
        )
        # A creation reservation persists before Queue-it issues a transfer URL.
        await repository.create(
            QueueSession(
                session_id="without-url",
                transfer_url="",
                mode=SessionMode.HYBRID,
                state_path=Path("synthetic-reservation-state.json"),
                status=QueueStatus.CREATING,
            )
        )
        await repository.close()

    asyncio.run(seed())


def _client(database: Path) -> tuple[TestClient, SQLiteSessionRepository, FakeRuntime]:
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(repository)
    app = create_app(settings=settings(database), repository=repository, runtime=runtime)
    return TestClient(app), repository, runtime


def _rows(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT * FROM queue_sessions ORDER BY session_id"
        ).fetchall()


def test_copy_button_is_available_and_endpoint_returns_exact_persisted_url(
    tmp_path: Path,
) -> None:
    database = tmp_path / "copy.sqlite3"
    _seed(database)
    client, _, _ = _client(database)
    with client:
        partial = client.get("/partials/sessions").text
        assert 'data-copy-session="with-url"' in partial
        assert 'aria-label="Copy transfer URL"' in partial

        response = client.get("/sessions/with-url/transfer-url")
        assert response.status_code == 200
        assert response.json() == {"available": True, "transfer_url": TRANSFER_URL}
        assert response.headers["cache-control"] == "no-store"


def test_session_without_url_has_disabled_button_and_no_fabricated_url(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-url.sqlite3"
    _seed(database)
    client, _, _ = _client(database)
    with client:
        partial = client.get("/partials/sessions").text
        assert 'data-copy-session="without-url"' not in partial
        row = partial[partial.index("without-url") :]
        row = row[: row.index("</tr>")]
        assert '<button type="button" class="copy-url" disabled' in row

        response = client.get("/sessions/without-url/transfer-url")
        assert response.status_code == 404
        assert response.json()["available"] is False
        assert "transfer_url" not in response.json()
        assert "http" not in response.text


def test_missing_and_replaced_session_ids_are_not_found(tmp_path: Path) -> None:
    database = tmp_path / "deleted.sqlite3"
    _seed(database)
    client, repository, _ = _client(database)
    with client:
        unknown = client.get("/sessions/never-existed/transfer-url")
        assert unknown.status_code == 404
        assert unknown.json() == {"available": False, "error": "Session not found."}

        # Replace completed after the row rendered: the replacement has its own ID and
        # the old ID must not resolve to it.
        asyncio.run(
            repository.create(
                QueueSession(
                    session_id="replacement",
                    queue_id="fake-queue-0002",
                    transfer_url=REPLACEMENT_URL,
                    mode=SessionMode.HYBRID,
                    state_path=Path("synthetic-replacement-state.json"),
                    status=QueueStatus.ACTIVE_QUEUE,
                )
            )
        )
        assert asyncio.run(repository.delete_unowned_session("with-url"))
        stale = client.get("/sessions/with-url/transfer-url")
        assert stale.status_code == 404
        assert REPLACEMENT_URL not in stale.text
        assert TRANSFER_URL not in stale.text
        assert client.get("/sessions/replacement/transfer-url").json()["transfer_url"] == (
            REPLACEMENT_URL
        )


def test_dashboard_and_partials_never_render_the_url_or_state_path(tmp_path: Path) -> None:
    database = tmp_path / "projection.sqlite3"
    _seed(database)
    client, _, _ = _client(database)
    with client:
        pages = [
            client.get("/dashboard").text,
            client.get("/partials/sessions").text,
            client.get("/partials/sessions?search=with-url").text,
            client.get("/partials/summary").text,
        ]
    for html in pages:
        assert TRANSFER_URL not in html
        assert "fake-token" not in html
        assert "queue.synthetic.invalid" not in html
        assert STATE_PATH not in html
    assert "Copy URL" in pages[0]


def test_copy_is_read_only_in_every_runtime_state(tmp_path: Path) -> None:
    database = tmp_path / "read-only.sqlite3"
    _seed(database)
    client, repository, runtime = _client(database)
    with client:
        # Paused monitoring, CHECKING (live scheduler lease), and OPEN_IN_CHROME.
        client.post("/monitoring/pause")
        now = datetime.now(UTC)
        asyncio.run(
            repository.create(
                QueueSession(
                    session_id="checking",
                    queue_id="fake-queue-0003",
                    transfer_url=TRANSFER_URL + "-checking",
                    mode=SessionMode.HYBRID,
                    state_path=Path("synthetic-checking-state.json"),
                    status=QueueStatus.CHECKING,
                    worker_id="synthetic-worker",
                    lease_until=now + timedelta(minutes=5),
                )
            )
        )
        client.post("/sessions/with-url/open")
        partial = client.get("/partials/sessions").text
        assert "OPEN IN BROWSER" in partial
        assert "CHECKING" in partial
        assert 'data-copy-session="with-url"' in partial
        assert 'data-copy-session="checking"' in partial

        before = _rows(database)
        calls: list[str] = []
        for name in _FORBIDDEN:
            def forbidden(*_: object, _name: str = name, **__: object) -> None:
                calls.append(_name)
                raise AssertionError(f"Copy URL called {_name}")

            setattr(repository, name, forbidden)
        open_calls, close_calls = list(runtime.open_calls), list(runtime.close_calls)
        actions, started = list(runtime.actions), list(runtime.started)

        assert client.get("/sessions/with-url/transfer-url").json()["transfer_url"] == (
            TRANSFER_URL
        )
        assert client.get("/sessions/checking/transfer-url").json()["transfer_url"] == (
            TRANSFER_URL + "-checking"
        )
        assert calls == []
        assert _rows(database) == before
        assert runtime.open_calls == open_calls
        assert runtime.close_calls == close_calls
        assert runtime.actions == actions
        assert runtime.started == started


def test_url_never_reaches_logs_metrics_or_unrelated_action_responses(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "redaction.sqlite3"
    _seed(database)
    client, _, _ = _client(database)
    caplog.set_level(logging.DEBUG)
    with client:
        responses = [
            client.get("/dashboard").text,
            client.get("/partials/sessions").text,
            client.post("/sessions/with-url/refresh").text,
            client.post("/sessions/with-url/replace").text,
            client.post("/sessions/with-url/open").text,
            client.post("/sessions/with-url/close").text,
            client.post("/sessions/new").text,
            client.post("/monitoring/pause").text,
            client.get("/metrics").text,
            client.get("/sessions/never-existed/transfer-url").text,
            client.get("/sessions/without-url/transfer-url").text,
        ]
        copied = client.get("/sessions/with-url/transfer-url")
    assert copied.json()["transfer_url"] == TRANSFER_URL
    for body in responses:
        assert TRANSFER_URL not in body
        assert "fake-token" not in body
    assert TRANSFER_URL not in caplog.text
    assert "fake-token" not in caplog.text


def test_failure_while_copying_returns_a_generic_url_free_response(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "failure.sqlite3"
    _seed(database)
    client, repository, _ = _client(database)
    caplog.set_level(logging.DEBUG)

    async def broken(_: str) -> None:
        raise sqlite3.OperationalError(f"database is locked near {TRANSFER_URL}")

    with client:
        repository.get = broken  # type: ignore[method-assign]
        response = client.get("/sessions/with-url/transfer-url")
    assert response.status_code == 503
    assert TRANSFER_URL not in response.text
    assert TRANSFER_URL not in caplog.text


def test_pages_reference_content_versioned_static_assets(tmp_path: Path) -> None:
    database = tmp_path / "static.sqlite3"
    _seed(database)
    client, _, _ = _client(database)
    static = Path(create_app.__code__.co_filename).parent / "static"
    with client:
        dashboard = client.get("/dashboard").text
        for name in ("dashboard.js", "htmx.min.js", "app.css"):
            version = hashlib.sha256((static / name).read_bytes()).hexdigest()[:12]
            url = f"/static/{name}?v={version}"
            # A changed file gets a new URL, so a cached old script cannot shadow it.
            assert url in dashboard
            served = client.get(url)
            assert served.status_code == 200
            assert served.content == (static / name).read_bytes()


def test_static_url_changes_when_file_content_changes(tmp_path: Path) -> None:
    (tmp_path / "dashboard.js").write_text("old", encoding="utf-8")
    before = static_url_builder(tmp_path)("dashboard.js")
    (tmp_path / "dashboard.js").write_text("new", encoding="utf-8")
    after = static_url_builder(tmp_path)("dashboard.js")
    assert before != after
    assert before.startswith("/static/dashboard.js?v=")
    assert static_url_builder(tmp_path)("missing.js") == "/static/missing.js"
