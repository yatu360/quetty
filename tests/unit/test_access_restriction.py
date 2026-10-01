import asyncio
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import Error as PlaywrightError

from queue_load_test.browser import BrowserManager
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.metrics.logging import JsonLogFormatter
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


# --- Hardening: pacing, halting, cleanup, observability, restart --------------------


class _EndlessBrowserManager(_BrowserManager):
    """Serves ``successes`` queue pages, then restriction pages without end.

    Sets ``stop_event`` once ``stop_after`` restricted contexts have been created, so
    a test can bound an otherwise unbounded restriction stream.
    """

    def __init__(
        self,
        *,
        successes: int = 0,
        stop_after: int | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__([])
        self._successes = successes
        self._stop_after = stop_after
        self._stop_event = stop_event
        self.restricted_contexts = 0

    @asynccontextmanager
    async def context(self):  # type: ignore[no-untyped-def,override]
        if self._successes > 0:
            self._successes -= 1
            self.pages.append(_Page(200, [QUEUE_PAGE]))
        else:
            self.restricted_contexts += 1
            self.pages.append(_Page(200, [RESTRICTED]))
            if (
                self._stop_event is not None
                and self._stop_after is not None
                and self.restricted_contexts >= self._stop_after
            ):
                self._stop_event.set()
        async with super().context() as context:
            yield context


def _controller(
    repository: SQLiteSessionRepository,
    creator: QueueSessionCreator,
    *,
    target: int,
    workers: int = 1,
    capacity: int | None = None,
    observability: PrometheusMetrics | None = None,
) -> SessionCreationController:
    return SessionCreationController(
        repository=repository,
        handler=creator,
        target_queue_ids=target,
        worker_count=workers,
        queue_capacity=capacity,
        observability=observability,
    )


async def test_restrictions_then_success_are_replaced_immediately_and_reset_the_streak(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "streak.sqlite3")
    pages = [_Page(200, [RESTRICTED]) for _ in range(4)] + [
        _Page(200, [QUEUE_PAGE]),
        _Page(200, [RESTRICTED]),
        _Page(200, [QUEUE_PAGE]),
    ]
    manager = _BrowserManager(pages)
    observability = PrometheusMetrics()
    creator, _ = _creator(
        tmp_path, repository, manager, queue_ids=["q-1", "q-2"], observability=observability
    )
    controller = _controller(repository, creator, target=2, observability=observability)

    metrics = await asyncio.wait_for(controller.run(), timeout=5)

    # Target counts only persisted unique Queue IDs, never restricted attempts.
    assert metrics.successful_unique_ids == 2
    assert await repository.count_successful_queue_ids() == 2
    assert metrics.access_restricted == 5
    assert metrics.consecutive_access_restricted == 0
    assert creator.access_restricted_attempts == 5
    # Each restricted work item was replaced by a new work item with its own context.
    assert len(manager.contexts) == metrics.completed_work_items == 7
    assert all(context.closed for context in manager.contexts)
    sample = observability.registry.get_sample_value
    assert sample("queue_creation_access_restricted_total") == 5
    assert sample("queue_creation_access_restricted_consecutive") == 0
    await repository.close()


async def test_replacement_starts_without_any_controller_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "no-delay.sqlite3")
    manager = _BrowserManager(
        [_Page(200, [RESTRICTED]) for _ in range(5)] + [_Page(200, [QUEUE_PAGE])]
    )
    creator, _ = _creator(tmp_path, repository, manager, queue_ids=["q-1"])
    controller = _controller(repository, creator, target=1)
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float, *args: Any, **kwargs: Any) -> Any:
        slept.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    metrics = await asyncio.wait_for(controller.run(), timeout=5)

    assert metrics.successful_unique_ids == 1
    assert metrics.access_restricted == 5
    # The fakes sleep for 0 s at most; the controller itself never waits.
    assert all(delay == 0 for delay in slept)
    await repository.close()


