import asyncio
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Error as PlaywrightError

from queue_load_test.browser import BrowserManager
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.queue_monitor import (
    RenderedAccessRestrictionDetector,
    is_access_restricted_text,
    normalize_page_text,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    AccessRestrictedError,
    AcquisitionFailure,
    CreationOutcomeKind,
    CreationRetryPolicy,
    CreationWorkItem,
    QueueSessionCreator,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import TransferExtractionResult

RESTRICTED = "We are sorry, your access has been restricted"
QUEUE_PAGE = "You are now in line. Your number in line: 1234. Users in line ahead of you: 12"
CODE = AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.code
FIXTURES = Path(__file__).parents[1] / "fixtures" / "queue_it"


# --- Detector -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        RESTRICTED,
        RESTRICTED.upper(),
        "  we are   sorry,\n your access\thas been restricted.  ",
        "We are sorry your access has been restricted!",
        "Header\nWe are sorry, your access has been restricted\nReference 123",
    ],
)
def test_restriction_text_is_detected_case_and_whitespace_insensitively(text: str) -> None:
    assert is_access_restricted_text(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        QUEUE_PAGE,
        "The event will begin soon",
        "Access denied",
        "403 Forbidden",
        "This site can't be reached",
        "We are sorry, something went wrong",
        "Your access has been granted",
        "We are sorry, your access has been restored",
    ],
)
def test_unrelated_pages_are_not_classified_as_restricted(text: str | None) -> None:
    assert is_access_restricted_text(text) is False


def test_queue_it_fixtures_are_not_classified_as_restricted() -> None:
    for fixture in FIXTURES.glob("*.html"):
        if fixture.name == "access_restricted.html":
            continue
        assert not is_access_restricted_text(fixture.read_text(encoding="utf-8")), fixture.name


def test_normalization_folds_punctuation_and_whitespace() -> None:
    assert normalize_page_text(" We’re  SORRY,\n") == "we re sorry"


class _RaisingLocator:
    first: "_RaisingLocator"

    def __init__(self) -> None:
        self.first = self

    async def inner_text(self, **_: object) -> str:
        raise PlaywrightError("Target page, context or browser has been closed")


class _RaisingPage:
    def locator(self, _selector: str) -> _RaisingLocator:
        return _RaisingLocator()


async def test_unreadable_page_is_not_classified_as_restricted() -> None:
    detector = RenderedAccessRestrictionDetector()

    assert await detector.detect(cast(Any, _RaisingPage())) is False


def test_restricted_error_uses_the_typed_failure_code() -> None:
    error = AccessRestrictedError()

    assert error.code == "access_restricted_before_queue"
    assert CODE == error.code


# --- Creator ------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status


class _Body:
    def __init__(self, page: "_Page") -> None:
        self._page = page
        self.first = self

    async def inner_text(self, **_: object) -> str:
        return await self._page.render()


class _Page:
    """A page whose rendered body text can change between observations."""

    def __init__(self, status: int, bodies: list[str], *, goto_error: bool = False) -> None:
        self.url = "https://queue.staging.test/journey"
        self.status = status
        self.bodies = deque(bodies)
        self.goto_error = goto_error
        self.render_gate: asyncio.Event | None = None
        self.rendering = asyncio.Event()

    async def goto(self, *_: object, **__: object) -> _Response:
        if self.goto_error:
            raise PlaywrightError("net::ERR_CONNECTION_RESET")
        return _Response(self.status)

    def locator(self, _selector: str) -> _Body:
        return _Body(self)

    async def render(self) -> str:
        self.rendering.set()
        if self.render_gate is not None:
            await self.render_gate.wait()
        return self.bodies.popleft() if len(self.bodies) > 1 else self.bodies[0]

    @property
    def restricted(self) -> bool:
        return is_access_restricted_text(self.bodies[0])


class _Context:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> _Page:
        return self.page

    async def storage_state(self) -> dict[str, object]:
        return {"cookies": [], "origins": []}


class _BrowserManager:
    def __init__(self, pages: list[_Page]) -> None:
        self.pages = deque(pages)
        self.contexts: list[_Context] = []
        self.active = 0
        self.maximum_active = 0

    def report_navigation(self, context: object, *, responsive: bool) -> None:
        """Navigation health is irrelevant to this fake."""

    @asynccontextmanager
    async def context(self):  # type: ignore[no-untyped-def]
        context = _Context(self.pages.popleft())
        self.contexts.append(context)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            yield context
        finally:
            context.closed = True
            self.active -= 1


