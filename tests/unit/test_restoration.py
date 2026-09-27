import asyncio
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from queue_load_test.browser import BrowserManager
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore, StateUnreadableError
from queue_load_test.transfer import (
    QueueSessionRestorer,
    RestoreFailure,
    RestoreMethod,
    TransferExtractionResult,
    TransferFailure,
)


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self) -> None:
        self.visited_urls: list[str] = []

    async def goto(self, url: str, **_: object) -> FakeResponse:
        self.visited_urls.append(url)
        return FakeResponse()


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def storage_state(self) -> dict[str, object]:
        return {"cookies": [{"name": "refreshed"}], "origins": []}


class TimeoutPage(FakePage):
    async def goto(self, url: str, **_: object) -> FakeResponse:
        self.visited_urls.append(url)
        raise PlaywrightTimeoutError("controlled navigation timeout")


class TimeoutContext(FakeContext):
    def __init__(self) -> None:
        super().__init__()
        self.page = TimeoutPage()


class FakeBrowserManager:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.storage_states: list[object | None] = []

    async def create_context(self, *, storage_state: object | None = None) -> object:
        context = FakeContext()
        self.contexts.append(context)
        self.storage_states.append(storage_state)
        return FakeOwnedContext(context)

    @asynccontextmanager
    async def context(self, *, storage_state: object | None = None):
        context = FakeContext()
        self.contexts.append(context)
        self.storage_states.append(storage_state)
        try:
            yield context
        finally:
            context.closed = True


class TimeoutBrowserManager(FakeBrowserManager):
    @asynccontextmanager
    async def context(self, *, storage_state: object | None = None):
        context = TimeoutContext()
        self.contexts.append(context)
        self.storage_states.append(storage_state)
        try:
            yield context
        finally:
            context.closed = True


class FakeOwnedContext:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.context.closed = True


class LiveExtractor:
    async def extract(self, _: object, *, session_id: str) -> QueueProgress:
        return QueueProgress(
            session_id=session_id,
            queue_number="123",
            users_ahead=12,
            active_queue=True,
        )


class NoTerminalStateDetector:
    async def detect(self, _: object) -> None:
        return None