async def test_persistent_restriction_keeps_retrying_until_success(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "persistent.sqlite3")
    await _seed(repository, 1)
    restricted = 25
    manager = _BrowserManager(
        [_Page(200, [RESTRICTED]) for _ in range(restricted)] + [_Page(200, [QUEUE_PAGE])]
    )
    observability = PrometheusMetrics()
    creator, state_store = _creator(
        tmp_path, repository, manager, queue_ids=["q-late"], observability=observability
    )
    controller = _controller(repository, creator, target=2, observability=observability)

    metrics = await asyncio.wait_for(controller.run(), timeout=5)

    assert metrics.successful_unique_ids == 2
    assert metrics.access_restricted == restricted
    assert len(manager.contexts) == restricted + 1
    assert manager.maximum_active == 1
    assert manager.active == 0
    assert len(_state_files(state_store)) == 1
    sample = observability.registry.get_sample_value
    assert sample("queue_creation_access_restricted_total") == restricted
    assert sample("queue_creation_access_restricted_consecutive") == 0
    await repository.close()


async def test_concurrent_restricted_workers_stay_bounded(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "bounded.sqlite3")
    manager = _BrowserManager(
        [_Page(200, [RESTRICTED]) for _ in range(20)]
        + [_Page(200, [QUEUE_PAGE]) for _ in range(10)]
    )
    creator, _ = _creator(
        tmp_path, repository, manager, queue_ids=[f"q-{n}" for n in range(10)]
    )
    controller = _controller(repository, creator, target=3, workers=3, capacity=3)

    metrics = await asyncio.wait_for(controller.run(), timeout=5)

    assert metrics.successful_unique_ids == 3
    assert await repository.count_successful_queue_ids() == 3
    assert metrics.access_restricted == 20
    assert manager.maximum_active <= 3
    assert metrics.maximum_queue_depth <= 3
    assert metrics.currently_creating == 0
    assert all(context.closed for context in manager.contexts)
    await repository.close()


async def test_graceful_stop_during_endless_restrictions_is_responsive(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "stop.sqlite3")
    stop_event = asyncio.Event()
    manager = _EndlessBrowserManager(stop_after=10, stop_event=stop_event)
    creator, state_store = _creator(tmp_path, repository, manager)
    controller = _controller(repository, creator, target=5, workers=3, capacity=3)

    metrics = await asyncio.wait_for(controller.run(stop_event), timeout=2)

    # Only already-issued work drains after the stop.
    assert 10 <= len(manager.contexts) <= 10 + 3
    assert metrics.access_restricted == len(manager.contexts)
    assert metrics.consecutive_access_restricted == len(manager.contexts)
    assert metrics.successful_unique_ids == 0
    assert manager.maximum_active <= 3
    assert manager.active == 0
    assert metrics.currently_creating == 0
    assert _state_files(state_store) == []
    await repository.close()


async def test_restriction_removes_uncommitted_state_and_takes_no_lease(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "cleanup.sqlite3")
    seeded = await _seed(repository, 1)
    manager = _BrowserManager([_Page(200, [RESTRICTED])])
    creator, state_store = _creator(tmp_path, repository, manager)
    # A leftover uncommitted document under the work item's own session ID.
    await state_store.save("restricted", {"cookies": [], "origins": []})

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="restricted"))

    assert outcome.failure_code == CODE
    assert await state_store.load("restricted") is None
    assert _state_files(state_store) == []
    persisted = await repository.get("restricted")
    assert persisted is not None
    assert persisted.status is QueueStatus.FAILED
    assert persisted.worker_id is None and persisted.lease_until is None
    now = datetime.now(UTC)
    claimed = await repository.claim_due_sessions(
        worker_id="monitor-test",
        now=now + timedelta(days=1),
        lease_until=now + timedelta(days=2),
        limit=10,
    )
    assert "restricted" not in {session.session_id for session in claimed}
    existing = await repository.get(seeded[0].session_id)
    assert existing is not None and existing.queue_id == seeded[0].queue_id
    await repository.close()


class _ExplodingExtractor(_LiveExtractor):
    async def extract(self, page: _Page, *, session_id: str) -> QueueProgress:
        raise RuntimeError("unexpected extractor bug")