class _LiveExtractor:
    """Reports a live queue only when the page is not the restriction page."""

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, page: _Page, *, session_id: str) -> QueueProgress:
        self.calls += 1
        if page.restricted:
            return QueueProgress(session_id=session_id)
        return QueueProgress(session_id=session_id, active_queue=True)


class _TransferExtractor:
    def __init__(self, queue_ids: deque[str]) -> None:
        self._queue_ids = queue_ids

    async def extract(
        self,
        _: object,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult:
        del expected_queue_id
        queue_id = self._queue_ids.popleft()
        return TransferExtractionResult(
            transfer_url=f"https://queue.staging.test/journey?q={queue_id}",
            observed_queue_id=queue_id,
        )


async def _no_wait(_: float) -> None:
    return None


def _creator(
    tmp_path: Path,
    repository: SQLiteSessionRepository,
    manager: _BrowserManager,
    *,
    queue_ids: list[str] | None = None,
    live_extractor: _LiveExtractor | None = None,
    max_attempts: int = 3,
    observability: PrometheusMetrics | None = None,
    mode: SessionMode = SessionMode.HYBRID,
) -> tuple[QueueSessionCreator, FileSystemStateStore]:
    state_store = FileSystemStateStore(tmp_path / "state")
    ids = deque(queue_ids or [])
    creator = QueueSessionCreator(
        browser_manager=cast(BrowserManager, manager),
        repository=repository,
        state_store=state_store,
        staging_url="https://staging.test",
        state_directory=state_store.directory,
        mode=mode,
        live_extractor=cast(Any, live_extractor or _LiveExtractor()),
        transfer_extractor_factory=lambda _: _TransferExtractor(ids),
        retry_policy=CreationRetryPolicy(
            max_attempts=max_attempts,
            initial_backoff_seconds=0,
            maximum_backoff_seconds=0,
            jitter_seconds=0,
        ),
        live_page_timeout_seconds=5,
        sleep=_no_wait,
        jitter=lambda _start, _end: 0,
        observability=observability,
    )
    return creator, state_store


def _state_files(state_store: FileSystemStateStore) -> list[Path]:
    if not state_store.directory.exists():
        return []
    return [path for path in state_store.directory.iterdir() if path.is_file()]


async def test_restriction_before_queue_id_fails_attempt_without_persisting_identity(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "restricted.sqlite3")
    manager = _BrowserManager([_Page(200, [RESTRICTED])])
    live_extractor = _LiveExtractor()
    observability = PrometheusMetrics()
    creator, state_store = _creator(
        tmp_path,
        repository,
        manager,
        live_extractor=live_extractor,
        observability=observability,
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="restricted"))

    assert outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE
    assert outcome.failure_code == CODE
    assert outcome.session is None
    # Not retried within the work item: the controller schedules replacement work.
    assert outcome.attempts == 1
    assert len(manager.contexts) == 1
    assert manager.contexts[0].closed
    assert manager.active == 0
    # Classified before the page is ever observed as a Queue-it journey.
    assert live_extractor.calls == 0
    persisted = await repository.get("restricted")
    assert persisted is not None
    assert persisted.status is QueueStatus.FAILED
    assert persisted.queue_id is None
    assert persisted.transfer_url == ""
    assert persisted.last_error == CODE
    assert await repository.count_successful_queue_ids() == 0
    assert await repository.count_lost_queue_ids() == 0
    assert _state_files(state_store) == []
    sample = observability.registry.get_sample_value
    assert sample("queue_creation_access_restricted_total") == 1
    assert sample("queue_creation_permanent_failures_total") == 1
    assert (sample("queue_ids_acquired_total") or 0) == 0
    await repository.close()


async def test_restriction_with_error_status_is_classified_from_rendered_page(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "restricted-403.sqlite3")
    manager = _BrowserManager([_Page(403, [RESTRICTED])])
    creator, _ = _creator(tmp_path, repository, manager)

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="restricted"))

    assert outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE
    assert outcome.failure_code == CODE
    assert manager.contexts[0].closed
    await repository.close()


