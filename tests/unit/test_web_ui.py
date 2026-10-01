import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from queue_load_test.browser import CAMOUFOX_BROWSER_VERSION
from queue_load_test.browser.preflight import CAMOUFOX_FETCH_COMMAND
from queue_load_test.config import Settings
from queue_load_test.models import (
    BrowserBackendName,
    BrowserRuntimeState,
    MonitoringStrategy,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.repository import SQLiteSessionRepository, UnownedSessionsError
from queue_load_test.web import create_app
from queue_load_test.web.actions import (
    OperatorAction,
    OperatorActionKind,
    OperatorActionStatus,
)
from queue_load_test.web.manual import ManualOpenResult, ManualOpenStatus
from queue_load_test.web.service import (
    AccessRestrictionStatus,
    ApplicationRunRuntime,
    RuntimeCapacity,
    _automatic_monitor_for_strategy,
    _status_discovery_for_run,
    browser_build_label,
)


class FakeRuntime:
    def __init__(self, repository: SQLiteSessionRepository | None = None) -> None:
        self.started: list[RunConfig] = []
        self.closed = 0
        self.repository = repository
        self.pause_calls = 0
        self.resume_calls = 0
        self.open_calls: list[str] = []
        self.close_calls: list[str] = []
        self.actions: list[OperatorAction] = []
        self.tokens: list[str | None] = []
        self.stopped_accepting = False
        self.reset_calls = 0

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

    async def pause_monitoring(self) -> None:
        self.pause_calls += 1
        if self.repository is not None:
            await self.repository.set_monitoring_paused(True)

    async def resume_monitoring(self) -> None:
        self.resume_calls += 1
        if self.repository is not None:
            await self.repository.set_monitoring_paused(False)

    async def open_session(self, session_id: str) -> ManualOpenResult:
        self.open_calls.append(session_id)
        if self.repository is not None:
            now = datetime.now(UTC)
            await self.repository.acquire_manual_ownership(
                session_id,
                owner_id="fake-ui",
                now=now,
                lease_until=now + timedelta(seconds=30),
                capacity=5,
            )
        return ManualOpenResult(ManualOpenStatus.OPENED, "Opened in browser")

    async def close_session(self, session_id: str) -> bool:
        self.close_calls.append(session_id)
        if self.repository is None:
            return False
        return await self.repository.release_manual_ownership(
            session_id,
            owner_id="fake-ui",
        )

    def stop_accepting(self) -> None:
        self.stopped_accepting = True

    async def request_action(
        self,
        kind: OperatorActionKind,
        session_id: str | None = None,
        *,
        request_token: str | None = None,
    ) -> OperatorAction:
        self.tokens.append(request_token)
        action = OperatorAction(
            action_id="fake-action",
            kind=kind,
            status=OperatorActionStatus.REQUESTED,
            session_id=session_id,
            message=f"{kind.value.title()} requested",
        )
        self.actions.append(action)
        return action

    def session_action(self, session_id: str) -> OperatorAction | None:
        return next((item for item in reversed(self.actions) if item.session_id == session_id), None)

    def latest_add_action(self) -> OperatorAction | None:
        return next((item for item in reversed(self.actions) if item.session_id is None), None)

    async def reset(self) -> None:
        self.reset_calls += 1
        if self.repository is not None:
            await self.repository.reset_all()

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
        assert "recorded as ADMITTED" in response.text
        assert "Browser backend: patchright" in response.text
        assert "standard Chrome is the fallback" in response.text

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
        # Phase 7 acceptance: a new run with default settings uses Patchright.
        assert runtime.started[0].browser_backend is BrowserBackendName.PATCHRIGHT
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


def test_summary_reports_aggregate_access_restriction_status(tmp_path: Path) -> None:
    database = tmp_path / "restricted.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime()
    app = create_app(settings=settings(database), repository=repository, runtime=runtime)

    with TestClient(app) as client:
        client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/queue", "requested_sessions": "5"},
            follow_redirects=False,
        )
        before = client.get("/partials/summary").text
        assert "<dt>Access-restricted attempts</dt><dd>0</dd>" in before
        assert '<strong class="state">RUNNING</strong>' in before

        runtime.access_restriction_status = AccessRestrictionStatus(  # type: ignore[attr-defined]
            attempts=3, consecutive=2
        )
        after = client.get("/partials/summary").text
        assert "<dt>Access-restricted attempts</dt><dd>3</dd>" in after
        assert '<strong class="state">BACKING OFF</strong>' in after
        # Successful-ID counts remain persisted unique Queue IDs only.
        assert "<dt>Valid Queue IDs</dt><dd>0</dd>" in after
        assert "sorry" not in after.casefold()