@pytest.mark.parametrize("scenario", ["success", "restriction", "exception", "cancellation"])
async def test_context_is_released_on_every_outcome(tmp_path: Path, scenario: str) -> None:
    repository = SQLiteSessionRepository(tmp_path / f"{scenario}.sqlite3")
    page = _Page(200, [RESTRICTED if scenario in {"restriction", "cancellation"} else QUEUE_PAGE])
    if scenario == "cancellation":
        page.render_gate = asyncio.Event()
    manager = _BrowserManager([page])
    creator, state_store = _creator(
        tmp_path,
        repository,
        manager,
        queue_ids=["q-1"],
        live_extractor=_ExplodingExtractor() if scenario == "exception" else None,
    )
    work_item = CreationWorkItem(sequence=1, session_id=scenario)

    if scenario == "exception":
        with pytest.raises(RuntimeError):
            await creator.create(work_item)
    elif scenario == "cancellation":
        task = asyncio.create_task(creator.create(work_item))
        await asyncio.wait_for(page.rendering.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await creator.create(work_item)

    assert manager.contexts[0].closed
    assert manager.active == 0
    has_state = await state_store.load(scenario) is not None
    assert has_state is (scenario == "success")
    assert await repository.count_successful_queue_ids() == (1 if scenario == "success" else 0)
    await repository.close()


async def test_restricted_attempt_emits_one_sanitized_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "logging.sqlite3")
    manager = _BrowserManager([_Page(200, [RESTRICTED])])
    creator, _ = _creator(tmp_path, repository, manager)

    with caplog.at_level(logging.DEBUG):
        await creator.create(CreationWorkItem(sequence=1, session_id="logged"))

    events = [r for r in caplog.records if r.getMessage() == "acquisition_access_restricted"]
    assert len(events) == 1
    payload = json.loads(JsonLogFormatter().format(events[0]))
    assert payload["classification"] == "ACCESS_RESTRICTED_BEFORE_QUEUE"
    assert payload["retryable"] is False
    assert payload["status"] == "FAILED"
    assert payload["session_id"] == "logged"
    assert payload["attempt"] == 1
    assert isinstance(payload["duration"], float)
    rendered = " ".join(JsonLogFormatter().format(record) for record in caplog.records)
    for forbidden in ("sorry", "restricted<", "https://", "cookies", "origins", "<html"):
        assert forbidden not in rendered.casefold()
    await repository.close()


async def test_access_restriction_metrics_have_no_labels(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "labels.sqlite3")
    manager = _BrowserManager([_Page(200, [RESTRICTED]), _Page(200, [QUEUE_PAGE])])
    observability = PrometheusMetrics()
    creator, _ = _creator(
        tmp_path, repository, manager, queue_ids=["queue-secret"], observability=observability
    )
    controller = _controller(repository, creator, target=1, observability=observability)

    await controller.run()

    restriction_samples = [
        sample
        for family in observability.registry.collect()
        if "access_restricted" in family.name
        for sample in family.samples
    ]
    assert {sample.name for sample in restriction_samples} >= {
        "queue_creation_access_restricted_total",
        "queue_creation_access_restricted_consecutive",
    }
    assert all(sample.labels == {} for sample in restriction_samples)
    sensitive = {"queue-secret", *(context.page.url for context in manager.contexts)}
    sensitive |= {row.session_id for row in await repository.list()}
    for family in observability.registry.collect():
        for sample in family.samples:
            assert not sensitive & set(sample.labels.values()), sample
    await repository.close()


async def test_restart_counts_only_persisted_successes_and_never_resurrects_restrictions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.sqlite3"
    repository = SQLiteSessionRepository(database)
    stop_event = asyncio.Event()
    first_manager = _EndlessBrowserManager(successes=1, stop_after=3, stop_event=stop_event)
    first_creator, _ = _creator(tmp_path, repository, first_manager, queue_ids=["q-1"])
    first = _controller(repository, first_creator, target=3)

    first_metrics = await first.run(stop_event)

    assert first_metrics.access_restricted == 3
    assert first_metrics.successful_unique_ids == 1
    restricted_ids = {
        row.session_id for row in await repository.list(QueueStatus.FAILED)
    }
    assert len(restricted_ids) == 3
    await repository.close()

    resumed_repository = SQLiteSessionRepository(database)
    second_manager = _BrowserManager([_Page(200, [QUEUE_PAGE]) for _ in range(5)])
    second_creator, _ = _creator(
        tmp_path, resumed_repository, second_manager, queue_ids=["q-2", "q-3"]
    )
    second = _controller(resumed_repository, second_creator, target=3)

    second_metrics = await second.run()

    assert second_metrics.initial_successful_unique_ids == 1
    assert second_metrics.unique_ids_acquired == 2
    assert second_metrics.successful_unique_ids == 3
    assert second_metrics.access_restricted == 0
    assert len(second_manager.contexts) == 2
    for session_id in restricted_ids:
        row = await resumed_repository.get(session_id)
        assert row is not None
        assert row.status is QueueStatus.FAILED and row.queue_id is None
    assert await resumed_repository.count_successful_queue_ids() == 3
    await resumed_repository.close()