class ScriptedTransferExtractor:
    def __init__(self, results: list[TransferExtractionResult]) -> None:
        self.results = deque(results)
        self.expected_ids: list[str | None] = []

    async def extract(
        self,
        _: object,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult:
        self.expected_ids.append(expected_queue_id)
        return self.results.popleft()


def session(mode: SessionMode) -> QueueSession:
    return QueueSession(
        session_id="session-1",
        queue_id="queue-expected",
        transfer_url="https://queue.staging.test/journey?q=queue-expected",
        mode=mode,
        status=QueueStatus.PARKED,
        state_path=Path(".browser-state/session-1.json"),
    )


async def setup_restorer(
    tmp_path: Path,
    mode: SessionMode,
    results: list[TransferExtractionResult],
    *,
    storage_navigation_url: str | None = None,
) -> tuple[
    QueueSessionRestorer,
    QueueSession,
    SQLiteSessionRepository,
    FileSystemStateStore,
    FakeBrowserManager,
    ScriptedTransferExtractor,
]:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    persisted_session = session(mode)
    await repository.create(persisted_session)
    browser_manager = FakeBrowserManager()
    transfer_extractor = ScriptedTransferExtractor(results)
    restorer = QueueSessionRestorer(
        browser_manager=cast(BrowserManager, browser_manager),
        repository=repository,
        state_store=state_store,
        expected_journey_url="https://queue.staging.test/journey",
        storage_navigation_url=storage_navigation_url,
        live_extractor=cast(Any, LiveExtractor()),
        terminal_state_detector=cast(Any, NoTerminalStateDetector()),
        transfer_extractor_factory=lambda _: transfer_extractor,
        observation_timeout_seconds=0,
    )
    return (
        restorer,
        persisted_session,
        repository,
        state_store,
        browser_manager,
        transfer_extractor,
    )


def matching_result() -> TransferExtractionResult:
    return TransferExtractionResult(
        transfer_url="https://queue.staging.test/journey?q=queue-expected",
        expected_queue_id="queue-expected",
        observed_queue_id="queue-expected",
    )


def unavailable_result() -> TransferExtractionResult:
    return TransferExtractionResult(
        expected_queue_id="queue-expected",
        failure=TransferFailure.MISSING_UI,
    )


def mismatch_result() -> TransferExtractionResult:
    return TransferExtractionResult(
        transfer_url="https://queue.staging.test/journey?q=queue-other",
        expected_queue_id="queue-expected",
        observed_queue_id="queue-other",
        identity_mismatch=True,
        failure=TransferFailure.IDENTITY_MISMATCH,
    )


async def test_transfer_only_restores_same_identity_and_progress(tmp_path: Path) -> None:
    restorer, expected, repository, _, manager, extractor = await setup_restorer(
        tmp_path,
        SessionMode.TRANSFER_ONLY,
        [matching_result()],
    )

    result = await restorer.restore(expected)

    assert result.success
    assert result.method is RestoreMethod.TRANSFER
    assert result.identity_match is True
    assert result.observed_queue_id == "queue-expected"
    assert extractor.expected_ids == ["queue-expected"]
    assert manager.contexts[0].page.visited_urls == [expected.transfer_url]
    assert manager.storage_states == [None]
    assert manager.contexts[0].closed
    progress = await repository.get_progress(expected.session_id)
    assert progress is not None
    assert progress.queue_number == "123"
    persisted = await repository.get(expected.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    assert persisted.last_error is None
    await repository.close()


async def test_manual_open_retains_only_matching_identity_context(tmp_path: Path) -> None:
    restorer, expected, repository, _, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.TRANSFER_ONLY,
        [matching_result()],
    )

    opened = await restorer.restore_open(expected)

    assert opened.result.success
    assert opened.result.identity_match is True
    assert opened.result.observed_queue_id == "queue-expected"
    assert opened.owned_context is not None
    assert opened.page is manager.contexts[0].page
    assert not manager.contexts[0].closed
    await opened.owned_context.close()
    await repository.close()


async def test_manual_open_identity_mismatch_closes_context_without_replacing_id(
    tmp_path: Path,
) -> None:
    restorer, expected, repository, _, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.TRANSFER_ONLY,
        [mismatch_result()],
    )

    opened = await restorer.restore_open(expected)

    assert not opened.result.success
    assert opened.result.failure is RestoreFailure.IDENTITY_MISMATCH
    assert opened.owned_context is None
    assert manager.contexts[0].closed
    persisted = await repository.get(expected.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    assert persisted.last_error == "restore:IDENTITY_MISMATCH"
    await repository.close()


async def test_manual_hybrid_open_refreshes_storage_state(tmp_path: Path) -> None:
    restorer, expected, repository, state_store, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [matching_result()],
    )

    opened = await restorer.restore_open(expected)

    assert opened.result.success
    assert opened.result.state_refreshed
    assert await state_store.load(expected.session_id) == {
        "cookies": [{"name": "refreshed"}],
        "origins": [],
    }
    assert opened.owned_context is not None
    assert not manager.contexts[0].closed
    await opened.owned_context.close()
    await repository.close()


async def test_manual_final_inspection_refreshes_hybrid_storage_state(tmp_path: Path) -> None:
    restorer, expected, repository, state_store, _, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [matching_result()],
    )
    context = FakeContext()

    result = await restorer.inspect_open(
        expected,
        context=cast(Any, context),
        page=cast(Any, context.page),
    )

    assert result.success
    assert result.state_refreshed
    assert await state_store.load(expected.session_id) == {
        "cookies": [{"name": "refreshed"}],
        "origins": [],
    }
    await repository.close()