async def test_plain_error_status_keeps_its_existing_classification(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "plain-403.sqlite3")
    manager = _BrowserManager([_Page(403, ["403 Forbidden"])])
    observability = PrometheusMetrics()
    creator, _ = _creator(tmp_path, repository, manager, observability=observability)

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="forbidden"))

    assert outcome.failure_code == "permanent_http_response"
    assert (
        observability.registry.get_sample_value("queue_creation_access_restricted_total") == 0
    )
    await repository.close()


async def test_restriction_rendered_after_navigation_is_detected_on_a_later_poll(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "late-restriction.sqlite3")
    # First render is a blank loading shell; the restriction appears afterwards.
    page = _Page(200, ["Loading", RESTRICTED])
    manager = _BrowserManager([page])

    class _NotReadyExtractor(_LiveExtractor):
        async def extract(self, page: _Page, *, session_id: str) -> QueueProgress:
            self.calls += 1
            return QueueProgress(session_id=session_id)

    live_extractor = _NotReadyExtractor()
    creator, _ = _creator(tmp_path, repository, manager, live_extractor=live_extractor)

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="late"))

    assert outcome.failure_code == CODE
    assert live_extractor.calls == 1
    assert manager.contexts[0].closed
    assert await repository.count_successful_queue_ids() == 0
    await repository.close()


async def test_ordinary_queue_page_is_not_classified_as_restricted(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "queue.sqlite3")
    manager = _BrowserManager([_Page(200, [QUEUE_PAGE])])
    creator, state_store = _creator(tmp_path, repository, manager, queue_ids=["queue-1"])

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="queued"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    assert outcome.failure_code is None
    assert await repository.count_successful_queue_ids() == 1
    assert await state_store.load("queued") is not None
    await repository.close()


async def test_generic_navigation_error_keeps_existing_transient_retry(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "nav-error.sqlite3")
    manager = _BrowserManager(
        [_Page(200, [QUEUE_PAGE], goto_error=True), _Page(200, [QUEUE_PAGE])]
    )
    observability = PrometheusMetrics()
    creator, _ = _creator(
        tmp_path,
        repository,
        manager,
        queue_ids=["queue-1"],
        observability=observability,
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="retried"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    assert outcome.attempts == 2
    assert outcome.temporary_failures == 1
    assert all(context.closed for context in manager.contexts)
    assert (
        observability.registry.get_sample_value("queue_creation_access_restricted_total") == 0
    )
    await repository.close()


async def test_generic_navigation_error_exhaustion_is_not_access_restricted(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "nav-exhausted.sqlite3")
    manager = _BrowserManager([_Page(200, [RESTRICTED], goto_error=True)])
    creator, _ = _creator(tmp_path, repository, manager, max_attempts=1)

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="nav"))

    # The page never rendered, so even restriction-like text cannot be inspected.
    assert outcome.kind is CreationOutcomeKind.TEMPORARY_FAILURE
    assert outcome.failure_code == "transient_browser_error"
    await repository.close()


