import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import QueueIdConflictError, SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationRetryPolicy,
    CreationWorkItem,
    QueueSessionCreator,
    SessionCreationController,
    TransientCreationError,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import TransferExtractionResult


@dataclass(frozen=True)
class ScriptedStep:
    kind: CreationOutcomeKind
    queue_id: str | None = None
    attempts: int = 1
    temporary_failures: int = 0


class ScriptedCreationHandler:
    def __init__(
        self,
        repository: SQLiteSessionRepository,
        steps: list[ScriptedStep],
    ) -> None:
        self.repository = repository
        self.steps = deque(steps)
        self.active = 0
        self.maximum_active = 0
        self.calls = 0
        self._lock = asyncio.Lock()

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        async with self._lock:
            step = self.steps.popleft()
            self.calls += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.001)
            if step.kind is CreationOutcomeKind.SUCCESS:
                session = QueueSession(
                    session_id=work_item.session_id,
                    queue_id=step.queue_id,
                    transfer_url=f"https://queue.test/journey?q={step.queue_id}",
                    mode=SessionMode.HYBRID,
                    status=QueueStatus.PARKED,
                    state_path=Path(f".browser-state/{work_item.session_id}.json"),
                    attempt_count=step.attempts,
                )
                try:
                    await self.repository.create(session)
                except QueueIdConflictError:
                    await self._persist_failed(work_item, step, "duplicate_queue_id")
                    return CreationOutcome(
                        kind=CreationOutcomeKind.DUPLICATE,
                        attempts=step.attempts,
                        temporary_failures=step.temporary_failures,
                        duration_seconds=0.001,
                    )
                return CreationOutcome(
                    kind=CreationOutcomeKind.SUCCESS,
                    attempts=step.attempts,
                    temporary_failures=step.temporary_failures,
                    duration_seconds=0.001,
                    session=session,
                )

            await self._persist_failed(work_item, step, step.kind.value.casefold())
            return CreationOutcome(
                kind=step.kind,
                attempts=step.attempts,
                temporary_failures=step.temporary_failures,
                duration_seconds=0.001,
            )
        finally:
            self.active -= 1

    async def _persist_failed(
        self,
        work_item: CreationWorkItem,
        step: ScriptedStep,
        error: str,
    ) -> None:
        await self.repository.create(
            QueueSession(
                session_id=work_item.session_id,
                queue_id=None,
                transfer_url="",
                mode=SessionMode.HYBRID,
                status=QueueStatus.FAILED,
                state_path=Path(f".browser-state/{work_item.session_id}.json"),
                attempt_count=step.attempts,
                last_error=error,
            )
        )