async def test_transfer_only_failure_is_recorded_without_fallback(tmp_path: Path) -> None:
    restorer, expected, repository, _, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.TRANSFER_ONLY,
        [unavailable_result()],
    )

    result = await restorer.restore(expected)

    assert not result.success
    assert result.failure is RestoreFailure.TRANSFER_UNAVAILABLE
    assert len(result.attempts) == 1
    assert len(manager.contexts) == 1
    persisted = await repository.get(expected.session_id)
    assert persisted is not None
    assert persisted.last_error == "restore:TRANSFER_UNAVAILABLE"
    assert persisted.queue_id == "queue-expected"
    await repository.close()


async def test_navigation_timeout_is_sanitized_counted_and_context_closed(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    expected = session(SessionMode.TRANSFER_ONLY)
    await repository.create(expected)
    manager = TimeoutBrowserManager()
    metrics = PrometheusMetrics()
    restorer = QueueSessionRestorer(
        browser_manager=cast(BrowserManager, manager),
        repository=repository,
        state_store=FileSystemStateStore(tmp_path / "state"),
        observability=metrics,
    )

    result = await restorer.restore(expected)

    assert result.failure is RestoreFailure.NAVIGATION_FAILED
    assert metrics.registry.get_sample_value("navigation_timeouts_total") == 1
    assert manager.contexts[0].closed
    await repository.close()


async def test_transfer_identity_mismatch_is_observable_and_expected_id_is_unchanged(
    tmp_path: Path,
) -> None:
    restorer, expected, repository, _, _, _ = await setup_restorer(
        tmp_path,
        SessionMode.TRANSFER_ONLY,
        [mismatch_result()],
    )

    result = await restorer.restore(expected)

    assert not result.success
    assert result.failure is RestoreFailure.IDENTITY_MISMATCH
    assert result.identity_match is False
    assert result.expected_queue_id == "queue-expected"
    assert result.observed_queue_id == "queue-other"
    assert "queue-expected" not in repr(result)
    assert "queue-other" not in repr(result)
    persisted = await repository.get(expected.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    assert len(await repository.list()) == 1
    await repository.close()


async def test_hybrid_falls_back_to_storage_state(tmp_path: Path) -> None:
    restorer, expected, repository, state_store, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [unavailable_result(), matching_result()],
    )
    state = {"cookies": [], "origins": []}
    await state_store.save(expected.session_id, state)

    result = await restorer.restore(expected)

    assert result.success
    assert result.method is RestoreMethod.STORAGE_STATE
    assert result.state_refreshed
    assert [attempt.method for attempt in result.attempts] == [
        RestoreMethod.TRANSFER,
        RestoreMethod.STORAGE_STATE,
    ]
    assert manager.storage_states == [None, state]
    assert all(context.closed for context in manager.contexts)
    assert await state_store.load(expected.session_id) == {
        "cookies": [{"name": "refreshed"}],
        "origins": [],
    }
    await repository.close()


async def test_explicit_storage_only_restore_skips_transfer_attempt(tmp_path: Path) -> None:
    restorer, expected, repository, state_store, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [matching_result()],
        storage_navigation_url="https://staging.test/protected",
    )
    state = {"cookies": [], "origins": []}
    await state_store.save(expected.session_id, state)

    result = await restorer.restore_with_method(expected, RestoreMethod.STORAGE_STATE)

    assert result.success
    assert result.method is RestoreMethod.STORAGE_STATE
    assert [attempt.method for attempt in result.attempts] == [RestoreMethod.STORAGE_STATE]
    assert manager.storage_states == [state]
    assert manager.contexts[0].page.visited_urls == ["https://staging.test/protected"]
    assert manager.contexts[0].closed
    assert await state_store.load(expected.session_id) == state
    await repository.close()


async def test_hybrid_missing_state_file_is_reported(tmp_path: Path) -> None:
    restorer, expected, repository, _, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [unavailable_result()],
    )

    result = await restorer.restore(expected)

    assert not result.success
    assert result.method is RestoreMethod.STORAGE_STATE
    assert result.failure is RestoreFailure.STATE_MISSING
    assert len(manager.contexts) == 1
    await repository.close()


