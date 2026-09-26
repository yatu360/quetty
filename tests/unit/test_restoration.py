from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore
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


class FakeBrowserManager:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.storage_states: list[object | None] = []

    @asynccontextmanager
    async def context(self, *, storage_state: object | None = None):
        context = FakeContext()
        self.contexts.append(context)
        self.storage_states.append(storage_state)
        try:
            yield context
        finally:
            context.closed = True


class LiveExtractor:
    async def extract(self, _: object, *, session_id: str) -> QueueProgress:
        return QueueProgress(
            session_id=session_id,
            queue_number="123",
            users_ahead=12,
            active_queue=True,
        )


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
        live_extractor=cast(Any, LiveExtractor()),
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
    await repository.close()
