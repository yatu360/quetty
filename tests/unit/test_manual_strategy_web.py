"""Manual Strategy setup, persistence, restart, and dashboard status."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from test_web_ui import FakeRuntime, settings

from queue_load_test.config import Settings
from queue_load_test.models import BrowserBackendName, MonitoringStrategy, RunConfig
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web import create_app
from queue_load_test.web.manual import ManualAcquisitionState
from queue_load_test.web.service import DashboardService

SETUP = {"target_url": "https://staging.example.test/", "requested_sessions": "2"}


class ManualWindowRuntime(FakeRuntime):
    """A runtime whose Manual Strategy acquisition window is open."""

    def __init__(
        self,
        repository: SQLiteSessionRepository | None = None,
        state: ManualAcquisitionState | None = ManualAcquisitionState.AWAITING_QUEUE_ID,
    ) -> None:
        super().__init__(repository)
        self.manual_acquisition_state = state


def chrome_settings(database: Path, strategy: MonitoringStrategy) -> Settings:
    return settings(database).model_copy(
        update={"browser_backend": BrowserBackendName.CHROME, "monitoring_strategy": strategy}
    )


def test_setup_offers_all_three_strategies_with_the_manual_explanation(tmp_path: Path) -> None:
    database = tmp_path / "setup.sqlite3"
    app = create_app(
        settings=settings(database),
        repository=SQLiteSessionRepository(database),
        runtime=FakeRuntime(),
    )
    with TestClient(app) as client:
        page = client.get("/setup").text

    assert "Headed Window Strategy" in page
    assert "Direct Monitoring Strategy" in page
    assert "Manual Strategy" in page
    assert 'value="manual"' in page
    assert "Opens one visible acquisition window at a time and waits for you to close" in page


def test_manual_selection_persists_manual_and_restart_restores_it(tmp_path: Path) -> None:
    database = tmp_path / "manual.sqlite3"
    first = FakeRuntime()
    with TestClient(
        create_app(
            settings=chrome_settings(database, MonitoringStrategy.HEADED_WINDOW),
            repository=SQLiteSessionRepository(database),
            runtime=first,
        )
    ) as client:
        response = client.post(
            "/setup", data={**SETUP, "monitoring_strategy": "manual"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "Manual Strategy" in client.get("/dashboard").text

    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT monitoring_strategy FROM run_config").fetchone()
    assert stored == ("manual",)
    assert first.started[0].monitoring_strategy is MonitoringStrategy.MANUAL

    # The environment default changes; the persisted run is never migrated.
    restarted = FakeRuntime()
    with TestClient(
        create_app(
            settings=chrome_settings(database, MonitoringStrategy.DIRECT),
            repository=SQLiteSessionRepository(database),
            runtime=restarted,
        )
    ):
        pass
    assert restarted.started[0].monitoring_strategy is MonitoringStrategy.MANUAL


def test_existing_headed_run_is_not_migrated_when_manual_becomes_the_default(
    tmp_path: Path,
) -> None:
    database = tmp_path / "headed.sqlite3"
    with TestClient(
        create_app(
            settings=chrome_settings(database, MonitoringStrategy.HEADED_WINDOW),
            repository=SQLiteSessionRepository(database),
            runtime=FakeRuntime(),
        )
    ) as client:
        client.post("/setup", data={**SETUP, "monitoring_strategy": "headed_window"})

    restarted = FakeRuntime()
    with TestClient(
        create_app(
            settings=chrome_settings(database, MonitoringStrategy.MANUAL),
            repository=SQLiteSessionRepository(database),
            runtime=restarted,
        )
    ) as client:
        # A second setup submission cannot change the persisted strategy.
        response = client.post(
            "/setup", data={**SETUP, "monitoring_strategy": "manual"}, follow_redirects=False
        )
        assert response.headers["location"] == "/dashboard"
    assert [run.monitoring_strategy for run in restarted.started] == [
        MonitoringStrategy.HEADED_WINDOW
    ]
    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT monitoring_strategy FROM run_config").fetchall()
    assert stored == [("headed_window",)]


def test_switching_to_manual_requires_stop_and_reset(tmp_path: Path) -> None:
    database = tmp_path / "reset.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = FakeRuntime(repository)
    app = create_app(
        settings=chrome_settings(database, MonitoringStrategy.HEADED_WINDOW),
        repository=repository,
        runtime=runtime,
    )
    with TestClient(app) as client:
        client.post("/setup", data={**SETUP, "monitoring_strategy": "direct"})
        assert client.post("/run/reset", follow_redirects=False).status_code == 303
        client.post("/setup", data={**SETUP, "monitoring_strategy": "manual"})

    assert [run.monitoring_strategy for run in runtime.started] == [
        MonitoringStrategy.DIRECT,
        MonitoringStrategy.MANUAL,
    ]


async def test_dashboard_shows_waiting_for_manual_close_and_the_window_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "summary.sqlite3"
    repository = SQLiteSessionRepository(database)
    run = RunConfig(
        run_id="manual",
        target_url="https://staging.example.test/",
        requested_sessions=2,
        created_at=datetime.now(UTC),
        monitoring_strategy=MonitoringStrategy.MANUAL,
    )
    await repository.create_run(run)

    waiting = await DashboardService(repository, ManualWindowRuntime(repository)).summary(run)
    restricted = await DashboardService(
        repository,
        ManualWindowRuntime(
            repository, ManualAcquisitionState.ACCESS_RESTRICTED_BEFORE_QUEUE
        ),
    ).summary(run)
    idle = await DashboardService(repository, ManualWindowRuntime(repository, None)).summary(
        run
    )

    assert waiting.creation == "WAITING FOR MANUAL CLOSE"
    assert waiting.manual_window == "AWAITING QUEUE ID"
    assert waiting.monitoring_strategy == "Manual Strategy"
    assert restricted.creation == "WAITING FOR MANUAL CLOSE"
    assert restricted.manual_window == "ACCESS RESTRICTED BEFORE QUEUE"
    assert idle.creation == "RUNNING" and idle.manual_window is None
    await repository.close()


def test_dashboard_partial_renders_the_manual_window_state(tmp_path: Path) -> None:
    database = tmp_path / "partial.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = ManualWindowRuntime(repository, ManualAcquisitionState.QUEUE_ID_ACQUIRED)
    app = create_app(
        settings=chrome_settings(database, MonitoringStrategy.MANUAL),
        repository=repository,
        runtime=runtime,
    )
    with TestClient(app) as client:
        client.post("/setup", data={**SETUP, "monitoring_strategy": "manual"})
        summary = client.get("/partials/summary").text

    assert "WAITING FOR MANUAL CLOSE" in summary
    assert "Manual window" in summary and "QUEUE ID ACQUIRED" in summary
