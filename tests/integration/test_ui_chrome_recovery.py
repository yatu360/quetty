"""Installed-Chrome recovery for operator UI browser work (local simulator only).

Chrome is real (``channel="chrome"``, headless) and is SIGKILLed mid-session; the
Queue-it-like pages come from ``LocalQueueSimulator`` on 127.0.0.1. No staging
environment is contacted and every Queue ID is a synthetic local value.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringRetryPolicy, PollingPolicy, QueueSessionMonitor
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)
from queue_load_test.web.manual import ManualChromeSessionManager

psutil = pytest.importorskip("psutil")


def _chrome_main_processes() -> list[Any]:
    found = []
    for process in psutil.Process(os.getpid()).children(recursive=True):
        try:
            arguments = process.cmdline()
        except psutil.Error:
            continue
        if "--remote-debugging-pipe" in arguments and not any(
            argument.startswith("--type=") for argument in arguments
        ):
            found.append(process)
    return found


class _SaveFails(FileSystemStateStore):
    async def save(self, session_id: str, state: Any) -> Path:
        raise OSError("state volume unavailable")


def _stack(
    tmp_path: Path,
    simulator: LocalQueueSimulator,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
) -> tuple[BrowserManager, QueueSessionRestorer, QueueSessionMonitor]:
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=2,
        max_active_contexts=2,
        headless=True,
    )
    restorer = QueueSessionRestorer(
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
        expected_journey_url=simulator.queue_url,
        storage_navigation_url=simulator.queue_url,
        admission_detector=AdmissionDetector.from_urls(simulator.protected_url),
        navigation_timeout_ms=3_000,
        admission_wait_timeout_ms=0,
        observation_timeout_seconds=3.0,
        observation_interval_seconds=0.1,
    )
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=MonitoringRetryPolicy(max_attempts=1),
    )
    return manager, restorer, monitor


def _session(simulator: LocalQueueSimulator, queue_id: str, mode: SessionMode) -> QueueSession:
    return QueueSession(
        session_id=f"session-{queue_id}",
        queue_id=queue_id,
        transfer_url=simulator.transfer_url(queue_id),
        mode=mode,
        status=QueueStatus.ACTIVE_QUEUE,
        state_path=Path(f"{queue_id}.json"),
    )


async def _until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.05)


async def test_chrome_kill_while_open_releases_ownership_and_reopens(tmp_path: Path) -> None:
    simulator = LocalQueueSimulator()
    await simulator.start()
    repository = SQLiteSessionRepository(tmp_path / "chrome.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    browser, restorer, monitor = _stack(tmp_path, simulator, repository, state_store)
    item = _session(simulator, "sim-ui-1", SessionMode.TRANSFER_ONLY)
    await repository.create(item)
    manual = ManualChromeSessionManager(
        repository=repository,
        browser_manager=browser,
        restorer=restorer,
        monitor=monitor,
        capacity=2,
        lease_seconds=3.3,
    )
    try:
        opened = await manual.open(item.session_id)
        assert opened.message == "Opened in Chrome"
        owned = await repository.get(item.session_id)
        assert owned is not None and owned.manual_owner_id is not None
        assert (await browser.capacity(repair=False)).active_contexts == 1

        processes = _chrome_main_processes()
        assert len(processes) == 1
        processes[0].kill()
        await _until(lambda: manual.open_count == 0)

        released = await repository.get(item.session_id)
        assert released is not None
        assert released.manual_owner_id is None
        assert released.queue_id == "sim-ui-1"
        assert released.status is not QueueStatus.FAILED
        assert (await browser.capacity(repair=False)).active_contexts == 0

        # The next explicit Open relaunches the headed pool and restores the same identity.
        reopened = await manual.open(item.session_id)
        assert reopened.message == "Opened in Chrome"
        assert len(_chrome_main_processes()) == 1
    finally:
        await manual.close()  # app shutdown with the window open
        final = await repository.get(item.session_id)
        assert final is not None and final.manual_owner_id is None
        assert final.queue_id == "sim-ui-1"
        assert _chrome_main_processes() == []
        await repository.close()
        await simulator.close()


async def test_refresh_with_failing_state_save_keeps_identity_and_no_context_leak(
    tmp_path: Path,
) -> None:
    simulator = LocalQueueSimulator()
    await simulator.start()
    repository = SQLiteSessionRepository(tmp_path / "refresh.sqlite3")
    await repository.initialize()
    state_store = _SaveFails(tmp_path / "state")
    browser, _, monitor = _stack(tmp_path, simulator, repository, state_store)
    item = _session(simulator, "sim-ui-2", SessionMode.HYBRID)
    simulator.progress["sim-ui-2"] = 73
    await repository.create(item)
    await browser.start()

    class _NoCreator:
        async def create(self, *_: object) -> Any:
            raise AssertionError("refresh never creates identities")

    class _NoTarget:
        def adjust_target(self, delta: int) -> None:
            raise AssertionError("refresh never changes population")

    actions = OperatorActionManager(
        repository=repository,
        creator=_NoCreator(),
        monitor=monitor,
        state_store=state_store,
        target_adjustment=_NoTarget(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
    )
    await actions.start()
    try:
        await actions.request(OperatorActionKind.REFRESH, item.session_id)

        def finished() -> bool:
            action = actions.for_session(item.session_id)
            return action is not None and action.status in {
                OperatorActionStatus.SUCCESS,
                OperatorActionStatus.FAILED,
            }

        await _until(finished)
        refreshed = await repository.get(item.session_id)
        progress = await repository.get_progress(item.session_id)
        assert refreshed is not None
        assert refreshed.queue_id == "sim-ui-2"
        assert refreshed.worker_id is None
        assert refreshed.status is QueueStatus.ACTIVE_QUEUE
        # The verified live observation is persisted even though the state save failed.
        assert progress is not None and progress.progress_percentage == 73
        assert (await browser.capacity(repair=False)).active_contexts == 0
    finally:
        await actions.close(timeout_seconds=5)
        await browser.shutdown()
        await repository.close()
        await simulator.close()
