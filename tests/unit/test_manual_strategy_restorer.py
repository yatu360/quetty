"""Restorer seams used by Manual Strategy acquisition windows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserManager
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
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import (
    QueueSessionRestorer,
    RestoreFailure,
    TransferExtractionResult,
)

TARGET = "https://staging.example.test/"
CREDENTIALS = IPRoyalCredentials(
    server="http://proxy.fake-iproyal.test:12321",
    username="FAKEUSER_window2Hd",
    base_password="FAKEPASS_window6Tc",
)


class FakePage:
    def __init__(self, *, fail_navigation: bool) -> None:
        self.fail_navigation = fail_navigation
        self.gotos: list[str] = []
        self.url = TARGET

    def on(self, event: str, callback: Any) -> None:
        return None

    async def goto(self, url: str, **_: Any) -> None:
        self.gotos.append(url)
        if self.fail_navigation:
            raise TimeoutError("navigation timed out")


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page

    async def storage_state(self) -> dict[str, Any]:
        return {"cookies": [], "origins": []}


class FakeOwned:
    def __init__(self, page: FakePage) -> None:
        self.context = FakeContext(page)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class RecordingManager:
    def __init__(self, *, fail_navigation: bool = False) -> None:
        self.fail_navigation = fail_navigation
        self.calls: list[dict[str, Any]] = []
        self.owned: list[FakeOwned] = []

    async def create_context(self, **kwargs: Any) -> FakeOwned:
        self.calls.append(kwargs)
        owned = FakeOwned(FakePage(fail_navigation=self.fail_navigation))
        self.owned.append(owned)
        return owned

    def report_navigation(self, context: object, *, responsive: bool) -> None:
        return None


class IdentityOnlyExtractor:
    """A live page exposing no pre-queue, progress, or lifecycle markers."""

    async def extract(self, page: object, *, session_id: str) -> QueueProgress:
        return QueueProgress(session_id=session_id)


class TransferFactory:
    def __init__(self, queue_id: str) -> None:
        self.queue_id = queue_id

    def __call__(self, _: str) -> TransferFactory:
        return self

    async def extract(
        self, page: object, *, expected_queue_id: str | None = None
    ) -> TransferExtractionResult:
        return TransferExtractionResult(
            transfer_url=f"https://queue.example.test/?q={self.queue_id}",
            observed_queue_id=self.queue_id,
        )


def reservation(proxy_session_id: str | None = None) -> QueueSession:
    return QueueSession(
        session_id="window",
        queue_id=None,
        transfer_url="",
        mode=SessionMode.HYBRID,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_session_id=proxy_session_id,
        status=QueueStatus.CREATING,
        state_path=Path("state/window.json"),
    )


def restorer(
    tmp_path: Path, manager: RecordingManager, *, proxied: bool = False
) -> QueueSessionRestorer:
    resolver = (
        SessionProxyResolver(
            RunConfig(
                run_id="r",
                target_url=TARGET,
                requested_sessions=1,
                created_at=datetime.now(UTC),
                browser_backend=BrowserBackendName.PATCHRIGHT,
                monitoring_strategy=MonitoringStrategy.MANUAL,
                proxy_provider=ProxyProvider.IPROYAL,
                proxy_country="gb",
                proxy_lifetime="2h",
            ),
            CREDENTIALS,
        )
        if proxied
        else None
    )
    return QueueSessionRestorer(
        browser_manager=cast(BrowserManager, manager),
        repository=cast(Any, SQLiteSessionRepository(tmp_path / "unused.sqlite3")),
        state_store=FileSystemStateStore(tmp_path / "state"),
        storage_navigation_url=TARGET,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        live_extractor=cast(Any, IdentityOnlyExtractor()),
        transfer_extractor_factory=cast(Any, TransferFactory("queue-window")),
        proxy_resolver=resolver,
    )


async def test_acquisition_window_stays_open_after_a_failed_navigation(tmp_path: Path) -> None:
    manager = RecordingManager(fail_navigation=True)

    opened = await restorer(tmp_path, manager).restore_open(
        reservation(), keep_open_on_navigation_failure=True
    )

    assert opened.owned_context is not None and opened.page is not None
    assert opened.identity_pending is True
    assert manager.owned[0].closed is False


async def test_manual_open_still_discards_a_window_whose_navigation_failed(
    tmp_path: Path,
) -> None:
    manager = RecordingManager(fail_navigation=True)

    opened = await restorer(tmp_path, manager).restore_open(reservation())

    assert opened.owned_context is None
    assert opened.result.failure is RestoreFailure.NAVIGATION_FAILED
    assert manager.owned[0].closed is True


async def test_acquisition_window_routes_through_the_reserved_sticky_session(
    tmp_path: Path,
) -> None:
    manager = RecordingManager()

    opened = await restorer(tmp_path, manager, proxied=True).restore_open(
        reservation("Mn34Pq56"), keep_open_on_navigation_failure=True
    )

    assert opened.owned_context is not None
    proxy = manager.calls[0]["proxy"]
    assert "_session-Mn34Pq56_" in proxy["password"]
    assert manager.owned[0].context.page.gotos == [TARGET]


async def test_acquisition_window_with_unassigned_proxy_fails_closed(tmp_path: Path) -> None:
    manager = RecordingManager()

    opened = await restorer(tmp_path, manager, proxied=True).restore_open(
        reservation(None), keep_open_on_navigation_failure=True
    )

    assert opened.owned_context is None
    assert opened.result.failure is RestoreFailure.PROXY_ASSIGNMENT_MISSING
    assert manager.calls == []


async def test_identity_is_adopted_without_live_queue_markers_only_when_requested(
    tmp_path: Path,
) -> None:
    manager = RecordingManager()
    window_restorer = restorer(tmp_path, manager)
    session = reservation()
    context: Any = FakeContext(FakePage(fail_navigation=False))

    unchanged = await window_restorer.adopt_open(session, context=context, page=context.page)
    assert unchanged is None and session.queue_id is None

    adopted = await window_restorer.adopt_open(
        session, context=context, page=context.page, require_live_queue=False
    )
    assert adopted is not None and adopted.success
    assert session.queue_id == "queue-window"
    assert adopted.observed_queue_id == "queue-window" and adopted.identity_match is True
