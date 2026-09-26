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


class FakeUrlPage:
    def __init__(self, url: str, redirected_url: str | None = None) -> None:
        self.url = url
        self.redirected_url = redirected_url

    async def wait_for_url(self, predicate: object, **_: object) -> None:
        if self.redirected_url is not None:
            self.url = self.redirected_url


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

    assert await detector.detect(page, wait_timeout_ms=100)


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