def test_run_runtime_access_restriction_status_is_aggregate_and_safe(tmp_path: Path) -> None:
    database = tmp_path / "status.sqlite3"
    runtime = ApplicationRunRuntime(
        settings=settings(database), repository=SQLiteSessionRepository(database)
    )

    assert runtime.access_restriction_status == AccessRestrictionStatus(0, 0)

    runtime._creator = cast(Any, SimpleNamespace(access_restricted_attempts=4))
    runtime._creation = cast(
        Any, SimpleNamespace(metrics=SimpleNamespace(consecutive_access_restricted=2))
    )

    assert runtime.access_restriction_status == AccessRestrictionStatus(4, 2)


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

    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TRIGGER reject_population_rewrite
        BEFORE UPDATE ON queue_sessions
        BEGIN
            SELECT RAISE(ABORT, 'session population was rewritten');
        END
        """
    )
    connection.commit()
    connection.close()
    control_repository = SQLiteSessionRepository(database)
    assert asyncio.run(control_repository.set_monitoring_paused(True))
    assert asyncio.run(control_repository.set_monitoring_paused(False)) is False
    assert asyncio.run(
        control_repository.recovery_summary(now=datetime.now(UTC))
    ).total_persisted_sessions == 10_000
    asyncio.run(control_repository.close())


def test_dashboard_pause_resume_reflects_persisted_truth_and_keeps_refreshing(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pause-ui.sqlite3"
    repository = SQLiteSessionRepository(database)
    asyncio.run(repository.initialize())
    asyncio.run(
        repository.create_run(
            RunConfig(
                run_id="pause-run",
                target_url="https://staging.example.test/queue",
                requested_sessions=1,
                created_at=datetime.now(UTC),
            )
        )
    )
    asyncio.run(repository.create(QueueSession(
        session_id="due-session",
        queue_id="due-queue",
        transfer_url="https://sensitive.invalid/transfer",
        mode=SessionMode.TRANSFER_ONLY,
        state_path=Path("sensitive-state.json"),
        status=QueueStatus.PARKED,
        next_check_at=datetime.now(UTC),
    )))
    asyncio.run(repository.close())

    app_repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(app_repository)
    app = create_app(
        settings=settings(database),
        repository=app_repository,
        runtime=runtime,
    )
    with TestClient(app) as client:
        running = client.get("/partials/summary")
        assert "RUNNING" in running.text
        assert "Pause Monitoring" in running.text
        assert "Due backlog" in running.text
        assert "<dt>Due backlog</dt><dd>1</dd>" in running.text
        assert "every 2s" in running.text

        paused = client.post("/monitoring/pause")
        assert paused.status_code == 200
        assert "PAUSED" in paused.text
        assert "Resume Monitoring" in paused.text
        assert runtime.pause_calls == 1
        assert len(runtime.started) == 1

        # Persisted truth, not process-local UI state, drives the next refresh.
        assert "PAUSED" in client.get("/partials/summary").text
        resumed = client.post("/monitoring/resume")
        assert "RUNNING" in resumed.text
        assert runtime.resume_calls == 1


def test_dashboard_startup_respects_pause_persisted_before_repository_reopen(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paused-restart.sqlite3"
    repository = SQLiteSessionRepository(database)
    asyncio.run(repository.initialize())
    asyncio.run(
        repository.create_run(
            RunConfig(
                run_id="paused-run",
                target_url="https://staging.example.test/queue",
                requested_sessions=1,
                created_at=datetime.now(UTC),
            )
        )
    )
    asyncio.run(repository.set_monitoring_paused(True))
    asyncio.run(repository.close())

    reopened = SQLiteSessionRepository(database)
    runtime = FakeRuntime(reopened)
    app = create_app(settings=settings(database), repository=reopened, runtime=runtime)
    with TestClient(app) as client:
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "PAUSED" in response.text
        assert "Resume Monitoring" in response.text
        assert len(runtime.started) == 1


def test_dashboard_opens_and_closes_existing_session_in_chrome(tmp_path: Path) -> None:
    database = tmp_path / "manual-open-ui.sqlite3"
    repository = SQLiteSessionRepository(database)
    asyncio.run(repository.initialize())
    asyncio.run(
        repository.create_run(
            RunConfig(
                run_id="manual-run",
                target_url="https://staging.example.test/queue",
                requested_sessions=1,
                created_at=datetime.now(UTC),
            )
        )
    )
    asyncio.run(
        repository.create(
            QueueSession(
                session_id="manual-session",
                queue_id="expected-queue",
                transfer_url="https://sensitive.invalid/transfer",
                mode=SessionMode.HYBRID,
                state_path=Path("sensitive-state.json"),
                status=QueueStatus.ACTIVE_QUEUE,
            )
        )
    )
    asyncio.run(repository.close())

    app_repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(app_repository)
    app = create_app(settings=settings(database), repository=app_repository, runtime=runtime)
    with TestClient(app) as client:
        initial = client.get("/partials/sessions")
        assert ">Open<" in initial.text

        opened = client.post("/sessions/manual-session/open")
        assert opened.status_code == 200
        assert "Opened in browser" in opened.text
        assert "OPEN IN BROWSER" in opened.text
        assert ">Close<" in opened.text
        assert runtime.open_calls == ["manual-session"]

        closed = client.post("/sessions/manual-session/close")
        assert closed.status_code == 200
        assert "Browser session closed" in closed.text
        assert ">Open<" in closed.text
        assert runtime.close_calls == ["manual-session"]


def test_operator_action_routes_render_controls_and_survive_partial_refresh(
    tmp_path: Path,
) -> None:
    database = tmp_path / "operator-routes.sqlite3"
    repository = SQLiteSessionRepository(database)
    asyncio.run(repository.initialize())
    asyncio.run(
        repository.create_run(
            RunConfig(
                run_id="operator-run",
                target_url="https://staging.example.test/queue",
                requested_sessions=1,
                created_at=datetime.now(UTC),
            )
        )
    )
    asyncio.run(repository.create(QueueSession(
        session_id="operator-session",
        queue_id="operator-queue",
        transfer_url="https://sensitive.invalid/transfer",
        mode=SessionMode.HYBRID,
        state_path=Path("sensitive-state.json"),
        status=QueueStatus.PARKED,
    )))
    asyncio.run(repository.close())

    app_repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(app_repository)
    app = create_app(settings=settings(database), repository=app_repository, runtime=runtime)
    with TestClient(app) as client:
        initial = client.get("/partials/sessions")
        assert "Refresh Now" in initial.text
        assert "+ New Session" in initial.text
        assert "hx-confirm" in initial.text

        refresh = client.post("/sessions/operator-session/refresh")
        assert refresh.status_code == 200
        assert "Refresh: requested" in refresh.text
        partial = client.get("/partials/sessions")
        assert "Refresh: requested" in partial.text

        added = client.post("/sessions/new")
        assert added.status_code == 200
        assert "Add requested" in added.text
        assert runtime.actions[-1].kind is OperatorActionKind.ADD


def test_dashboard_reset_wipes_run_and_redirects_to_setup(tmp_path: Path) -> None:
    database = tmp_path / "reset-ui.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(repository)
    app = create_app(settings=settings(database), repository=repository, runtime=runtime)

    with TestClient(app) as client:
        client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/queue", "requested_sessions": "1"},
        )
        dashboard = client.get("/dashboard")
        assert "Stop &amp; Reset Run" in dashboard.text

        response = client.post("/run/reset", headers={"HX-Request": "true"})
        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == "/setup"
        assert runtime.reset_calls == 1
        assert client.get("/", follow_redirects=False).headers["location"] == "/setup"

        # Without a run there is nothing to reset; a plain post just lands on setup.
        again = client.post("/run/reset", follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"] == "/setup"
        assert runtime.reset_calls == 1


def test_failed_dashboard_reset_keeps_run_and_accepts_later_actions(tmp_path: Path) -> None:
    database = tmp_path / "reset-failed.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(repository)
    app = create_app(settings=settings(database), repository=repository, runtime=runtime)

    async def fail_reset() -> None:
        raise RuntimeError("controlled reset failure")

    runtime.reset = fail_reset  # type: ignore[method-assign]
    with TestClient(app) as client:
        client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/queue", "requested_sessions": "1"},
        )

        response = client.post("/run/reset", headers={"HX-Request": "true"})

        assert "Reset failed; sessions were not deleted" in response.text
        assert "controlled reset failure" not in response.text
        assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
        paused = client.post("/monitoring/pause")
        assert paused.status_code == 200
        assert runtime.pause_calls == 1


@pytest.fixture(autouse=True)
def passing_camoufox_preflight(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Unit tests never launch a real browser; setup sees a passing preflight."""

    calls: list[int] = []

    async def ready() -> Any:
        calls.append(1)
        return SimpleNamespace(passed=True, error=None, remedy="")

    monkeypatch.setattr("queue_load_test.web.app.run_camoufox_preflight", ready)
    return calls


