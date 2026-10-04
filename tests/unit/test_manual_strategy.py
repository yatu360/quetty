"""Manual Strategy: one operator-owned visible acquisition window at a time."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.models import (
    BrowserBackendName,
    MonitoringStrategy,
    ProxyProvider,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.proxy import IPRoyalCredentials, SessionProxyResolver
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationWorkItem,
    PollingPolicy,
    QueueSessionCreator,
    QueueSessionMonitor,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import (
    OpenedSessionRestore,
    QueueSessionRestorer,
    RestoreMethod,
    SessionRestoreResult,
)
from queue_load_test.web import service as service_module
from queue_load_test.web.manual import (
    ManualAcquisitionState,
    ManualChromeSessionManager,
    ManualCloseReason,
    ManualOpenError,
)
from queue_load_test.web.manual_acquisition import (
    MANUAL_BROWSER_LOST,
    MANUAL_WINDOW_CLOSED,
    ManualAcquisitionHandler,
)
from queue_load_test.web.service import ApplicationRunRuntime

TARGET = "https://staging.example.test/"
FAKE_SERVER = "http://proxy.fake-iproyal.test:12321"
CREDENTIALS = IPRoyalCredentials(
    server=FAKE_SERVER, username="FAKEUSER_manual7Hd", base_password="FAKEPASS_manual3Tc"
)


class EventSource:
    def __init__(self) -> None:
        self.callbacks: dict[str, list[Callable[[Any], None]]] = {}

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.callbacks.setdefault(event, []).append(callback)

    def emit(self, event: str, payload: Any = None) -> None:
        for callback in list(self.callbacks.get(event, [])):
            callback(self if payload is None else payload)


class FakeBrowser:
    def __init__(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected


class FakePage(EventSource):
    def __init__(self, context: FakeContext) -> None:
        super().__init__()
        self.context = context
        self.closed = False
        self.restricted = False

    def is_closed(self) -> bool:
        return self.closed

    def operator_close(self) -> None:
        """What the browser reports when the operator closes the tab/window."""

        if not self.closed:
            self.closed = True
            self.emit("close")


class FakeContext(EventSource):
    def __init__(self, browser: FakeBrowser) -> None:
        super().__init__()
        self.browser = browser
        self.pages: list[FakePage] = []
        self.closed = False

    def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        self.emit("page", page)
        return page


class FakeOwnedContext:
    def __init__(self, browser: FakeBrowser) -> None:
        self.context = FakeContext(browser)
        self.closed = False
        self.browser_id = 0
        # Set when application code closes a window the operator still had open.
        self.closed_by_application_while_open = False

    async def close(self) -> None:
        if self.closed:
            return
        if any(not page.closed for page in self.context.pages):
            self.closed_by_application_while_open = True
        self.closed = True
        self.context.closed = True
        for page in self.context.pages:
            page.operator_close()
        self.context.emit("close")


class FakeHeadedManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.started = False
        self.shutdown_calls = 0

    async def start(self) -> None:
        self.started = True

    async def capacity(self, *, repair: bool = True) -> object:
        @dataclass
        class Process:
            index: int
            connected: bool

        @dataclass
        class Capacity:
            processes: list[Process]

        return Capacity([Process(0, self.browser.connected)])

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.started = False


@dataclass
class Window:
    session_id: str
    proxy_session_id: str | None
    owned: FakeOwnedContext
    page: FakePage
    keep_open_on_navigation_failure: bool


@dataclass
class FakeWindowRestorer:
    """The headed restorer seen by the manager; one ``Window`` per acquisition window."""

    browser: FakeBrowser
    windows: list[Window] = field(default_factory=list)
    # Queue IDs the operator reaches, keyed by window index.
    queue_ids: dict[int, str] = field(default_factory=dict)
    # What an in-window inspection observes (empty: identity only, no usable state).
    progress: Callable[[str], QueueProgress] = field(
        default=lambda session_id: QueueProgress(session_id=session_id)
    )
    adoption_calls: list[bool] = field(default_factory=list)
    inspections: int = 0
    maximum_open: int = 0

    def open_windows(self) -> list[Window]:
        return [window for window in self.windows if not window.owned.closed]

    async def restore_open(
        self, session: QueueSession, *, keep_open_on_navigation_failure: bool = False
    ) -> OpenedSessionRestore:
        assert session.queue_id is None
        owned = FakeOwnedContext(self.browser)
        page = owned.context.new_page()
        self.windows.append(
            Window(
                session.session_id,
                session.proxy_session_id,
                owned,
                page,
                keep_open_on_navigation_failure,
            )
        )
        self.maximum_open = max(self.maximum_open, len(self.open_windows()))
        return OpenedSessionRestore(
            SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=True,
                expected_queue_id=None,
                observed_queue_id=None,
                identity_match=None,
            ),
            cast(Any, owned),
            cast(Any, page),
            identity_pending=True,
        )

    def _index(self, page: object) -> int:
        return next(
            index
            for index, window in enumerate(self.windows)
            if page in window.owned.context.pages
        )

    async def adopt_open(
        self,
        session: QueueSession,
        *,
        context: object,
        page: object,
        require_live_queue: bool = True,
    ) -> SessionRestoreResult | None:
        self.adoption_calls.append(require_live_queue)
        queue_id = self.queue_ids.get(self._index(page))
        if queue_id is None or cast(FakePage, page).closed:
            return None
        session.queue_id = queue_id
        session.transfer_url = f"https://queue.example.test/?q={queue_id}"
        session.last_error = None
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=queue_id,
            observed_queue_id=queue_id,
            identity_match=True,
            state_refreshed=True,
            progress=self.progress(session.session_id),
        )

    async def inspect_open(
        self, session: QueueSession, *, context: object, page: object
    ) -> SessionRestoreResult:
        self.inspections += 1
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=self.progress(session.session_id),
        )

    async def discard_state(self, session: QueueSession) -> None:
        return None


class PageRestrictionDetector:
    async def detect(self, page: Any) -> bool:
        return bool(page.restricted)


class UnusedRestorer:
    async def restore(self, _: QueueSession) -> SessionRestoreResult:
        raise AssertionError("manual acquisition observes its own open window")


class NoContextManager:
    def context(self, **_: Any) -> Any:
        raise AssertionError("Manual Strategy never uses the automatic creation context")


@dataclass
class Rig:
    repository: SQLiteSessionRepository
    windows: FakeWindowRestorer
    manager: ManualChromeSessionManager
    headed: FakeHeadedManager
    creator: QueueSessionCreator
    handler: ManualAcquisitionHandler
    browser: FakeBrowser

    def controller(self, *, target: int, workers: int = 1) -> SessionCreationController:
        return SessionCreationController(
            repository=self.repository,
            handler=self.handler,
            target_queue_ids=target,
            worker_count=workers,
            queue_capacity=workers,
        )

    async def rows(self) -> list[QueueSession]:
        return sorted(await self.repository.list(), key=lambda row: row.created_at)


def proxied_run() -> RunConfig:
    return RunConfig(
        run_id="manual",
        target_url=TARGET,
        requested_sessions=1,
        created_at=datetime.now(UTC),
        browser_backend=BrowserBackendName.PATCHRIGHT,
        monitoring_strategy=MonitoringStrategy.MANUAL,
        proxy_provider=ProxyProvider.IPROYAL,
        proxy_country="gb",
        proxy_lifetime="2h",
    )


class RecordingIpTracker:
    def __init__(self) -> None:
        self.observed: list[tuple[str, str | None]] = []

    async def observe_after_check(self, session: QueueSession) -> None:
        self.observed.append((session.session_id, session.proxy_session_id))


async def make_rig(
    tmp_path: Path,
    *,
    proxied: bool = False,
    repository: SQLiteSessionRepository | None = None,
    ip_tracker: RecordingIpTracker | None = None,
) -> Rig:
    repository = repository or SQLiteSessionRepository(tmp_path / "manual.sqlite3")
    browser = FakeBrowser()
    windows = FakeWindowRestorer(browser)
    headed = FakeHeadedManager(browser)
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=UnusedRestorer(),
        polling_policy=PollingPolicy(jitter_seconds=0),
    )
    manager = ManualChromeSessionManager(
        repository=repository,
        browser_manager=cast(BrowserManager, headed),
        restorer=cast(QueueSessionRestorer, windows),
        monitor=monitor,
        capacity=5,
        lease_seconds=30,
        restriction_detector=PageRestrictionDetector(),
        acquisition_poll_seconds=0.005,
    )
    state_store = FileSystemStateStore(tmp_path / "state")
    creator = QueueSessionCreator(
        browser_manager=cast(BrowserManager, NoContextManager()),
        repository=repository,
        state_store=state_store,
        staging_url=TARGET,
        state_directory=tmp_path / "state",
        mode=SessionMode.HYBRID,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=SessionProxyResolver(proxied_run(), CREDENTIALS) if proxied else None,
        proxy_ip_tracker=cast(Any, ip_tracker),
    )
    handler = ManualAcquisitionHandler(
        creator=creator,
        manual_sessions=manager,
        repository=repository,
        state_store=state_store,
        open_failure_delay_seconds=0,
    )
    return Rig(repository, windows, manager, headed, creator, handler, browser)


async def until(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def row(rig: Rig, session_id: str) -> QueueSession:
    found = await rig.repository.get(session_id)
    assert found is not None
    return found


async def persisted_with_id(rig: Rig, session_id: str) -> bool:
    found = await rig.repository.get(session_id)
    return found is not None and found.queue_id is not None and found.status is (
        QueueStatus.PRE_QUEUE
    )


async def released(rig: Rig, session_id: str) -> bool:
    found = await rig.repository.get(session_id)
    return found is not None and found.manual_owner_id is None


@pytest.mark.parametrize("workers", [1, 3])
async def test_windows_are_strictly_sequential_and_advance_only_on_operator_close(
    tmp_path: Path, workers: int
) -> None:
    rig = await make_rig(tmp_path)
    controller = rig.controller(target=3, workers=workers)
    run = asyncio.create_task(controller.run())

    # Window 1 opens; acquiring its Queue ID neither closes it nor opens window 2.
    await until(lambda: len(rig.windows.windows) == 1)
    first = rig.windows.windows[0]
    assert first.keep_open_on_navigation_failure is True
    rig.windows.queue_ids[0] = "queue-one"
    await until(lambda: persisted_with_id(rig, first.session_id))
    assert rig.manager.acquisition_state is ManualAcquisitionState.QUEUE_ID_ACQUIRED
    persisted = await row(rig, first.session_id)
    assert persisted.queue_id == "queue-one"
    assert persisted.manual_owner_id is not None  # still operator-owned, unclaimable
    await asyncio.sleep(0.2)  # ~40 observation ticks
    assert len(rig.windows.windows) == 1
    assert not first.owned.closed and not run.done()

    # Closing window 1 is the continue signal for window 2.
    first.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 2)
    second = rig.windows.windows[1]
    after_close = await row(rig, first.session_id)
    assert after_close.queue_id == "queue-one"
    assert after_close.status is QueueStatus.PRE_QUEUE
    assert after_close.manual_owner_id is None

    # Window 2 closes before any Queue ID: a FAILED attempt, never counted.
    await asyncio.sleep(0.05)
    assert len(rig.windows.windows) == 2
    second.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 3)
    failed = await row(rig, second.session_id)
    assert failed.status is QueueStatus.FAILED
    assert failed.queue_id is None and failed.transfer_url == ""
    assert failed.last_error == MANUAL_WINDOW_CLOSED
    assert await rig.repository.count_successful_queue_ids() == 1

    # Window 3 cannot be followed by window 4 until it is closed.
    third = rig.windows.windows[2]
    rig.windows.queue_ids[2] = "queue-three"
    await until(lambda: persisted_with_id(rig, third.session_id))
    await asyncio.sleep(0.05)
    assert len(rig.windows.windows) == 3
    third.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 4)
    fourth = rig.windows.windows[3]
    rig.windows.queue_ids[3] = "queue-four"
    await until(lambda: persisted_with_id(rig, fourth.session_id))
    assert not run.done()
    fourth.page.operator_close()
    metrics = await asyncio.wait_for(run, timeout=5)

    assert metrics.successful_unique_ids == 3
    assert metrics.permanent_failures == 1
    assert rig.windows.maximum_open == 1
    assert len(rig.windows.windows) == 4
    assert not any(w.owned.closed_by_application_while_open for w in rig.windows.windows)
    assert all(w.owned.closed for w in rig.windows.windows)
    assert rig.windows.adoption_calls and not any(rig.windows.adoption_calls)
    rows = await rig.rows()
    assert [r.queue_id for r in rows] == ["queue-one", None, "queue-three", "queue-four"]
    assert all(r.manual_owner_id is None for r in rows)
    await rig.manager.close()
    await rig.repository.close()


async def test_wait_has_no_deadline_and_never_closes_a_healthy_window(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)

    # Thousands of observation ticks: nothing times out, closes, or opens.
    await asyncio.sleep(1.0)
    window = rig.windows.windows[0]
    assert not window.owned.closed and not run.done()
    assert len(rig.windows.windows) == 1
    assert rig.manager.acquisition_state is ManualAcquisitionState.AWAITING_QUEUE_ID
    reservation = await row(rig, window.session_id)
    assert reservation.status is QueueStatus.CREATING and reservation.queue_id is None

    rig.windows.queue_ids[0] = "queue-late"
    window.page.operator_close()
    # Closed before the identity was observed: the late ID is never fabricated.
    await asyncio.sleep(0.05)
    assert (await row(rig, window.session_id)).queue_id is None
    await until(lambda: len(rig.windows.windows) == 2)
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    await rig.manager.close()
    await rig.repository.close()


async def test_an_extra_tab_or_navigation_does_not_end_the_window(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)
    window = rig.windows.windows[0]

    popup = window.owned.context.new_page()
    window.page.operator_close()  # the original tab closes; the popup is still open
    await asyncio.sleep(0.05)
    assert not run.done() and len(rig.windows.windows) == 1

    rig.windows.queue_ids[0] = "queue-popup"
    await until(lambda: persisted_with_id(rig, window.session_id))
    popup.operator_close()
    metrics = await asyncio.wait_for(run, timeout=5)
    assert metrics.successful_unique_ids == 1
    assert len(rig.windows.windows) == 1
    await rig.manager.close()
    await rig.repository.close()


async def test_identity_with_explicit_state_is_persisted_with_that_state(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    rig.windows.progress = lambda session_id: QueueProgress(
        session_id=session_id, queue_number="12", users_ahead=11, active_queue=True
    )
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)
    rig.windows.queue_ids[0] = "queue-active"
    window = rig.windows.windows[0]

    async def active() -> bool:
        found = await rig.repository.get(window.session_id)
        return found is not None and found.status is QueueStatus.ACTIVE_QUEUE

    await until(active)
    window.page.operator_close()
    await asyncio.wait_for(run, timeout=5)
    persisted = await row(rig, window.session_id)
    assert persisted.queue_id == "queue-active"
    assert persisted.status is QueueStatus.ACTIVE_QUEUE
    await rig.manager.close()
    await rig.repository.close()


async def test_access_restricted_window_stays_open_until_the_operator_closes_it(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    controller = rig.controller(target=1)
    run = asyncio.create_task(controller.run())
    await until(lambda: len(rig.windows.windows) == 1)
    window = rig.windows.windows[0]
    window.page.restricted = True

    await until(
        lambda: rig.manager.acquisition_state
        is ManualAcquisitionState.ACCESS_RESTRICTED_BEFORE_QUEUE
    )
    await asyncio.sleep(0.2)
    assert not window.owned.closed and len(rig.windows.windows) == 1 and not run.done()

    window.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 2)
    failed = await row(rig, window.session_id)
    assert failed.status is QueueStatus.FAILED
    assert failed.last_error == "access_restricted_before_queue"
    assert failed.queue_id is None
    assert rig.creator.access_restricted_attempts == 1
    assert controller.metrics.access_restricted == 1
    assert controller.metrics.consecutive_access_restricted == 1
    replacement = rig.windows.windows[1]
    assert replacement.session_id != window.session_id
    assert not replacement.owned.closed
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    await rig.manager.close()
    await rig.repository.close()


async def test_browser_loss_ends_the_window_with_its_own_classification(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)
    window = rig.windows.windows[0]

    rig.browser.connected = False
    window.page.operator_close()  # a crash closes pages too
    await until(lambda: len(rig.windows.windows) == 2)
    failed = await row(rig, window.session_id)
    assert failed.last_error == MANUAL_BROWSER_LOST
    assert await rig.repository.count_successful_queue_ids() == 0
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    await rig.manager.close()
    await rig.repository.close()


async def test_dashboard_close_is_an_operator_continue_signal(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)
    window = rig.windows.windows[0]
    rig.windows.queue_ids[0] = "queue-dashboard"
    await until(lambda: persisted_with_id(rig, window.session_id))

    assert await rig.manager.close_session(window.session_id)
    metrics = await asyncio.wait_for(run, timeout=5)
    assert metrics.successful_unique_ids == 1
    assert (await row(rig, window.session_id)).queue_id == "queue-dashboard"
    await rig.manager.close()
    await rig.repository.close()


async def test_a_second_acquisition_window_is_refused_while_one_is_open(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)

    with pytest.raises(ManualOpenError, match="already open"):
        await rig.manager.run_acquisition_window("anything")
    assert len(rig.windows.windows) == 1
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    await rig.manager.close()
    await rig.repository.close()


async def test_duplicate_queue_id_in_window_is_never_adopted(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    run = asyncio.create_task(rig.controller(target=2).run())
    await until(lambda: len(rig.windows.windows) == 1)
    first = rig.windows.windows[0]
    rig.windows.queue_ids[0] = "queue-same"
    await until(lambda: persisted_with_id(rig, first.session_id))
    first.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 2)

    second = rig.windows.windows[1]
    rig.windows.queue_ids[1] = "queue-same"
    await asyncio.sleep(0.1)
    second.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 3)
    duplicate = await row(rig, second.session_id)
    assert duplicate.status is QueueStatus.FAILED
    assert duplicate.queue_id is None and duplicate.last_error == "duplicate_queue_id"
    original = await row(rig, first.session_id)
    assert original.queue_id == "queue-same" and original.status is QueueStatus.PRE_QUEUE
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    await rig.manager.close()
    await rig.repository.close()


async def test_each_attempt_uses_its_own_immutable_iproyal_session(tmp_path: Path) -> None:
    tracker = RecordingIpTracker()
    rig = await make_rig(tmp_path, proxied=True, ip_tracker=tracker)
    run = asyncio.create_task(rig.controller(target=1).run())
    await until(lambda: len(rig.windows.windows) == 1)
    first = rig.windows.windows[0]
    assert first.proxy_session_id is not None
    first.page.operator_close()  # unsuccessful attempt
    await until(lambda: len(rig.windows.windows) == 2)
    second = rig.windows.windows[1]
    rig.windows.queue_ids[1] = "queue-proxied"
    await until(lambda: persisted_with_id(rig, second.session_id))
    second.page.operator_close()
    await asyncio.wait_for(run, timeout=5)

    failed = await row(rig, first.session_id)
    acquired = await row(rig, second.session_id)
    assert second.proxy_session_id is not None
    assert first.proxy_session_id != second.proxy_session_id
    # The window was opened for the reserved row, with that row's own sticky ID, and
    # the ID survives acquisition, in-window inspection, and close unchanged.
    assert failed.proxy_session_id == first.proxy_session_id
    assert acquired.proxy_session_id == second.proxy_session_id
    assert acquired.queue_id == "queue-proxied"
    # One baseline exit-IP lookup, for the acquired session only, through its own ID.
    assert tracker.observed == [(second.session_id, second.proxy_session_id)]
    await rig.manager.close()
    await rig.repository.close()


async def test_shutdown_while_waiting_is_bounded_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    stop = asyncio.Event()
    run = asyncio.create_task(rig.controller(target=3).run(stop))
    await until(lambda: len(rig.windows.windows) == 1)
    first = rig.windows.windows[0]
    rig.windows.queue_ids[0] = "queue-kept"
    await until(lambda: persisted_with_id(rig, first.session_id))
    first.page.operator_close()
    await until(lambda: len(rig.windows.windows) == 2)
    waiting = rig.windows.windows[1]

    # The ordered runtime shutdown: stop accepting, then stop producers.
    rig.manager.stop_accepting()
    stop.set()
    metrics = await asyncio.wait_for(run, timeout=5)
    await asyncio.wait_for(rig.manager.close(), timeout=5)

    assert metrics.successful_unique_ids == 1
    assert len(rig.windows.windows) == 2  # no next window during shutdown
    assert waiting.owned.closed
    rows = await rig.rows()
    # The interrupted no-ID attempt is discarded; the persisted identity is kept.
    assert [(r.queue_id, r.status) for r in rows] == [("queue-kept", QueueStatus.PRE_QUEUE)]
    assert all(r.manual_owner_id is None and r.worker_id is None for r in rows)
    assert rig.manager.open_count == 0 and rig.manager.acquisition_state is None
    assert rig.headed.shutdown_calls == 1

    # Restart resumes only the remaining deficit of persisted successful IDs.
    restarted = await make_rig(tmp_path, repository=rig.repository)
    controller = restarted.controller(target=3)
    resumed = asyncio.create_task(controller.run())
    for index in range(2):
        await until(lambda index=index: len(restarted.windows.windows) == index + 1)
        window = restarted.windows.windows[index]
        restarted.windows.queue_ids[index] = f"queue-resumed-{index}"
        await until(lambda window=window: persisted_with_id(restarted, window.session_id))
        window.page.operator_close()
    resumed_metrics = await asyncio.wait_for(resumed, timeout=5)
    assert resumed_metrics.initial_successful_unique_ids == 1
    assert resumed_metrics.successful_unique_ids == 3
    assert len(restarted.windows.windows) == 2
    await restarted.manager.close()
    await rig.repository.close()


async def test_shutdown_with_an_acquired_window_keeps_the_identity(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    stop = asyncio.Event()
    run = asyncio.create_task(rig.controller(target=2).run(stop))
    await until(lambda: len(rig.windows.windows) == 1)
    window = rig.windows.windows[0]
    rig.windows.queue_ids[0] = "queue-shutdown"
    await until(lambda: persisted_with_id(rig, window.session_id))

    rig.manager.stop_accepting()
    stop.set()
    await asyncio.wait_for(run, timeout=5)
    await rig.manager.close()

    kept = await row(rig, window.session_id)
    assert kept.queue_id == "queue-shutdown" and kept.manual_owner_id is None
    assert window.owned.closed and len(rig.windows.windows) == 1
    await rig.repository.close()


async def test_window_end_reason_records_first_cause_only(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    reservation = await rig.creator.reserve_session(
        CreationWorkItem(sequence=1, session_id="explicit")
    )
    task = asyncio.create_task(rig.manager.run_acquisition_window(reservation.session_id))
    await until(lambda: len(rig.windows.windows) == 1)
    rig.windows.windows[0].page.operator_close()
    result = await asyncio.wait_for(task, timeout=5)
    assert result.close_reason is ManualCloseReason.OPERATOR_CLOSED
    assert result.identity_adopted is False and result.access_restricted is False
    await rig.manager.close()
    await rig.repository.close()


# --- Runtime wiring -------------------------------------------------------------------


def runtime_settings(tmp_path: Path, **overrides: Any) -> Settings:
    database = tmp_path / "wiring.sqlite3"
    values: dict[str, Any] = {
        "_env_file": None,
        "DATABASE_URL": f"sqlite:///{database}",
        "STATE_DIRECTORY": str(tmp_path / "state"),
        "CHROME_PROCESS_COUNT": 1,
        "MAX_CONTEXTS_PER_BROWSER": 10,
        "MAX_ACTIVE_CONTEXTS": 10,
        "CREATION_WORKERS": 3,
        "CREATION_QUEUE_CAPACITY": 3,
    }
    values.update(overrides)
    return Settings(**values)


def run_config(strategy: MonitoringStrategy) -> RunConfig:
    return RunConfig(
        run_id=f"run-{strategy.value}",
        target_url=TARGET,
        requested_sessions=3,
        created_at=datetime.now(UTC),
        browser_backend=BrowserBackendName.CHROME,
        monitoring_strategy=strategy,
    )


async def test_manual_run_forces_one_visible_sequential_acquisition_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_run(self: object) -> None:
        return None

    monkeypatch.setattr(service_module.ApplicationRuntime, "run", no_run)
    settings = runtime_settings(tmp_path)
    repository = SQLiteSessionRepository(tmp_path / "wiring.sqlite3")
    runtime = ApplicationRunRuntime(settings=settings, repository=repository)
    await runtime.start_run(run_config(MonitoringStrategy.MANUAL))
    try:
        creation: Any = runtime._creation
        manual: Any = runtime._manual_sessions
        headed: Any = runtime._headed_browser_manager
        scheduler: Any = runtime._monitoring_scheduler
        actions: Any = runtime._operator_actions
        assert creation._worker_count == 1 and creation._queue_capacity == 1
        assert isinstance(creation._handler, ManualAcquisitionHandler)
        assert creation._handler._manual_sessions is manual
        # The acquisition window is visible, with its own headed slot.
        assert headed._headless is False
        assert headed._max_active_contexts == settings.max_manual_open_sessions + 1
        # After acquisition: the existing browser monitor, for automatic and Refresh.
        assert isinstance(scheduler._handler, QueueSessionMonitor)
        assert actions._monitor is scheduler._handler
        assert runtime.manual_acquisition_state is None
    finally:
        await runtime.close()
        await repository.close()


@pytest.mark.parametrize(
    "strategy", [MonitoringStrategy.HEADED_WINDOW, MonitoringStrategy.DIRECT]
)
async def test_other_strategies_keep_configured_creation_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strategy: MonitoringStrategy
) -> None:
    async def no_run(self: object) -> None:
        return None

    monkeypatch.setattr(service_module.ApplicationRuntime, "run", no_run)
    settings = runtime_settings(tmp_path)
    repository = SQLiteSessionRepository(tmp_path / "wiring.sqlite3")
    runtime = ApplicationRunRuntime(settings=settings, repository=repository)
    await runtime.start_run(run_config(strategy))
    try:
        creation: Any = runtime._creation
        headed: Any = runtime._headed_browser_manager
        assert creation._worker_count == 3 and creation._queue_capacity == 3
        assert isinstance(creation._handler, QueueSessionCreator)
        assert headed._max_active_contexts == settings.max_manual_open_sessions
    finally:
        await runtime.close()
        await repository.close()


async def test_persisted_manual_strategy_is_stored_as_manual(tmp_path: Path) -> None:
    database = tmp_path / "persisted.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(run_config(MonitoringStrategy.MANUAL))
    restored = await repository.get_active_run()
    await repository.close()

    assert restored is not None and restored.monitoring_strategy is MonitoringStrategy.MANUAL
    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT monitoring_strategy FROM run_config").fetchone()
    assert stored == ("manual",)
    assert MonitoringStrategy.parse("Manual") is MonitoringStrategy.MANUAL
    assert MonitoringStrategy.MANUAL.label == "Manual Strategy"