async def test_hybrid_corrupt_state_is_reported(tmp_path: Path) -> None:
    restorer, expected, repository, state_store, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [unavailable_result()],
    )
    state_store.directory.mkdir(parents=True)
    state_store.path_for(expected.session_id).write_text("not-json", encoding="utf-8")

    result = await restorer.restore(expected)

    assert not result.success
    assert result.failure is RestoreFailure.STATE_CORRUPT
    assert len(manager.contexts) == 1
    await repository.close()


async def test_hybrid_fallback_rejects_unexpected_queue_id(tmp_path: Path) -> None:
    restorer, expected, repository, state_store, manager, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [unavailable_result(), mismatch_result()],
    )
    await state_store.save(expected.session_id, {"cookies": [], "origins": []})

    result = await restorer.restore(expected)

    assert not result.success
    assert result.method is RestoreMethod.STORAGE_STATE
    assert result.failure is RestoreFailure.IDENTITY_MISMATCH
    assert result.identity_match is False
    assert result.observed_queue_id == "queue-other"
    assert all(context.closed for context in manager.contexts)
    persisted = await repository.get(expected.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    assert len(await repository.list()) == 1
    await repository.close()


class UnreadableStateStore(FileSystemStateStore):
    async def load(self, session_id: str) -> Any:
        raise StateUnreadableError(f"Could not read browser state for {session_id!r}")


class UnwritableStateStore(FileSystemStateStore):
    async def save(self, session_id: str, state: Any) -> Path:
        raise OSError("state volume unavailable")


async def test_state_store_interruption_is_transient_not_corrupt(tmp_path: Path) -> None:
    restorer, expected, repository, _, _, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [unavailable_result()],
    )
    restorer._state_store = UnreadableStateStore(tmp_path / "state")

    result = await restorer.restore(expected)

    assert not result.success
    assert result.failure is RestoreFailure.STATE_UNAVAILABLE
    persisted = await repository.get(expected.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    assert persisted.last_error == "restore:STATE_UNAVAILABLE"
    await repository.close()


async def test_state_refresh_failure_is_counted_separately_from_restore(tmp_path: Path) -> None:
    metrics = PrometheusMetrics()
    restorer, expected, repository, _, _, _ = await setup_restorer(
        tmp_path,
        SessionMode.HYBRID,
        [matching_result()],
    )
    restorer._state_store = UnwritableStateStore(tmp_path / "state")
    restorer._observability = metrics

    result = await restorer.restore(expected)

    assert result.failure is RestoreFailure.STATE_REFRESH_FAILED
    assert result.identity_match is True
    assert result.progress is not None
    assert metrics.registry.get_sample_value("state_refresh_failures_total") == 1
    assert metrics.registry.get_sample_value("transfer_restore_failures_total") == 0
    assert metrics.registry.get_sample_value("state_restore_failures_total") == 0
    await repository.close()


class HangingPageContext(FakeContext):
    async def new_page(self) -> FakePage:
        # Playwright can leave new_page pending forever after Chrome is killed.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class HangingBrowserManager(FakeBrowserManager):
    @asynccontextmanager
    async def context(self, *, storage_state: object | None = None):
        context = HangingPageContext()
        self.contexts.append(context)
        try:
            yield context
        finally:
            context.closed = True


async def test_hung_browser_call_is_bounded_and_context_released(tmp_path: Path) -> None:
    metrics = PrometheusMetrics()
    restorer, expected, repository, _, _, _ = await setup_restorer(
        tmp_path,
        SessionMode.TRANSFER_ONLY,
        [matching_result()],
    )
    manager = HangingBrowserManager()
    restorer._browser_manager = cast(BrowserManager, manager)
    restorer._attempt_timeout_seconds = 0.05
    restorer._observability = metrics

    result = await asyncio.wait_for(restorer.restore(expected), timeout=2)

    assert result.failure is RestoreFailure.NAVIGATION_FAILED
    assert all(context.closed for context in manager.contexts)
    assert metrics.registry.get_sample_value("browser_operation_timeouts_total") == 1
    persisted = await repository.get(expected.session_id)
    assert persisted is not None and persisted.queue_id == "queue-expected"
    await repository.close()
