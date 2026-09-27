import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from queue_load_test.config import Settings
from queue_load_test.models import (
    BrowserRuntimeState,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.repository import SQLiteSessionRepository, UnownedSessionsError
from queue_load_test.web import create_app
from queue_load_test.web.service import RuntimeCapacity


class FakeRuntime:
    def __init__(self) -> None:
        self.started: list[RunConfig] = []
        self.closed = 0

    async def start_run(self, run: RunConfig) -> None:
        self.started.append(run)

    async def capacity(self) -> RuntimeCapacity:
        return RuntimeCapacity(
            active_contexts=0,
            maximum_active_contexts=25,
            chrome_processes=0,
        )

    def error(self) -> str | None:
        return None

    async def close(self) -> None:
        self.closed += 1


def settings(database: Path, *, limit: int = 10_000) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite:///{database}",
        CHROME_PROCESS_COUNT=1,
        MAX_CONTEXTS_PER_BROWSER=25,
        MAX_ACTIVE_CONTEXTS=25,
        MAX_MANUAL_REQUESTED_SESSIONS=limit,
    )


def test_first_boot_shows_setup_and_valid_submission_starts_one_bounded_runtime(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "ui.sqlite3")
    runtime = FakeRuntime()
    app = create_app(settings=settings(tmp_path / "ui.sqlite3"), repository=repository, runtime=runtime)

    with TestClient(app) as client:
        response = client.get("/", follow_redirects=True)
        assert response.status_code == 200
        assert "Queue Session Setup" in response.text

        response = client.post(
            "/setup",
            data={
                "target_url": "https://authorised-staging.example/path",
                "requested_sessions": "100",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        assert len(runtime.started) == 1
        assert runtime.started[0].requested_sessions == 100
        # Setup delegates once; it does not allocate one task or row per requested visitor.
        assert asyncio.run(repository.recovery_summary(now=datetime.now(UTC))).total_persisted_sessions == 0


@pytest.mark.parametrize(
    ("target_url", "requested_sessions", "message"),
    [
        ("not a URL", "1", "valid absolute HTTP or HTTPS"),
        ("ftp://staging.example.test", "1", "valid absolute HTTP or HTTPS"),
        ("https://user:secret@staging.example.test", "1", "valid absolute HTTP or HTTPS"),
        ("https://staging.example.test", "0", "at least 1"),
        ("https://staging.example.test", "-1", "at least 1"),
        ("https://staging.example.test", "11", "safety limit of 10"),
    ],
)
def test_setup_validation(
    tmp_path: Path,
    target_url: str,
    requested_sessions: str,
    message: str,
) -> None:
    database = tmp_path / f"{abs(hash((target_url, requested_sessions)))}.sqlite3"
    repository = SQLiteSessionRepository(database)
    app = create_app(settings=settings(database, limit=10), repository=repository, runtime=FakeRuntime())

    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": target_url, "requested_sessions": requested_sessions},
        )
        assert response.status_code == 422
        assert message in response.text


def test_persisted_run_survives_restart_skips_setup_and_resumes_only_deficit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.sqlite3"
    first_repository = SQLiteSessionRepository(database)
    first_runtime = FakeRuntime()
    first_app = create_app(
        settings=settings(database), repository=first_repository, runtime=first_runtime
    )
    with TestClient(first_app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/queue", "requested_sessions": "5"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    second_repository = SQLiteSessionRepository(database)
    existing = QueueSession(
        session_id="existing-session",
        queue_id="existing-queue",
        transfer_url="https://secret.invalid/transfer",
        mode=SessionMode.HYBRID,
        state_path=Path("secret-state.json"),
        status=QueueStatus.PRE_QUEUE,
    )
    asyncio.run(second_repository.create(existing))
    asyncio.run(second_repository.close())

    resumed_repository = SQLiteSessionRepository(database)
    resumed_runtime = FakeRuntime()
    resumed_app = create_app(
        settings=settings(database), repository=resumed_repository, runtime=resumed_runtime
    )
    with TestClient(resumed_app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.headers["location"] == "/dashboard"
        assert len(resumed_runtime.started) == 1
        assert resumed_runtime.started[0].requested_sessions == 5
        # The existing bounded controller receives target=5 and authoritatively counts 1,
        # so its restart deficit is 4 rather than a new population of 5.
        assert client.get("/partials/summary").text.find("<dd>4</dd>") != -1


def test_new_run_is_refused_when_legacy_sessions_have_no_target(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "legacy.sqlite3")
    asyncio.run(repository.initialize())
    asyncio.run(
        repository.create(
            QueueSession(
                transfer_url="https://secret.invalid/transfer",
                mode=SessionMode.TRANSFER_ONLY,
                state_path=Path("state.json"),
            )
        )
    )
    run = RunConfig(
        run_id="run",
        target_url="https://different.example.test",
        requested_sessions=1,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(UnownedSessionsError):
        asyncio.run(repository.create_run(run))
    asyncio.run(repository.close())


def test_dashboard_renders_safe_paginated_searchable_partial_population(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dashboard.sqlite3"
    repository = SQLiteSessionRepository(database)
    asyncio.run(repository.initialize())
    run = RunConfig(
        run_id="run-dashboard",
        target_url="https://staging.example.test/queue",
        requested_sessions=100,
        created_at=datetime.now(UTC),
    )
    asyncio.run(repository.create_run(run))
    asyncio.run(
        repository.create(
            QueueSession(
                session_id="visible-session",
                queue_id="visible-queue",
                transfer_url="https://sensitive.example/transfer-token",
                mode=SessionMode.HYBRID,
                state_path=Path("sensitive-browser-state.json"),
                status=QueueStatus.ACTIVE_QUEUE,
            ),
            QueueProgress(session_id="visible-session", progress_percentage=42.5),
        )
    )
    asyncio.run(
        repository.create(
            QueueSession(
                session_id="no-progress-session",
                queue_id="no-progress-queue",
                transfer_url="https://sensitive.example/another-token",
                mode=SessionMode.TRANSFER_ONLY,
                state_path=Path("another-sensitive-state.json"),
                status=QueueStatus.PRE_QUEUE,
            )
        )
    )
    asyncio.run(repository.close())

    app_repository = SQLiteSessionRepository(database)
    app = create_app(
        settings=settings(database), repository=app_repository, runtime=FakeRuntime()
    )
    with TestClient(app) as client:
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "visible-session" in response.text
        assert "visible-queue" in response.text
        assert "ACTIVE_QUEUE" in response.text
        assert "42.5%" in response.text
        assert "no-progress-session" in response.text
        assert "transfer-token" not in response.text
        assert "sensitive-browser-state" not in response.text
        assert 'hx-get="/partials/summary"' in response.text
        assert "every 2s" in response.text

        missing = client.get("/partials/sessions?search=missing")
        assert "No sessions match" in missing.text
        filtered = client.get("/partials/sessions?status=ACTIVE_QUEUE&runtime_state=PARKED")
        assert "visible-session" in filtered.text
        assert client.get("/partials/sessions?status=FAILED").text.find("visible-session") == -1


def test_ten_thousand_rows_return_only_one_database_bounded_page(tmp_path: Path) -> None:
    database = tmp_path / "scale.sqlite3"
    repository = SQLiteSessionRepository(database)
    asyncio.run(repository.initialize())
    asyncio.run(
        repository.create_many(
            (
                QueueSession(
                    session_id=f"session-{index:05d}",
                    queue_id=f"queue-{index:05d}",
                    transfer_url="https://sensitive.invalid/transfer",
                    mode=SessionMode.TRANSFER_ONLY,
                    state_path=Path(f"state-{index}.json"),
                    status=QueueStatus.PARKED,
                ),
                None,
            )
            for index in range(10_000)
        )
    )

    page = asyncio.run(
        repository.list_session_summaries(
            page=100,
            page_size=50,
            runtime_state=BrowserRuntimeState.PARKED,
        )
    )
    assert page.total == 10_000
    assert len(page.items) == 50
    assert page.items[0].session_id == "session-04950"
    asyncio.run(repository.close())
