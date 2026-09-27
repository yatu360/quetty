"""Phase 5 UI reliability: restart, recovery, shutdown, and failure containment.

These tests run the real ``ApplicationRunRuntime`` (bounded creation, scheduler,
operator workers, headed-session manager, repository, and web app). Only Chrome
and Queue-it are replaced: a fake BrowserManager, creator, and restorer stand in
for browser work so every scenario is deterministic and contacts nothing.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from queue_load_test.browser import BrowserCapacity, BrowserProcessCapacity
from queue_load_test.config import Settings
from queue_load_test.models import (
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import CreationOutcome, CreationOutcomeKind, CreationWorkItem
from queue_load_test.transfer import (
    OpenedSessionRestore,
    RestoreMethod,
    SessionRestoreResult,
)
from queue_load_test.utils.instance_lock import InstanceLock, InstanceLockError
from queue_load_test.web import create_app
from queue_load_test.web import service as service_module
from queue_load_test.web.actions import OperatorActionStatus
from queue_load_test.web.service import ApplicationRunRuntime

TARGET = "https://staging.example.test/queue"


# --------------------------------------------------------------------------- fakes


class _Events:
    def __init__(self) -> None:
        self.callbacks: dict[str, list[Callable[[Any], None]]] = {}

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.callbacks.setdefault(event, []).append(callback)

    def emit(self, event: str) -> None:
        for callback in self.callbacks.get(event, []):
            callback(self)


class FakePage(_Events):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed


@dataclass
class World:
    """Everything the fakes observed, shared across one test."""

    created: list[str] = field(default_factory=list)
    restores: list[str] = field(default_factory=list)
    opens: list[str] = field(default_factory=list)
    open_contexts: int = 0
    managers: list[FakeBrowserManager] = field(default_factory=list)
    block_restore: asyncio.Event | None = None
    restore_started: asyncio.Event = field(default_factory=asyncio.Event)
    fail_creation: bool = False
    creator_managers: list[FakeBrowserManager] = field(default_factory=list)
    restorer_managers: list[FakeBrowserManager] = field(default_factory=list)


class FakeOwnedContext:
    def __init__(self, world: World) -> None:
        self.world = world
        self.context = _Events()
        self.closed = False
        self.browser_id = 0
        world.open_contexts += 1

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.world.open_contexts -= 1
        self.context.emit("close")


class FakeBrowserManager:
    def report_navigation(self, context: object, *, responsive: bool) -> None:
        """Navigation health reports are irrelevant to this fake."""

    def __init__(self, world: World, *, headless: bool = True, **_: object) -> None:
        self.world = world
        self.headless = headless
        self.started = False
        self.start_calls = 0
        self.shutdown_calls = 0
        self.repair_requests: list[bool] = []
        self.connected = True
        world.managers.append(self)

    async def start(self) -> None:
        self.started = True
        self.start_calls += 1

    async def shutdown(self) -> None:
        self.started = False
        self.shutdown_calls += 1

    async def capacity(self, *, repair: bool = True) -> BrowserCapacity:
        self.repair_requests.append(repair)
        return BrowserCapacity(
            chrome_processes=int(self.started),
            connected_processes=int(self.started and self.connected),
            active_contexts=0,
            available_contexts=5,
            maximum_active_contexts=5,
            processes=(BrowserProcessCapacity(0, self.connected, 0, 5),),
        )


class FakeCreator:
    def __init__(self, world: World, repository: SQLiteSessionRepository) -> None:
        self.world = world
        self.repository = repository

    async def create(self, item: CreationWorkItem) -> CreationOutcome:
        if self.world.fail_creation:
            return CreationOutcome(
                kind=CreationOutcomeKind.TEMPORARY_FAILURE,
                attempts=1,
                temporary_failures=1,
                duration_seconds=0,
            )
        self.world.created.append(item.session_id)
        session = QueueSession(
            session_id=item.session_id,
            queue_id=f"created-{len(self.world.created):03d}",
            transfer_url=f"{TARGET}?q=created-{len(self.world.created):03d}",
            mode=SessionMode.HYBRID,
            status=QueueStatus.PRE_QUEUE,
            state_path=Path(f"{item.session_id}.json"),
            next_check_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await self.repository.create(session)
        return CreationOutcome(
            kind=CreationOutcomeKind.SUCCESS,
            attempts=1,
            temporary_failures=0,
            duration_seconds=0,
            session=session,
        )


class FakeRestorer:
    def __init__(self, world: World, browser_manager: FakeBrowserManager) -> None:
        self.world = world
        self.browser_manager = browser_manager
        world.restorer_managers.append(browser_manager)

    def _observed(self, session: QueueSession) -> SessionRestoreResult:
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(
                session_id=session.session_id,
                progress_percentage=64.0,
                active_queue=True,
            ),
        )

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        self.world.restores.append(session.session_id)
        self.world.restore_started.set()
        if self.world.block_restore is not None:
            await self.world.block_restore.wait()
        return self._observed(session)

    async def restore_open(self, session: QueueSession) -> OpenedSessionRestore:
        self.world.opens.append(session.session_id)
        owned = FakeOwnedContext(self.world)
        return OpenedSessionRestore(self._observed(session), owned, FakePage())  # type: ignore[arg-type]

    async def inspect_open(self, session: QueueSession, **_: object) -> SessionRestoreResult:
        return self._observed(session)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    state = World()

    class BrowserManagerFactory:
        def __new__(cls, **kwargs: object) -> FakeBrowserManager:  # type: ignore[misc]
            return FakeBrowserManager(state, **kwargs)

        @staticmethod
        def from_settings(_: Settings, **kwargs: object) -> FakeBrowserManager:
            return FakeBrowserManager(state, **kwargs)

    monkeypatch.setattr(service_module, "BrowserManager", BrowserManagerFactory)
    monkeypatch.setattr(
        service_module,
        "QueueSessionCreator",
        lambda **kwargs: (
            state.creator_managers.append(kwargs["browser_manager"]),
            FakeCreator(state, kwargs["repository"]),
        )[1],
    )
    monkeypatch.setattr(
        service_module,
        "QueueSessionRestorer",
        lambda **kwargs: FakeRestorer(state, kwargs["browser_manager"]),
    )
    return state


# ------------------------------------------------------------------------- helpers


def settings_for(database: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": f"sqlite:///{database}",
        "STATE_DIRECTORY": database.parent / "state",
        "CHROME_PROCESS_COUNT": 1,
        "MAX_CONTEXTS_PER_BROWSER": 5,
        "MAX_ACTIVE_CONTEXTS": 5,
        "MAX_MANUAL_OPEN_SESSIONS": 2,
        "OPERATOR_WORKERS": 2,
        "MONITOR_SCHEDULER_TICK_SECONDS": 0.02,
        "MONITOR_RETRY_MAX_ATTEMPTS": 1,
        "SHUTDOWN_TIMEOUT_SECONDS": 2,
        "POLL_JITTER_SECONDS": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def parked(index: int, *, due: bool = False, **fields: Any) -> QueueSession:
    now = datetime.now(UTC)
    return QueueSession(
        session_id=f"persisted-{index}",
        queue_id=f"persisted-queue-{index}",
        transfer_url=f"{TARGET}?q=persisted-queue-{index}",
        mode=SessionMode.HYBRID,
        status=QueueStatus.ACTIVE_QUEUE,
        state_path=Path(f"persisted-{index}.json"),
        created_at=now - timedelta(minutes=10 - index),
        next_check_at=now - timedelta(seconds=1) if due else now + timedelta(hours=1),
        **fields,
    )


async def seed(
    database: Path,
    *,
    requested: int,
    sessions: tuple[QueueSession, ...] = (),
    paused: bool = False,
) -> None:
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    await repository.create_run(
        RunConfig(
            run_id="reliability-run",
            target_url=TARGET,
            requested_sessions=requested,
            created_at=datetime.now(UTC),
        )
    )
    for session in sessions:
        await repository.create(session)
    await repository.set_monitoring_paused(paused)
    await repository.close()


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ui.local") as client:
            yield client


async def eventually(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout: float = 3.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


def queue_ids(database: Path) -> dict[str, str | None]:
    with sqlite3.connect(database) as connection:
        return dict(connection.execute("SELECT session_id, queue_id FROM queue_sessions"))


def ownership_rows(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM queue_sessions "
                "WHERE worker_id IS NOT NULL OR manual_owner_id IS NOT NULL"
            ).fetchone()[0]
        )


def population_adjustment(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return int(
            connection.execute(
                "SELECT operator_population_adjustment FROM runtime_control"
            ).fetchone()[0]
        )


def build(
    database: Path,
    *,
    lock: bool = True,
    **overrides: object,
) -> tuple[FastAPI, SQLiteSessionRepository, ApplicationRunRuntime]:
    settings = settings_for(database, **overrides)
    repository = SQLiteSessionRepository(database)
    runtime = ApplicationRunRuntime(settings=settings, repository=repository)
    app = create_app(
        settings=settings,
        repository=repository,
        runtime=runtime,
        instance_lock=InstanceLock.for_database(database) if lock else None,
    )
    return app, repository, runtime


# ---------------------------------------------------------------- restart / resume


async def test_restart_with_partial_target_resumes_only_deficit(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "partial.sqlite3"
    await seed(database, requested=5, sessions=(parked(0), parked(1)))
    before = queue_ids(database)
    app, repository, _ = build(database)

    async with running(app) as client:
        response = await client.get("/")
        assert response.headers["location"] == "/dashboard"
        assert (await client.get("/setup")).headers["location"] == "/dashboard"
        await eventually(lambda: _count_valid(repository, 5))

    assert len(world.created) == 3
    after = queue_ids(database)
    assert {key: after[key] for key in before} == before
    assert len(after) == 5
    assert ownership_rows(database) == 0


async def test_restart_with_completed_target_creates_nothing(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "complete.sqlite3"
    sessions = tuple(parked(index) for index in range(3))
    await seed(database, requested=3, sessions=sessions)
    app, _, runtime = build(database)

    async with running(app) as client:
        summary = await client.get("/partials/summary")
        assert "COMPLETE" in summary.text
        await asyncio.sleep(0.2)

    assert world.created == []
    assert queue_ids(database) == {s.session_id: s.queue_id for s in sessions}
    assert runtime.error() is None


async def test_restart_while_running_resumes_due_checks(tmp_path: Path, world: World) -> None:
    database = tmp_path / "running.sqlite3"
    await seed(database, requested=1, sessions=(parked(0, due=True),))
    app, repository, _ = build(database)

    async with running(app) as client:
        assert "RUNNING" in (await client.get("/partials/summary")).text
        await eventually(lambda: _progress_is(repository, "persisted-0", 64.0))

    assert world.restores == ["persisted-0"]
    assert queue_ids(database) == {"persisted-0": "persisted-queue-0"}
    assert ownership_rows(database) == 0


async def test_restart_while_paused_keeps_pause_and_still_acquires_deficit(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "paused.sqlite3"
    await seed(database, requested=2, sessions=(parked(0, due=True),), paused=True)
    app, repository, _ = build(database)

    async with running(app) as client:
        summary = await client.get("/partials/summary")
        assert "PAUSED" in summary.text and "Resume Monitoring" in summary.text
        await eventually(lambda: _count_valid(repository, 2))
        await asyncio.sleep(0.15)  # several scheduler ticks
        assert world.restores == []
        assert await repository.is_monitoring_paused()

    assert len(world.created) == 1


async def test_restart_clears_stale_manual_and_lease_ownership_under_instance_lock(
    tmp_path: Path, world: World
) -> None:
    """A SIGKILLed process leaves unexpired owners; the next exclusive start clears them."""

    database = tmp_path / "stale.sqlite3"
    future = datetime.now(UTC) + timedelta(minutes=5)
    await seed(
        database,
        requested=3,
        sessions=(
            parked(0, due=True, manual_owner_id="manual-dead", manual_lease_until=future),
            parked(1, due=True, worker_id="operator-request-dead", lease_until=future),
            parked(2, worker_id="monitor-dead", lease_until=future),
        ),
        paused=True,
    )
    before = queue_ids(database)
    app, repository, _ = build(database)

    async with running(app) as client:
        assert ownership_rows(database) == 0
        page = await client.get("/partials/sessions")
        assert "OPEN IN BROWSER" not in page.text
        assert "CHECKING" not in page.text
        opened = await client.post("/sessions/persisted-0/open", headers={"HX-Request": "true"})
        assert "Opened in browser" in opened.text
        refresh = await client.post("/sessions/persisted-1/refresh")
        assert "Refresh: requested" in refresh.text or "Refresh: running" in refresh.text
        await eventually(lambda: _progress_is(repository, "persisted-1", 64.0))

    assert queue_ids(database) == before
    assert ownership_rows(database) == 0
    assert world.open_contexts == 0


async def test_restart_without_instance_lock_keeps_unexpired_owners(tmp_path: Path) -> None:
    database = tmp_path / "unlocked.sqlite3"
    future = datetime.now(UTC) + timedelta(minutes=5)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await seed(
        database,
        requested=2,
        sessions=(
            parked(0, manual_owner_id="maybe-live", manual_lease_until=future),
            parked(1, manual_owner_id="dead", manual_lease_until=past),
        ),
    )
    repository = SQLiteSessionRepository(database)
    recovery = await repository.recover_startup_ownership(now=datetime.now(UTC), exclusive=False)
    assert (recovery.manual_released, recovery.leases_released) == (1, 0)
    assert (await repository.get("persisted-0")).manual_owner_id == "maybe-live"  # type: ignore[union-attr]
    await repository.close()


def test_second_ui_instance_is_refused_until_first_exits(tmp_path: Path) -> None:
    database = tmp_path / "exclusive.sqlite3"
    first = InstanceLock.for_database(f"sqlite:///{database}")
    second = InstanceLock.for_database(database)
    assert first is not None and second is not None
    assert InstanceLock.for_database("sqlite:///:memory:") is None
    first.acquire()
    with pytest.raises(InstanceLockError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


# ------------------------------------------------------------------------ shutdown


async def test_shutdown_with_manual_browser_open_releases_everything(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "shutdown-open.sqlite3"
    await seed(database, requested=2, sessions=(parked(0), parked(1)))
    before = queue_ids(database)
    app, _, runtime = build(database)

    async with running(app) as client:
        await client.post("/sessions/persisted-0/open")
        await client.post("/sessions/persisted-1/open")
        assert world.open_contexts == 2
        assert ownership_rows(database) == 2

    # Final inspection persisted the latest observation before ownership was released.
    assert _read_progress(database, "persisted-0") == 64.0
    assert ownership_rows(database) == 0
    assert world.open_contexts == 0
    assert queue_ids(database) == before
    headed = [manager for manager in world.managers if not manager.headless]
    assert headed and all(manager.shutdown_calls == 1 for manager in headed)
    automatic = [manager for manager in world.managers if manager.headless]
    assert automatic and all(not manager.started for manager in automatic)
    assert runtime.error() is None


async def test_shutdown_during_operator_work_releases_ownership(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "shutdown-work.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),), paused=True)
    before = queue_ids(database)
    world.block_restore = asyncio.Event()
    app, _, _ = build(database, SHUTDOWN_TIMEOUT_SECONDS=0.2)

    async with running(app) as client:
        await client.post("/sessions/persisted-0/refresh")
        await asyncio.wait_for(world.restore_started.wait(), timeout=2)
        assert ownership_rows(database) == 1
    # The refresh never finished inside the shutdown timeout, so it was cancelled.

    assert ownership_rows(database) == 0
    assert queue_ids(database) == before
    assert _read_progress(database, "persisted-0") is None


async def test_mutations_are_refused_once_shutdown_begins(tmp_path: Path, world: World) -> None:
    database = tmp_path / "refuse.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    app, _, runtime = build(database)

    async with running(app) as client:
        runtime.stop_accepting()
        response = await client.post(
            "/sessions/persisted-0/refresh", headers={"HX-Request": "true"}
        )
        assert "shutting down" in response.text
        opened = await client.post("/sessions/persisted-0/open")
        assert "shutting down" in opened.text
    assert world.restores == [] and world.opens == []


# ------------------------------------------------------------- failure containment


async def test_database_error_during_ui_action_is_contained(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "db-error.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),), paused=True)
    before = queue_ids(database)
    app, repository, runtime = build(database)
    original = repository.acquire_operator_lease
    failures = {"remaining": 1}

    async def flaky(*args: Any, **kwargs: Any) -> QueueSession:
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "acquire_operator_lease", flaky)
    async with running(app) as client:
        failed = await client.post(
            "/sessions/persisted-0/refresh", headers={"HX-Request": "true"}
        )
        assert failed.status_code == 503
        assert failed.headers["HX-Retarget"] == "#action-feedback"
        assert "database temporarily unavailable" in failed.text
        assert "disk I/O" not in failed.text
        assert ownership_rows(database) == 0
        assert runtime.error() is None

        retried = await client.post("/sessions/persisted-0/refresh")
        assert retried.status_code == 200
        await eventually(lambda: _progress_is(repository, "persisted-0", 64.0))

    assert queue_ids(database) == before
    assert ownership_rows(database) == 0


async def test_poll_failure_is_retargeted_and_later_poll_recovers(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "poll-error.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    app, repository, runtime = build(database)
    original = repository.list_session_summaries

    async def broken(**_: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    async with running(app) as client:
        monkeypatch.setattr(repository, "list_session_summaries", broken)
        failed = await client.get("/partials/sessions", headers={"HX-Request": "true"})
        assert failed.status_code == 503
        assert failed.headers["HX-Retarget"] == "#refresh-status"
        monkeypatch.setattr(repository, "list_session_summaries", original)
        recovered = await client.get("/partials/sessions")
        assert recovered.status_code == 200
        assert 'id="refresh-status"' in recovered.text  # clears the failure notice
        assert runtime.error() is None


async def test_template_exception_is_contained_and_state_unchanged(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "template-error.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    before = queue_ids(database)
    app, _, runtime = build(database)

    async with running(app) as client:
        monkeypatch.setattr(runtime, "latest_add_action", lambda: object())
        failed = await client.get("/dashboard")
        assert failed.status_code == 503
        assert "internal error" in failed.text
        monkeypatch.undo()
        assert (await client.get("/dashboard")).status_code == 200
        assert runtime.error() is None

    assert queue_ids(database) == before
    assert ownership_rows(database) == 0


async def test_invalid_filters_are_ignored_not_crashed(tmp_path: Path, world: World) -> None:
    database = tmp_path / "filters.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    app, _, _ = build(database)

    async with running(app) as client:
        response = await client.get("/partials/sessions?status=BOGUS&runtime_state=nope")
        assert response.status_code == 200
        assert "Ignored unknown lifecycle status and browser state filter" in response.text
        assert "persisted-0" in response.text


async def test_polling_is_read_only_and_never_starts_browser_work(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "poll.sqlite3"
    await seed(database, requested=2, sessions=(parked(0), parked(1)), paused=True)
    app, repository, _ = build(database)

    async with running(app) as client:
        await asyncio.sleep(0.05)
        connection = repository._connect()
        changes = connection.total_changes
        tasks = len(asyncio.all_tasks())
        for _ in range(20):
            assert (await client.get("/partials/sessions")).status_code == 200
            assert (await client.get("/partials/summary")).status_code == 200
        assert connection.total_changes == changes
        assert len(asyncio.all_tasks()) <= tasks
        assert world.restores == [] and world.opens == []
        # Dashboard capacity reads never request Chrome repair.
        assert all(
            request is False for manager in world.managers for request in manager.repair_requests
        )


async def test_duplicate_add_submission_with_same_token_creates_one(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "duplicate-add.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    app, repository, _ = build(database)

    async with running(app) as client:
        first, second = await asyncio.gather(
            client.post("/sessions/new?token=render-1"),
            client.post("/sessions/new?token=render-1"),
        )
        assert first.status_code == second.status_code == 200
        await eventually(lambda: _count_valid(repository, 2))
        await asyncio.sleep(0.05)
        repeated = await client.post("/sessions/new?token=render-1")
        assert "New session added" in repeated.text or "Add" in repeated.text

    assert len(world.created) == 1
    assert population_adjustment(database) == 1


async def test_failed_add_leaves_no_orphan_or_population_drift(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "failed-add.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    world.fail_creation = True
    app, _, runtime = build(database)

    async with running(app) as client:
        await client.post("/sessions/new?token=a")
        await eventually(lambda: _add_status(runtime, OperatorActionStatus.FAILED))

    assert queue_ids(database) == {"persisted-0": "persisted-queue-0"}
    assert population_adjustment(database) == 0
    assert ownership_rows(database) == 0


# --------------------------------------------------------------- creation browser


async def test_acquisition_uses_headed_chrome_and_monitoring_stays_headless(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "headed-creation.sqlite3"
    await seed(database, requested=1)
    app, _, _ = build(database)

    async with running(app):
        await eventually(lambda: _started(world.creator_managers))
        (creation,) = world.creator_managers
        assert not creation.headless and creation.start_calls == 1
        automatic = world.restorer_managers[0]
        assert automatic.headless and automatic is not creation

    assert creation.shutdown_calls == 1 and not creation.started


async def test_headless_creation_setting_reuses_the_automatic_pool(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "headless-creation.sqlite3"
    await seed(database, requested=1)
    app, _, _ = build(database, CREATION_HEADLESS=True)

    async with running(app):
        (creation,) = world.creator_managers
        assert creation.headless and creation is world.restorer_managers[0]
    assert len([manager for manager in world.managers if manager.headless]) == 1


# ------------------------------------------------------------------------ run reset


async def test_reset_stops_everything_wipes_data_and_allows_a_fresh_run(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "reset.sqlite3"
    await seed(database, requested=2, sessions=(parked(0), parked(1)), paused=True)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    (state_directory / "persisted-0.json").write_text("{}")
    (state_directory / ".persisted-1.json.abc.tmp").write_text("{}")
    app, repository, runtime = build(database)

    async with running(app) as client:
        await client.post("/sessions/persisted-0/open")
        assert world.open_contexts == 1

        response = await client.post("/run/reset", headers={"HX-Request": "true"})

        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == "/setup"
        assert world.open_contexts == 0
        assert queue_ids(database) == {}
        assert list(state_directory.iterdir()) == []
        assert await repository.get_active_run() is None
        assert not await repository.is_monitoring_paused()
        assert population_adjustment(database) == 0
        assert all(not manager.started for manager in world.managers)
        landing = await client.get("/")
        assert landing.headers["location"] == "/setup"

        setup = await client.post(
            "/setup", data={"target_url": TARGET, "requested_sessions": "1"}
        )
        assert setup.status_code == 303
        fresh = await repository.get_active_run()
        assert fresh is not None and fresh.run_id != "reliability-run"
        assert fresh.requested_sessions == 1
    assert runtime.error() is None


async def test_reset_whose_wipe_fails_restarts_the_existing_run(
    tmp_path: Path, world: World
) -> None:
    database = tmp_path / "reset-fail.sqlite3"
    await seed(database, requested=1, sessions=(parked(0),))
    before = queue_ids(database)
    app, repository, _ = build(database)

    async def fail_reset_all() -> None:
        raise sqlite3.OperationalError("controlled wipe failure")

    repository.reset_all = fail_reset_all  # type: ignore[method-assign]
    async with running(app) as client:
        response = await client.post("/run/reset", headers={"HX-Request": "true"})
        assert "Reset failed; sessions were not deleted and the run was restarted" in (
            response.text
        )
        assert queue_ids(database) == before
        # The restarted runtime serves headed opens again.
        opened = await client.post("/sessions/persisted-0/open")
        assert "Opened in browser" in opened.text
        assert world.open_contexts == 1
    assert ownership_rows(database) == 0


# -------------------------------------------------------------------------- helpers


async def _started(managers: list[FakeBrowserManager]) -> bool:
    return bool(managers) and all(manager.started for manager in managers)


async def _count_valid(repository: SQLiteSessionRepository, expected: int) -> bool:
    return await repository.count_successful_queue_ids() == expected


async def _progress_is(
    repository: SQLiteSessionRepository, session_id: str, expected: float
) -> bool:
    progress = await repository.get_progress(session_id)
    return progress is not None and progress.progress_percentage == expected


async def _add_status(runtime: ApplicationRunRuntime, status: OperatorActionStatus) -> bool:
    action = runtime.latest_add_action()
    return action is not None and action.status is status


def _read_progress(database: Path, session_id: str) -> float | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT progress_percentage FROM queue_progress WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return None if row is None else float(row[0])


@pytest.fixture(autouse=True)
def _passing_camoufox_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never launch a real browser for the new-run preflight."""

    async def ready() -> object:
        return type("Ready", (), {"passed": True, "error": None, "remedy": ""})()

    monkeypatch.setattr("queue_load_test.web.app.run_camoufox_preflight", ready)
