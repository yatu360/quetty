"""Manual Strategy end to end with a real local browser and the local simulator.

The simulator page exposes only a transfer identity (no pre-queue marker, progress,
position, or lifecycle text), so a persisted session must be PRE_QUEUE. Closing the
page from the test is exactly the browser event an operator closing the window
produces; the application itself never closes a healthy acquisition window. The
window runs headless here only so CI needs no display; production wiring is headed
(``tests/unit/test_manual_strategy.py``). No Queue-it traffic.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.config import Settings
from queue_load_test.harness.local_queue_simulator import (
    STAGE_IDENTITY_ONLY,
    LocalQueueSimulator,
)
from queue_load_test.models import BrowserBackendName, MonitoringStrategy, QueueStatus, RunConfig
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web.manual import ManualAcquisitionState
from queue_load_test.web.service import ApplicationRunRuntime

BACKENDS = (BrowserBackendName.PATCHRIGHT, BrowserBackendName.CHROME)


def settings(tmp_path: Path, backend: BrowserBackendName) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite:///{tmp_path / 'manual.sqlite3'}",
        STATE_DIRECTORY=str(tmp_path / "state"),
        DIRECT_MONITOR_DIRECTORY=str(tmp_path / "direct"),
        SESSION_MODE="HYBRID",
        BROWSER_BACKEND=backend.value,
        CHROME_PROCESS_COUNT=1,
        MAX_CONTEXTS_PER_BROWSER=5,
        MAX_ACTIVE_CONTEXTS=5,
        MAX_MANUAL_OPEN_SESSIONS=2,
        MANUAL_OPEN_LEASE_SECONDS=6,
        # Deliberately more than one: Manual Strategy must still be sequential.
        CREATION_WORKERS=3,
        CREATION_QUEUE_CAPACITY=3,
        MONITOR_WORKERS=1,
        MONITOR_SCHEDULER_TICK_SECONDS=0.2,
        SHUTDOWN_TIMEOUT_SECONDS=10,
        HEADLESS=True,
        CREATION_HEADLESS=True,
    )


async def until(predicate: Callable[[], Any], timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.1)


async def pre_queue(repository: SQLiteSessionRepository, session_id: str) -> bool:
    row = await repository.get(session_id)
    return row is not None and row.queue_id is not None and row.status is QueueStatus.PRE_QUEUE


@pytest.mark.parametrize("backend", BACKENDS)
async def test_manual_strategy_two_sequential_operator_closed_windows(
    tmp_path: Path, backend: BrowserBackendName
) -> None:
    simulator = LocalQueueSimulator(initial_stage=STAGE_IDENTITY_ONLY)
    await simulator.start()
    repository = SQLiteSessionRepository(tmp_path / "manual.sqlite3")
    runtime = ApplicationRunRuntime(
        settings=settings(tmp_path, backend), repository=repository, manual_headless=True
    )
    run = RunConfig(
        run_id="manual-integration",
        target_url=simulator.entry_url,
        requested_sessions=2,
        created_at=datetime.now(UTC),
        browser_backend=backend,
        monitoring_strategy=MonitoringStrategy.MANUAL,
    )
    await repository.create_run(run)
    try:
        await runtime.start_run(run)
        manual: Any = runtime._manual_sessions

        # Window 1 opens and its Queue ID is persisted while it stays open.
        await until(lambda: manual._acquisition_record is not None)
        first = manual._acquisition_record
        await until(
            lambda: runtime.manual_acquisition_state is ManualAcquisitionState.QUEUE_ID_ACQUIRED
        )
        await until(lambda: pre_queue(repository, first.session_id))
        first_row = await repository.get(first.session_id)
        assert first_row is not None and first_row.queue_id is not None
        assert first_row.status is QueueStatus.PRE_QUEUE
        assert first_row.manual_owner_id is not None
        first_queue_id = first_row.queue_id

        # The application does not advance while window 1 stays open.
        await asyncio.sleep(2.0)
        assert manual._acquisition_record is first and not first.page.is_closed()
        assert len(await repository.list()) == 1
        assert manual.open_count == 1

        # The operator closes window 1: window 2 opens.
        await first.page.close()
        await until(
            lambda: manual._acquisition_record is not None
            and manual._acquisition_record is not first
        )
        second = manual._acquisition_record
        kept = await repository.get(first.session_id)
        assert kept is not None and kept.queue_id == first_queue_id
        assert kept.status is QueueStatus.PRE_QUEUE and kept.manual_owner_id is None

        await until(
            lambda: runtime.manual_acquisition_state is ManualAcquisitionState.QUEUE_ID_ACQUIRED
        )
        await until(lambda: pre_queue(repository, second.session_id))
        # Closing window 2 completes the target of two.
        await second.page.close()
        creation: Any = runtime._creation
        await until(lambda: creation.metrics.successful_unique_ids == 2)
        await until(lambda: manual._acquisition_record is None)
        await asyncio.sleep(0.5)
        assert manual._acquisition_record is None  # no third window
        rows = await repository.list()
        assert len(rows) == 2
        assert {row.status for row in rows} == {QueueStatus.PRE_QUEUE}
        assert len({row.queue_id for row in rows}) == 2
        assert creation.metrics.maximum_concurrent_creating == 1
        assert await repository.count_successful_queue_ids() == 2
    finally:
        await runtime.close()
        shared: Any = runtime._shared_capacity
        headed: Any = runtime._headed_browser_manager
        automatic: Any = runtime._browser_manager
        assert shared is not None and shared.active == 0
        assert headed is not None and not headed.started
        assert automatic is not None and not automatic.started
        assert runtime._manual_sessions is not None and runtime._manual_sessions.open_count == 0
        remaining = await SQLiteSessionRepository(tmp_path / "manual.sqlite3").list()
        assert all(
            row.manual_owner_id is None and row.worker_id is None for row in remaining
        )
        await simulator.close()
