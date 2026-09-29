from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.queue_monitor import (
    AdmissionDetector,
    ExpectedDestination,
    QueueItTerminalStateDetector,
    TerminalQueueState,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringRetryPolicy, PollingPolicy, QueueSessionMonitor
from queue_load_test.transfer import (
    RestoreFailure,
    RestoreMethod,
    SessionRestoreResult,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def make_session(session_id: str = "session-1") -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id="queue-expected",
        transfer_url="https://queue.test/journey?q=queue-expected",
        mode=SessionMode.TRANSFER_ONLY,
        status=QueueStatus.PARKED,
        state_path=Path(f".browser-state/{session_id}.json"),
        next_check_at=NOW,
    )


class ScriptedRestorer:
    def __init__(self, results: list[SessionRestoreResult]) -> None:
        self.results = deque(results)
        self.calls = 0
        self.identities: list[str | None] = []

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        self.calls += 1
        self.identities.append(session.queue_id)
        return self.results.popleft()


def result(
    *,
    progress: QueueProgress | None = None,
    admitted: bool = False,
    expired: bool = False,
    failure: RestoreFailure | None = None,
) -> SessionRestoreResult:
    return SessionRestoreResult(
        method=RestoreMethod.TRANSFER,
        success=failure is None,
        expected_queue_id="queue-expected",
        observed_queue_id="queue-expected" if failure is None else None,
        identity_match=True if failure is None else None,
        progress=progress,
        admitted=admitted,
        expired=expired,
        failure=failure,
    )


async def monitor_for(
    tmp_path: Path,
    results: list[SessionRestoreResult],
    *,
    retry_policy: MonitoringRetryPolicy | None = None,
    sleeps: list[float] | None = None,
) -> tuple[QueueSessionMonitor, QueueSession, SQLiteSessionRepository, ScriptedRestorer]:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    session = make_session()
    await repository.create(session)
    restorer = ScriptedRestorer(results)

    async def record_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=retry_policy,
        clock=lambda: NOW,
        jitter=lambda _start, _end: 0,
        sleep=record_sleep,
    )
    return monitor, session, repository, restorer


async def test_turn_started_remains_distinct_from_admitted(tmp_path: Path) -> None:
    progress = QueueProgress(session_id="session-1", turn_started=True)
    monitor, session, repository, _ = await monitor_for(tmp_path, [result(progress=progress)])

    outcome = await monitor.check(session)

    assert outcome.observed_status is QueueStatus.TURN_STARTED
    assert outcome.next_check_at == NOW
    persisted = await repository.get(session.session_id)
    assert persisted is not None
    assert persisted.status is QueueStatus.TURN_STARTED
    await repository.close()


async def test_admitted_requires_expected_destination_result(tmp_path: Path) -> None:
    monitor, session, repository, _ = await monitor_for(tmp_path, [result(admitted=True)])

    outcome = await monitor.check(session)

    assert outcome.observed_status is QueueStatus.ADMITTED
    assert outcome.next_check_at is None
    persisted = await repository.get(session.session_id)
    assert persisted is not None
    assert persisted.status is QueueStatus.ADMITTED
    await repository.close()


async def test_expired_session_is_terminal_and_not_retried(tmp_path: Path) -> None:
    monitor, session, repository, restorer = await monitor_for(
        tmp_path,
        [result(expired=True, failure=RestoreFailure.SESSION_EXPIRED)],
    )

    outcome = await monitor.check(session)

    assert not outcome.success
    assert outcome.observed_status is QueueStatus.EXPIRED
    assert outcome.next_check_at is None
    assert restorer.calls == 1
    await repository.close()


async def test_transient_failure_retries_with_backoff(tmp_path: Path) -> None:
    sleeps: list[float] = []
    active = QueueProgress(session_id="session-1", active_queue=True)
    monitor, session, repository, restorer = await monitor_for(
        tmp_path,
        [
            result(failure=RestoreFailure.NAVIGATION_FAILED),
            result(progress=active),
        ],
        retry_policy=MonitoringRetryPolicy(
            max_attempts=3,
            initial_backoff_seconds=1,
            maximum_backoff_seconds=4,
            jitter_seconds=0,
        ),
        sleeps=sleeps,
    )

    outcome = await monitor.check(session)

    assert outcome.success
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    assert restorer.calls == 2
    assert sleeps == [1]
    await repository.close()


async def test_permanent_identity_failure_marks_failed_without_retry(tmp_path: Path) -> None:
    monitor, session, repository, restorer = await monitor_for(
        tmp_path,
        [result(failure=RestoreFailure.IDENTITY_MISMATCH)],
    )

    outcome = await monitor.check(session)

    assert not outcome.success
    assert outcome.observed_status is QueueStatus.FAILED
    assert outcome.next_check_at is None
    assert restorer.calls == 1
    persisted = await repository.get(session.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    await repository.close()


async def test_chrome_crash_retries_without_replacing_identity(tmp_path: Path) -> None:
    progress = QueueProgress(session_id="session-1", active_queue=True)
    monitor, session, repository, restorer = await monitor_for(
        tmp_path,
        [
            result(failure=RestoreFailure.STATE_CONTEXT_FAILED),
            result(progress=progress),
        ],
        retry_policy=MonitoringRetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0,
            maximum_backoff_seconds=0,
            jitter_seconds=0,
        ),
    )

    outcome = await monitor.check(session)

    assert outcome.success
    assert restorer.identities == ["queue-expected", "queue-expected"]
    persisted = await repository.get(session.session_id)
    assert persisted is not None
    assert persisted.queue_id == "queue-expected"
    await repository.close()


