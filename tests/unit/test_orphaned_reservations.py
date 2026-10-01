"""Startup discards IPRoyal creation reservations left by an interrupted creator."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from queue_load_test.config import Settings
from queue_load_test.models import (
    BrowserBackendName,
    ProxyProvider,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.proxy import IPRoyalCredentials, SessionProxyResolver
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import CreationWorkItem, QueueSessionCreator
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker
from queue_load_test.web import service as service_module
from queue_load_test.web.service import ApplicationRunRuntime

NOW = datetime(2026, 10, 1, tzinfo=UTC)
CREDENTIALS = IPRoyalCredentials(
    server="http://proxy.fake-iproyal.test:12321", username="u", base_password="p"
)


def run() -> RunConfig:
    return RunConfig(
        run_id="orphans",
        target_url="https://staging.example.test/",
        requested_sessions=2,
        created_at=NOW,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_provider=ProxyProvider.IPROYAL,
        proxy_country="gb",
        proxy_lifetime="2h",
    )


def row(session_id: str, **overrides: Any) -> QueueSession:
    values: dict[str, Any] = {
        "session_id": session_id,
        "queue_id": None,
        "transfer_url": "",
        "mode": SessionMode.HYBRID,
        "browser_backend": BrowserBackendName.PATCHRIGHT,
        "proxy_session_id": f"{session_id[:4]:0<4}Ab12"[:8],
        "status": QueueStatus.CREATING,
        "state_path": Path(f".browser-state/{session_id}.json"),
    }
    values.update(overrides)
    return QueueSession(**values)


async def test_only_unowned_identity_less_creating_rows_are_discarded(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "orphans.sqlite3")
    await repository.create(row("orph"), QueueProgress(session_id="orph", active_queue=True))
    # Manual adoption: CREATING but it already carries the adopted Queue ID.
    await repository.create(row("adpt", queue_id="queue-adopted"))
    await repository.create(row("plst", status=QueueStatus.PARKED, queue_id="queue-parked"))
    await repository.create(row("fail", status=QueueStatus.FAILED))
    await repository.create(row("lsed"))
    await repository.create(row("manl"))
    connection = repository._connect()
    lease = (NOW + timedelta(hours=1)).isoformat()
    connection.execute(
        "UPDATE queue_sessions SET worker_id = 'creator', lease_until = ? "
        "WHERE session_id = 'lsed'",
        (lease,),
    )
    connection.execute(
        "UPDATE queue_sessions SET manual_owner_id = 'manual', manual_lease_until = ? "
        "WHERE session_id = 'manl'",
        (lease,),
    )
    connection.commit()

    discarded = await repository.discard_orphaned_reservations()

    assert discarded == ("orph",)
    assert {item.session_id for item in await repository.list()} == {
        "adpt", "plst", "fail", "lsed", "manl"
    }
    assert await repository.get_progress("orph") is None  # cascaded
    assert await repository.discard_orphaned_reservations() == ()
    await repository.close()


class _HangingManager:
    def __init__(self) -> None:
        self.navigating = asyncio.Event()

    def report_navigation(self, *_: Any, **__: Any) -> None:
        return None

    @asynccontextmanager
    async def context(self, **_: Any) -> Any:
        manager = self

        class Page:
            url = "https://staging.example.test/"

            async def goto(self, *_: Any, **__: Any) -> Any:
                manager.navigating.set()
                await asyncio.Event().wait()

        class Context:
            async def new_page(self) -> Page:
                return Page()

        yield Context()


async def test_startup_discards_a_reservation_left_by_an_interrupted_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    database = tmp_path / "crash.sqlite3"
    state_directory = tmp_path / "state"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(run())
    await repository.adjust_operator_population(1)  # an operator Add was in flight
    manager = _HangingManager()
    creator = QueueSessionCreator(
        browser_manager=cast(Any, manager),
        repository=repository,
        state_store=FileSystemStateStore(state_directory),
        staging_url="https://staging.example.test/",
        state_directory=state_directory,
        mode=SessionMode.HYBRID,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=SessionProxyResolver(run(), CREDENTIALS),
    )
    task = asyncio.create_task(creator.create(CreationWorkItem(sequence=1, session_id="lost")))
    await asyncio.wait_for(manager.navigating.wait(), timeout=5)
    task.cancel()  # the process stops mid-acquisition
    with pytest.raises(asyncio.CancelledError):
        await task
    orphan = await repository.get("lost")
    assert orphan is not None and orphan.status is QueueStatus.CREATING
    assert orphan.queue_id is None and orphan.proxy_session_id is not None
    # A state file written just before the crash is cleaned up with the row.
    await FileSystemStateStore(state_directory).save("lost", {"cookies": [], "origins": []})
    await repository.close()

    async def no_run(self: object) -> None:
        return None

    monkeypatch.setattr(service_module.ApplicationRuntime, "run", no_run)
    restarted = SQLiteSessionRepository(database)
    runtime = ApplicationRunRuntime(
        settings=Settings(
            _env_file=None,
            DATABASE_URL=f"sqlite:///{database}",
            STATE_DIRECTORY=str(state_directory),
            DIRECT_MONITOR_DIRECTORY=str(tmp_path / "direct"),
            CHROME_PROCESS_COUNT=1,
            MAX_CONTEXTS_PER_BROWSER=5,
            MAX_ACTIVE_CONTEXTS=5,
            IPROYAL_PROXY_SERVER=CREDENTIALS.server,
            IPROYAL_PROXY_USERNAME="u",
            IPROYAL_PROXY_PASSWORD="p",
        ),
        repository=restarted,
    )
    await runtime.start_run(run())
    try:
        assert await restarted.get("lost") is None
        assert not (state_directory / "lost.json").exists()
        # The +1 the operator asked for stays, so the deficit refill re-creates it.
        assert await restarted.get_operator_population_adjustment() == 1
        report = await StateConsistencyChecker(
            restarted, FileSystemStateStore(state_directory)
        ).check()
        assert report.is_consistent
        contexts = [getattr(record, "observability_context", {}) for record in caplog.records]
        assert {"count": 1} in contexts
    finally:
        await runtime.close()
        await restarted.close()