async def test_target_one_replenishes_after_failure(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "target-one.sqlite3")
    handler = ScriptedCreationHandler(
        repository,
        [
            ScriptedStep(
                CreationOutcomeKind.TEMPORARY_FAILURE,
                attempts=2,
                temporary_failures=2,
            ),
            ScriptedStep(CreationOutcomeKind.SUCCESS, queue_id="queue-1"),
        ],
    )
    controller = SessionCreationController(
        repository=repository,
        handler=handler,
        target_queue_ids=1,
        worker_count=5,
        queue_capacity=2,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 1
    assert metrics.attempts == 3
    assert metrics.temporary_failures == 2
    assert metrics.currently_creating == 0
    assert handler.calls == 2
    assert handler.maximum_active == 1
    assert await repository.count_successful_queue_ids() == 1
    await repository.close()


async def test_already_satisfied_target_schedules_no_new_work(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "already-satisfied.sqlite3")
    await repository.create(
        QueueSession(
            session_id="existing",
            queue_id="existing-queue",
            transfer_url="https://queue.test/journey?q=existing-queue",
            mode=SessionMode.HYBRID,
            status=QueueStatus.PARKED,
            state_path=Path(".browser-state/existing.json"),
        )
    )
    handler = ScriptedCreationHandler(repository, [])
    controller = SessionCreationController(
        repository=repository,
        handler=handler,
        target_queue_ids=1,
        worker_count=5,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 1
    assert metrics.attempts == 0
    assert handler.calls == 0
    await repository.close()


def test_exponential_backoff_is_bounded_and_jittered() -> None:
    policy = CreationRetryPolicy(
        max_attempts=5,
        initial_backoff_seconds=1,
        maximum_backoff_seconds=4,
        jitter_seconds=0.5,
    )

    assert [policy.delay(attempt, lambda _start, end: end) for attempt in range(1, 5)] == [
        1.5,
        2.5,
        4.5,
        4.5,
    ]


async def test_target_ten_ignores_duplicates_and_failures(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "target-ten.sqlite3")
    steps = [
        ScriptedStep(CreationOutcomeKind.SUCCESS, queue_id="same-queue"),
        ScriptedStep(CreationOutcomeKind.SUCCESS, queue_id="same-queue"),
        ScriptedStep(
            CreationOutcomeKind.TEMPORARY_FAILURE,
            attempts=2,
            temporary_failures=2,
        ),
        ScriptedStep(CreationOutcomeKind.PERMANENT_FAILURE),
        *(
            ScriptedStep(CreationOutcomeKind.SUCCESS, queue_id=f"queue-{index}")
            for index in range(2, 11)
        ),
    ]
    handler = ScriptedCreationHandler(repository, steps)
    controller = SessionCreationController(
        repository=repository,
        handler=handler,
        target_queue_ids=10,
        worker_count=5,
        queue_capacity=5,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 10
    assert metrics.duplicates == 1
    assert metrics.temporary_failures == 2
    assert metrics.permanent_failures == 1
    assert metrics.currently_creating == 0
    assert 1 < handler.maximum_active <= 5
    assert handler.calls == 13
    assert await repository.count_successful_queue_ids() == 10
    assert len(await repository.list(QueueStatus.FAILED)) == 3
    await repository.close()


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(self, status: int) -> None:
        self.url = "https://queue.staging.test/journey"
        self.status = status

    async def goto(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(self.status)


class FakeContext:
    def __init__(self, status: int) -> None:
        self.page = FakePage(status)
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def storage_state(self) -> dict[str, object]:
        return {"cookies": [{"name": "queue", "value": "secret"}], "origins": []}


class FakeBrowserManager:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = deque(statuses)
        self.contexts: list[FakeContext] = []

    @asynccontextmanager
    async def context(self):
        context = FakeContext(self.statuses.popleft())
        self.contexts.append(context)
        try:
            yield context
        finally:
            context.closed = True


class RetryThenLiveExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, _: object, *, session_id: str) -> QueueProgress:
        self.calls += 1
        if self.calls == 1:
            raise TransientCreationError("temporary_queue_page")
        return QueueProgress(session_id=session_id, pre_queue=True)


class LiveExtractor:
    async def extract(self, _: object, *, session_id: str) -> QueueProgress:
        return QueueProgress(session_id=session_id, active_queue=True)


class SuccessfulTransferExtractor:
    async def extract(
        self,
        _: object,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult:
        del expected_queue_id
        return TransferExtractionResult(
            transfer_url="https://queue.staging.test/journey?q=queue-created",
            observed_queue_id="queue-created",
        )


async def no_wait(_: float) -> None:
    return None


async def test_hybrid_creator_retries_saves_state_persists_and_closes(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "creator.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    browser_manager = FakeBrowserManager([200, 200])
    live_extractor = RetryThenLiveExtractor()
    creator = QueueSessionCreator(
        browser_manager=cast(BrowserManager, browser_manager),
        repository=repository,
        state_store=state_store,
        staging_url="https://staging.test",
        state_directory=state_store.directory,
        mode=SessionMode.HYBRID,
        live_extractor=cast(Any, live_extractor),
        transfer_extractor_factory=lambda _: SuccessfulTransferExtractor(),
        retry_policy=CreationRetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0,
            maximum_backoff_seconds=0,
            jitter_seconds=0,
        ),
        sleep=no_wait,
        jitter=lambda _start, _end: 0,
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="created"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    assert outcome.attempts == 2
    assert outcome.temporary_failures == 1
    assert all(context.closed for context in browser_manager.contexts)
    persisted = await repository.get("created")
    assert persisted is not None
    assert persisted.status is QueueStatus.PARKED
    assert persisted.queue_id == "queue-created"
    assert persisted.attempt_count == 2
    assert await state_store.load("created") is not None
    await repository.close()


async def test_permanent_navigation_failure_is_recorded_and_context_closed(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "failed.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    browser_manager = FakeBrowserManager([404])
    creator = QueueSessionCreator(
        browser_manager=cast(BrowserManager, browser_manager),
        repository=repository,
        state_store=state_store,
        staging_url="https://staging.test",
        state_directory=state_store.directory,
        mode=SessionMode.HYBRID,
        retry_policy=CreationRetryPolicy(max_attempts=1),
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="failed"))

    assert outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE
    assert browser_manager.contexts[0].closed
    persisted = await repository.get("failed")
    assert persisted is not None
    assert persisted.status is QueueStatus.FAILED
    assert persisted.queue_id is None
    assert persisted.last_error == "permanent_http_response"
    assert await repository.count_successful_queue_ids() == 0
    await repository.close()


async def test_temporary_server_failure_retries_without_transfer_only_state(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "transfer-only.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    browser_manager = FakeBrowserManager([503, 200])
    creator = QueueSessionCreator(
        browser_manager=cast(BrowserManager, browser_manager),
        repository=repository,
        state_store=state_store,
        staging_url="https://staging.test",
        state_directory=state_store.directory,
        mode=SessionMode.TRANSFER_ONLY,
        live_extractor=cast(Any, LiveExtractor()),
        transfer_extractor_factory=lambda _: SuccessfulTransferExtractor(),
        retry_policy=CreationRetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0,
            maximum_backoff_seconds=0,
            jitter_seconds=0,
        ),
        sleep=no_wait,
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="transfer-only"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    assert outcome.attempts == 2
    assert outcome.temporary_failures == 1
    assert all(context.closed for context in browser_manager.contexts)
    assert await state_store.load("transfer-only") is None
    persisted = await repository.get("transfer-only")
    assert persisted is not None
    assert persisted.mode is SessionMode.TRANSFER_ONLY
    await repository.close()