class _MarkerLocator:
    def __init__(self, page: "FakeUrlPage") -> None:
        self._page = page

    async def count(self) -> int:
        page = self._page
        # Markers disappear once a redirect has happened.
        redirected = page.redirected_url is not None and page.reads > 1
        return int(page.waiting_room and not redirected)


class FakeUrlPage:
    """URL changes to ``redirected_url`` after the first read (a normal redirect)."""

    def __init__(
        self,
        url: str,
        redirected_url: str | None = None,
        *,
        waiting_room: bool = False,
    ) -> None:
        self._url = url
        self.redirected_url = redirected_url
        self.waiting_room = waiting_room
        self.reads = 0

    @property
    def url(self) -> str:
        self.reads += 1
        if self.reads > 1 and self.redirected_url is not None:
            return self.redirected_url
        return self._url

    def locator(self, _: str) -> _MarkerLocator:
        return _MarkerLocator(self)


def test_expected_destination_requires_matching_origin_and_path_boundary() -> None:
    destination = ExpectedDestination.from_url("https://staging.example.test/protected")

    assert destination.matches("https://staging.example.test/protected")
    assert destination.matches("https://staging.example.test/protected/account?x=1")
    assert not destination.matches("https://staging.example.test/protected-evil")
    assert not destination.matches("https://other.example.test/protected")


async def test_admission_detector_supports_normal_redirect_wait() -> None:
    detector = AdmissionDetector.from_urls("https://staging.example.test/protected")
    page = FakeUrlPage(
        "https://queue.test/journey",
        "https://staging.example.test/protected/home",
    )

    assert await detector.detect(page, wait_timeout_ms=1_000)


QUEUE_PAGE = "https://queue.example.test/?c=customer&e=event&q=queue-1"


async def test_target_equal_to_queue_page_is_not_admission_while_queuing() -> None:
    detector = AdmissionDetector.from_urls("https://queue.example.test/?c=customer&e=event")

    assert not await detector.detect(
        FakeUrlPage(QUEUE_PAGE, waiting_room=True), queue_url=QUEUE_PAGE
    )


async def test_matching_destination_with_waiting_room_markers_is_not_admission() -> None:
    detector = AdmissionDetector.from_urls("https://staging.example.test/protected")

    assert not await detector.detect(
        FakeUrlPage("https://staging.example.test/protected", waiting_room=True)
    )
    assert await detector.detect(FakeUrlPage("https://staging.example.test/protected"))


async def test_leaving_the_queue_page_counts_when_the_destination_is_unknown() -> None:
    detector = AdmissionDetector.from_urls("https://queue.example.test/?c=customer&e=event")

    other_host = FakeUrlPage("https://shop.example.test/checkout")
    other_path = FakeUrlPage("https://queue.example.test/tickets/basket")
    challenge = FakeUrlPage("https://queue.example.test/softblock/?q=queue-1", waiting_room=True)
    same_page = FakeUrlPage(QUEUE_PAGE)

    assert await detector.detect(other_host, queue_url=QUEUE_PAGE)
    assert await detector.detect(other_path, queue_url=QUEUE_PAGE)
    assert not await detector.detect(challenge, queue_url=QUEUE_PAGE)
    # A marker-free page at the configured target still counts (explicit destination).
    assert await detector.detect(same_page, queue_url=QUEUE_PAGE)
    strict = AdmissionDetector.from_urls("https://shop.example.test/")
    assert not await strict.detect(FakeUrlPage(QUEUE_PAGE), queue_url=QUEUE_PAGE)
    assert not await strict.detect(FakeUrlPage("chrome-error://chromewebdata/"), queue_url=QUEUE_PAGE)


async def test_redirect_away_from_queue_is_awaited_after_turn_started() -> None:
    detector = AdmissionDetector.from_urls("https://queue.example.test/?c=customer&e=event")
    page = FakeUrlPage(QUEUE_PAGE, "https://shop.example.test/welcome", waiting_room=True)

    assert not await detector.detect(page, queue_url=QUEUE_PAGE)  # still queuing now
    assert await detector.detect(page, wait_timeout_ms=1_000, queue_url=QUEUE_PAGE)


async def test_unreadable_page_is_never_admission() -> None:
    from playwright.async_api import Error as PlaywrightError

    class _Broken:
        url = "https://shop.example.test/checkout"

        def locator(self, _: str) -> object:
            class _Locator:
                async def count(self) -> int:
                    raise PlaywrightError("navigating")

            return _Locator()

    detector = AdmissionDetector.from_urls("https://shop.example.test/")
    assert not await detector.detect(_Broken())  # type: ignore[arg-type]


class FakeLocator:
    def __init__(self, visible: bool) -> None:
        self.first = self
        self.visible = visible

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.visible


class FakeTerminalPage:
    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector == '[data-testid="queue-expired"]')


async def test_explicit_expired_dom_marker_is_detected() -> None:
    detector = QueueItTerminalStateDetector()

    assert await detector.detect(FakeTerminalPage()) is TerminalQueueState.EXPIRED