async def test_cancellation_during_restriction_detection_leaks_no_context_or_state(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "cancelled.sqlite3")
    page = _Page(200, [RESTRICTED])
    page.render_gate = asyncio.Event()
    manager = _BrowserManager([page])
    creator, state_store = _creator(tmp_path, repository, manager)

    task = asyncio.create_task(creator.create(CreationWorkItem(sequence=1, session_id="c")))
    await asyncio.wait_for(page.rendering.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.contexts[0].closed
    assert manager.active == 0
    assert _state_files(state_store) == []
    assert await repository.count_successful_queue_ids() == 0
    await repository.close()


# --- Controller ---------------------------------------------------------------------


async def _seed(repository: SQLiteSessionRepository, count: int) -> list[QueueSession]:
    seeded = []
    for index in range(count):
        seeded.append(
            await repository.create(
                QueueSession(
                    session_id=f"existing-{index}",
                    queue_id=f"existing-queue-{index}",
                    transfer_url=f"https://queue.staging.test/journey?q=existing-queue-{index}",
                    mode=SessionMode.HYBRID,
                    status=QueueStatus.PARKED,
                    state_path=Path(f".browser-state/existing-{index}.json"),
                )
            )
        )
    return seeded


async def test_controller_replaces_restricted_attempts_until_target_without_overshoot(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "controller.sqlite3")
    seeded = await _seed(repository, 2)
    pages = [
        _Page(200, [RESTRICTED]),
        _Page(200, [QUEUE_PAGE]),
        _Page(403, [RESTRICTED]),
        _Page(200, [RESTRICTED]),
        *(_Page(200, [QUEUE_PAGE]) for _ in range(10)),
    ]
    manager = _BrowserManager(pages)
    creator, _ = _creator(
        tmp_path,
        repository,
        manager,
        queue_ids=[f"queue-{index}" for index in range(10)],
    )
    controller = SessionCreationController(
        repository=repository,
        handler=creator,
        target_queue_ids=6,
        worker_count=3,
        queue_capacity=3,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 6
    assert metrics.unique_ids_acquired == 4
    assert metrics.access_restricted == 3
    assert metrics.permanent_failures == 3
    assert await repository.count_successful_queue_ids() == 6
    # Each work item uses exactly one context; no extra work beyond the deficit.
    assert len(manager.contexts) == metrics.completed_work_items == 7
    assert all(context.closed for context in manager.contexts)
    assert manager.active == 0
    assert manager.maximum_active <= 3
    assert metrics.currently_creating == 0
    failed = await repository.list(QueueStatus.FAILED)
    assert len(failed) == 3
    assert all(row.queue_id is None and row.last_error == CODE for row in failed)
    # Successful identities are never deleted or replaced by a restricted attempt.
    for original in seeded:
        current = await repository.get(original.session_id)
        assert current is not None
        assert current.queue_id == original.queue_id
        assert current.status is QueueStatus.PARKED
    await repository.close()


async def test_restricted_attempts_near_target_cannot_overshoot(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "near-target.sqlite3")
    await _seed(repository, 4)
    pages = [_Page(200, [RESTRICTED]) for _ in range(5)] + [_Page(200, [QUEUE_PAGE])]
    manager = _BrowserManager(pages)
    creator, _ = _creator(tmp_path, repository, manager, queue_ids=["queue-last"])
    controller = SessionCreationController(
        repository=repository,
        handler=creator,
        target_queue_ids=5,
        worker_count=10,
        queue_capacity=10,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 5
    assert metrics.access_restricted == 5
    # A deficit of one keeps exactly one attempt in flight at a time.
    assert manager.maximum_active == 1
    assert metrics.maximum_concurrent_creating == 1
    assert await repository.count_successful_queue_ids() == 5
    assert manager.pages == deque()
    await repository.close()


async def test_duplicates_and_restrictions_are_counted_separately(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "duplicates.sqlite3")
    pages = [
        _Page(200, [QUEUE_PAGE]),
        _Page(200, [QUEUE_PAGE]),
        _Page(200, [RESTRICTED]),
        _Page(200, [QUEUE_PAGE]),
    ]
    manager = _BrowserManager(pages)
    creator, state_store = _creator(
        tmp_path,
        repository,
        manager,
        queue_ids=["same", "same", "other"],
    )
    controller = SessionCreationController(
        repository=repository,
        handler=creator,
        target_queue_ids=2,
        worker_count=1,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 2
    assert metrics.duplicates == 1
    assert metrics.access_restricted == 1
    assert metrics.permanent_failures == 1
    failed = {row.last_error for row in await repository.list(QueueStatus.FAILED)}
    assert failed == {"duplicate_queue_id", CODE}
    # Only the two successful identities keep browser state.
    assert len(_state_files(state_store)) == 2
    await repository.close()


async def test_controller_cancellation_during_restricted_attempts_leaks_no_context(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "controller-cancel.sqlite3")
    pages = [_Page(200, [RESTRICTED]) for _ in range(3)]
    for page in pages:
        page.render_gate = asyncio.Event()
    manager = _BrowserManager(pages)
    creator, state_store = _creator(tmp_path, repository, manager)
    controller = SessionCreationController(
        repository=repository,
        handler=creator,
        target_queue_ids=100,
        worker_count=3,
        queue_capacity=3,
    )

    run_task = asyncio.create_task(controller.run())
    for page in pages:
        await asyncio.wait_for(page.rendering.wait(), timeout=1)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_task, timeout=1)

    assert len(manager.contexts) == 3
    assert all(context.closed for context in manager.contexts)
    assert manager.active == 0
    assert controller.metrics.currently_creating == 0
    assert _state_files(state_store) == []
    assert await repository.count_successful_queue_ids() == 0
    await repository.close()