@pytest.fixture(autouse=True)
def passing_patchright_preflight(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patchright is the default backend; setup sees a passing preflight, never a browser."""

    calls: list[int] = []

    async def ready() -> Any:
        calls.append(1)
        return SimpleNamespace(
            passed=True, error=None, remedy="", observed_browser_version="153.0.8010.54"
        )

    monkeypatch.setattr("queue_load_test.web.app.run_patchright_preflight", ready)
    return calls


def _run_rows(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return list(connection.execute("SELECT browser_backend, browser_build FROM run_config"))


def test_explicit_camoufox_run_records_pinned_build_after_preflight(
    tmp_path: Path, passing_camoufox_preflight: list[int]
) -> None:
    database = tmp_path / "default.sqlite3"
    runtime = FakeRuntime()
    camoufox_settings = settings(database).model_copy(
        update={"browser_backend": BrowserBackendName.CAMOUFOX}
    )
    app = create_app(
        settings=camoufox_settings,
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert passing_camoufox_preflight == [1]
    assert _run_rows(database) == [("camoufox", CAMOUFOX_BROWSER_VERSION)]
    assert runtime.started[0].browser_backend is BrowserBackendName.CAMOUFOX


def test_failed_camoufox_preflight_creates_no_run_and_explains_the_fix(tmp_path: Path) -> None:
    database = tmp_path / "preflight.sqlite3"
    runtime = FakeRuntime()

    async def missing_build() -> Any:
        return SimpleNamespace(
            passed=False,
            error="BrowserBackendSetupError: missing",
            remedy=f"Install the pinned Camoufox browser with: {CAMOUFOX_FETCH_COMMAND}",
        )

    app = create_app(
        settings=settings(database).model_copy(
            update={"browser_backend": BrowserBackendName.CAMOUFOX}
        ),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
        camoufox_preflight=missing_build,
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 503
    assert CAMOUFOX_FETCH_COMMAND in response.text
    assert "BROWSER_BACKEND=chrome" in response.text
    assert _run_rows(database) == []
    assert runtime.started == []


def test_patchright_preflight_records_backend_and_observed_browser_build(tmp_path: Path) -> None:
    database = tmp_path / "patchright.sqlite3"
    runtime = FakeRuntime()
    calls: list[int] = []

    async def ready() -> Any:
        calls.append(1)
        return SimpleNamespace(
            passed=True,
            error=None,
            remedy="",
            observed_browser_version="153.0.8010.54",
        )

    app = create_app(
        settings=settings(database).model_copy(
            update={"browser_backend": BrowserBackendName.PATCHRIGHT}
        ),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
        patchright_preflight=ready,
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert calls == [1]
    assert _run_rows(database) == [("patchright", "153.0.8010.54")]
    assert runtime.started[0].browser_backend is BrowserBackendName.PATCHRIGHT


def test_failed_patchright_preflight_persists_nothing_and_is_actionable(tmp_path: Path) -> None:
    database = tmp_path / "patchright-failed.sqlite3"
    runtime = FakeRuntime()

    async def unavailable() -> Any:
        return SimpleNamespace(
            passed=False,
            error="Error: executable does not exist",
            remedy="Install Google Chrome with python -m patchright install chrome.",
            observed_browser_version=None,
        )

    app = create_app(
        settings=settings(database).model_copy(
            update={"browser_backend": BrowserBackendName.PATCHRIGHT}
        ),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
        patchright_preflight=unavailable,
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 503
    assert "python -m patchright install chrome" in response.text
    assert "BROWSER_BACKEND=chrome" in response.text
    assert _run_rows(database) == []
    assert runtime.started == []


def test_new_run_defaults_to_patchright_and_records_observed_build(
    tmp_path: Path, passing_patchright_preflight: list[int]
) -> None:
    database = tmp_path / "default.sqlite3"
    runtime = FakeRuntime()

    async def must_not_run() -> Any:
        raise AssertionError("Patchright runs never run the Camoufox preflight")

    assert settings(database).browser_backend is BrowserBackendName.PATCHRIGHT
    app = create_app(
        settings=settings(database),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
        camoufox_preflight=must_not_run,
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert passing_patchright_preflight == [1]
    assert _run_rows(database) == [("patchright", "153.0.8010.54")]


def test_explicit_chrome_fallback_skips_backend_preflights_and_records_no_build(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chrome.sqlite3"
    runtime = FakeRuntime()

    async def must_not_run() -> Any:
        raise AssertionError("Chrome runs never run a Camoufox or Patchright preflight")

    app = create_app(
        settings=settings(database).model_copy(
            update={"browser_backend": BrowserBackendName.CHROME}
        ),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
        camoufox_preflight=must_not_run,
        patchright_preflight=must_not_run,
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert _run_rows(database) == [("chrome", None)]
    assert runtime.started[0].browser_backend is BrowserBackendName.CHROME


async def test_existing_chrome_run_restarts_as_chrome_after_patchright_default(
    tmp_path: Path, passing_patchright_preflight: list[int]
) -> None:
    database = tmp_path / "existing-chrome.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(
        RunConfig(
            run_id="chrome-run",
            target_url="https://staging.example.test/",
            requested_sessions=1,
            created_at=datetime.now(UTC),
            browser_backend=BrowserBackendName.CHROME,
        )
    )
    await repository.close()
    runtime = FakeRuntime()
    app = create_app(
        settings=settings(database), repository=SQLiteSessionRepository(database), runtime=runtime
    )
    assert settings(database).browser_backend is BrowserBackendName.PATCHRIGHT

    with TestClient(app) as client:
        summary = client.get("/partials/summary").text

    assert [run.browser_backend for run in runtime.started] == [BrowserBackendName.CHROME]
    assert passing_patchright_preflight == []
    assert "installed Google Chrome" in summary
    assert _run_rows(database) == [("chrome", None)]


async def test_existing_camoufox_run_restarts_as_camoufox_after_default_change(
    tmp_path: Path, passing_camoufox_preflight: list[int]
) -> None:
    database = tmp_path / "existing.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(
        RunConfig(
            run_id="camoufox-run",
            target_url="https://staging.example.test/",
            requested_sessions=1,
            created_at=datetime.now(UTC),
            browser_backend=BrowserBackendName.CAMOUFOX,
            browser_build=CAMOUFOX_BROWSER_VERSION,
        )
    )
    await repository.close()
    runtime = FakeRuntime()
    app = create_app(
        settings=settings(database), repository=SQLiteSessionRepository(database), runtime=runtime
    )
    assert settings(database).browser_backend is BrowserBackendName.PATCHRIGHT

    with TestClient(app) as client:
        summary = client.get("/partials/summary").text

    assert [run.browser_backend for run in runtime.started] == [BrowserBackendName.CAMOUFOX]
    assert passing_camoufox_preflight == []
    assert CAMOUFOX_BROWSER_VERSION in summary
    assert _run_rows(database) == [("camoufox", CAMOUFOX_BROWSER_VERSION)]


async def test_existing_patchright_run_restarts_without_preflight_or_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "existing-patchright.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(
        RunConfig(
            run_id="patchright-run",
            target_url="https://staging.example.test/",
            requested_sessions=1,
            created_at=datetime.now(UTC),
            browser_backend=BrowserBackendName.PATCHRIGHT,
            browser_build="153.0.8010.54",
        )
    )
    await repository.close()
    runtime = FakeRuntime()

    async def must_not_run() -> Any:
        raise AssertionError("existing runs never rerun new-run preflight")

    app = create_app(
        settings=settings(database),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
        patchright_preflight=must_not_run,
    )
    with TestClient(app) as client:
        summary = client.get("/partials/summary").text

    assert [run.browser_backend for run in runtime.started] == [BrowserBackendName.PATCHRIGHT]
    assert "153.0.8010.54 via Patchright" in summary
    assert _run_rows(database) == [("patchright", "153.0.8010.54")]


def test_browser_build_label_never_hides_a_changed_pinned_build() -> None:
    def run(backend: BrowserBackendName, build: str | None) -> RunConfig:
        return RunConfig(
            run_id="r",
            target_url="https://staging.example.test/",
            requested_sessions=1,
            created_at=datetime.now(UTC),
            browser_backend=backend,
            browser_build=build,
        )

    assert browser_build_label(run(BrowserBackendName.CHROME, None)) == "installed Google Chrome"
    assert (
        browser_build_label(run(BrowserBackendName.CAMOUFOX, CAMOUFOX_BROWSER_VERSION))
        == CAMOUFOX_BROWSER_VERSION
    )
    assert "run created with 1.0-old" in browser_build_label(
        run(BrowserBackendName.CAMOUFOX, "1.0-old")
    )
    assert "not recorded" in browser_build_label(run(BrowserBackendName.CAMOUFOX, None))
    assert "153.0 via Patchright" == browser_build_label(
        run(BrowserBackendName.PATCHRIGHT, "153.0")
    )


@pytest.mark.parametrize("strategy", tuple(MonitoringStrategy))
@pytest.mark.parametrize("backend", tuple(BrowserBackendName))
def test_setup_persists_each_monitoring_strategy_independently_of_browser_backend(
    tmp_path: Path,
    strategy: MonitoringStrategy,
    backend: BrowserBackendName,
) -> None:
    database = tmp_path / f"{strategy.value}-{backend.value}.sqlite3"
    runtime = FakeRuntime()
    configured = settings(database).model_copy(update={"browser_backend": backend})
    app = create_app(
        settings=configured,
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
    )

    with TestClient(app) as client:
        setup = client.get("/setup")
        assert "Headed Window Strategy" in setup.text
        assert "Direct Monitoring Strategy" in setup.text
        assert "do not enter the Queue-it" in setup.text
        response = client.post(
            "/setup",
            data={
                "target_url": "https://staging.example.test/",
                "requested_sessions": "2",
                "monitoring_strategy": strategy.value,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        dashboard = client.get("/dashboard")
        assert strategy.label in dashboard.text

    assert len(runtime.started) == 1
    assert runtime.started[0].monitoring_strategy is strategy
    assert runtime.started[0].browser_backend is backend
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT monitoring_strategy, browser_backend FROM run_config"
        ).fetchone()
    assert row == (strategy.value, backend.value)


def test_restart_uses_persisted_strategy_when_environment_default_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "strategy-restart.sqlite3"
    first_runtime = FakeRuntime()
    first_settings = settings(database).model_copy(
        update={
            "browser_backend": BrowserBackendName.CHROME,
            "monitoring_strategy": MonitoringStrategy.HEADED_WINDOW,
        }
    )
    with TestClient(
        create_app(
            settings=first_settings,
            repository=SQLiteSessionRepository(database),
            runtime=first_runtime,
        )
    ) as client:
        assert client.post(
            "/setup",
            data={
                "target_url": "https://staging.example.test/",
                "requested_sessions": "1",
                "monitoring_strategy": "direct",
            },
            follow_redirects=False,
        ).status_code == 303

    restarted_runtime = FakeRuntime()
    changed_defaults = settings(database).model_copy(
        update={
            "browser_backend": BrowserBackendName.PATCHRIGHT,
            "monitoring_strategy": MonitoringStrategy.HEADED_WINDOW,
        }
    )
    with TestClient(
        create_app(
            settings=changed_defaults,
            repository=SQLiteSessionRepository(database),
            runtime=restarted_runtime,
        )
    ):
        pass

    assert len(restarted_runtime.started) == 1
    assert restarted_runtime.started[0].monitoring_strategy is MonitoringStrategy.DIRECT
    assert restarted_runtime.started[0].browser_backend is BrowserBackendName.CHROME


def test_stop_and_reset_allows_a_different_monitoring_strategy(tmp_path: Path) -> None:
    database = tmp_path / "strategy-reset.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(repository)
    app = create_app(
        settings=settings(database).model_copy(
            update={"browser_backend": BrowserBackendName.CHROME}
        ),
        repository=repository,
        runtime=runtime,
    )
    setup_base = {
        "target_url": "https://staging.example.test/",
        "requested_sessions": "1",
    }

    with TestClient(app) as client:
        assert client.post(
            "/setup",
            data={**setup_base, "monitoring_strategy": "headed_window"},
            follow_redirects=False,
        ).status_code == 303
        assert client.post("/run/reset", follow_redirects=False).status_code == 303
        assert client.post(
            "/setup",
            data={**setup_base, "monitoring_strategy": "direct"},
            follow_redirects=False,
        ).status_code == 303

    assert [run.monitoring_strategy for run in runtime.started] == [
        MonitoringStrategy.HEADED_WINDOW,
        MonitoringStrategy.DIRECT,
    ]


@pytest.mark.parametrize("strategy", tuple(MonitoringStrategy))
def test_manual_open_and_pause_resume_remain_available_for_each_strategy(
    tmp_path: Path,
    strategy: MonitoringStrategy,
) -> None:
    database = tmp_path / f"controls-{strategy.value}.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(repository)
    app = create_app(
        settings=settings(database).model_copy(
            update={"browser_backend": BrowserBackendName.CHROME}
        ),
        repository=repository,
        runtime=runtime,
    )

    with TestClient(app) as client:
        assert client.post(
            "/setup",
            data={
                "target_url": "https://staging.example.test/",
                "requested_sessions": "1",
                "monitoring_strategy": strategy.value,
            },
            follow_redirects=False,
        ).status_code == 303
        asyncio.run(
            repository.create(
                QueueSession(
                    session_id="session-1",
                    queue_id="queue-1",
                    transfer_url="https://secret.invalid/transfer",
                    mode=SessionMode.HYBRID,
                    status=QueueStatus.PARKED,
                    state_path=Path("state.json"),
                )
            )
        )
        assert client.post("/sessions/session-1/open").status_code == 200
        assert client.post("/monitoring/pause").status_code == 200
        assert asyncio.run(repository.is_monitoring_paused())
        assert client.post("/monitoring/resume").status_code == 200
        assert not asyncio.run(repository.is_monitoring_paused())

    assert runtime.open_calls == ["session-1"]
    assert runtime.pause_calls == 1
    assert runtime.resume_calls == 1


def test_setup_rejects_unknown_monitoring_strategy_without_persisting(tmp_path: Path) -> None:
    database = tmp_path / "unknown-strategy.sqlite3"
    runtime = FakeRuntime()
    app = create_app(
        settings=settings(database),
        repository=SQLiteSessionRepository(database),
        runtime=runtime,
    )

    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={
                "target_url": "https://staging.example.test/",
                "requested_sessions": "1",
                "monitoring_strategy": "unknown",
            },
        )

    assert response.status_code == 422
    assert "valid monitoring strategy" in response.text
    assert runtime.started == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_config").fetchone()[0] == 0


def test_phase8_strategy_boundary_routes_each_strategy_to_its_handler() -> None:
    browser_monitor = cast(Any, object())
    direct_handler = cast(Any, object())

    assert (
        _automatic_monitor_for_strategy(
            MonitoringStrategy.HEADED_WINDOW,
            browser_monitor=browser_monitor,
            direct_handler=direct_handler,
        )
        is browser_monitor
    )
    assert (
        _automatic_monitor_for_strategy(
            MonitoringStrategy.DIRECT,
            browser_monitor=browser_monitor,
            direct_handler=direct_handler,
        )
        is direct_handler
    )
    with pytest.raises(ValueError, match="requires its direct handler"):
        _automatic_monitor_for_strategy(
            MonitoringStrategy.DIRECT, browser_monitor=browser_monitor
        )


def test_status_discovery_is_opt_in_for_direct_runs_and_never_changes_headed_runs(
    tmp_path: Path,
) -> None:
    enabled = settings(tmp_path / "discovery.sqlite3").model_copy(
        update={
            "status_discovery_enabled": True,
            "status_discovery_directory": tmp_path / "evidence",
        }
    )
    headed = RunConfig(
        run_id="headed",
        target_url="https://staging.example.test/",
        requested_sessions=1,
        created_at=datetime.now(UTC),
        monitoring_strategy=MonitoringStrategy.HEADED_WINDOW,
    )
    direct = RunConfig(
        run_id="direct",
        target_url="https://staging.example.test/",
        requested_sessions=1,
        created_at=datetime.now(UTC),
        monitoring_strategy=MonitoringStrategy.DIRECT,
    )

    assert _status_discovery_for_run(headed, enabled) is None
    assert _status_discovery_for_run(direct, enabled) is not None
    assert (
        _status_discovery_for_run(
            direct,
            enabled.model_copy(update={"status_discovery_enabled": False}),
        )
        is None
    )
