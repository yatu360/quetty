"""Bounded Queue-it session creation and target acquisition."""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, Self, cast
from uuid import uuid4

from playwright.async_api import Page

from queue_load_test.browser import BrowserManager, BrowserManagerError
from queue_load_test.browser.errors import (
    BROWSER_ERROR_TYPES,
    BROWSER_TIMEOUT_ERROR_TYPES,
)
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import (
    BrowserBackendName,
    ProxyProvider,
    QueueProgress,
    QueueSession,
    QueueStatus,
    SessionMode,
)
from queue_load_test.proxy import (
    ProxyAuthWatch,
    ProxyFailure,
    ProxyResolutionError,
    SessionProxyResolver,
    classify_proxy_error,
    generate_proxy_session_id,
    resolve_session_proxy,
    validate_proxy_session_id,
)
from queue_load_test.proxy.ip_tracker import ProxyIpTracker
from queue_load_test.queue_monitor import (
    AccessRestrictionDetector,
    QueueItLiveStateExtractor,
    RenderedAccessRestrictionDetector,
)
from queue_load_test.repository import (
    ProxySessionIdConflictError,
    QueueIdConflictError,
    SessionRepository,
)
from queue_load_test.state import BrowserState, StateStore, StateStoreError
from queue_load_test.transfer import (
    QueueItTransferExtractor,
    TransferExtractionResult,
    TransferFailure,
)
from queue_load_test.utils.asyncio_tools import await_bounded

type Sleep = Callable[[float], Awaitable[None]]
type Jitter = Callable[[float, float], float]
type ProxySessionIdFactory = Callable[[], str]

logger = logging.getLogger(__name__)


class CreationOutcomeKind(StrEnum):
    SUCCESS = "SUCCESS"
    DUPLICATE = "DUPLICATE"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class AcquisitionFailure(StrEnum):
    """Typed acquisition failures with a dedicated detector; never a Queue status."""

    ACCESS_RESTRICTED_BEFORE_QUEUE = "ACCESS_RESTRICTED_BEFORE_QUEUE"

    @property
    def code(self) -> str:
        return self.value.lower()


class TransientCreationError(RuntimeError):
    """A sanitized failure that can reasonably succeed on retry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PermanentCreationError(RuntimeError):
    """A sanitized failure that should not be retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AccessRestrictedError(PermanentCreationError):
    """The rendered page is the access-restriction page; no Queue ID was acquired.

    Not retried within the work item: the attempt ends, its context is discarded and
    the bounded controller schedules ordinary replacement work.
    """

    def __init__(self) -> None:
        super().__init__(AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.code)


@dataclass(frozen=True, slots=True)
class CreationRetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    maximum_backoff_seconds: float = 5.0
    jitter_seconds: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("backoff values cannot be negative")
        if self.maximum_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum_backoff_seconds cannot be less than initial backoff")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds cannot be negative")

    def delay(self, failed_attempt: int, jitter: Jitter) -> float:
        exponential: float = min(
            self.initial_backoff_seconds * (2.0 ** max(0, failed_attempt - 1)),
            self.maximum_backoff_seconds,
        )
        return exponential + jitter(0.0, self.jitter_seconds)


@dataclass(frozen=True, slots=True)
class CreationWorkItem:
    sequence: int
    session_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True, slots=True)
class CreationOutcome:
    kind: CreationOutcomeKind
    attempts: int
    temporary_failures: int
    duration_seconds: float
    session: QueueSession | None = None
    progress: QueueProgress | None = None
    failure_code: str | None = None


class SessionCreationHandler(Protocol):
    async def create(self, work_item: CreationWorkItem) -> CreationOutcome: ...


class TransferExtractor(Protocol):
    async def extract(
        self,
        page: Page,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult: ...


type TransferExtractorFactory = Callable[[str], TransferExtractor]


@dataclass(slots=True)
class CreationMetrics:
    attempts: int = 0
    completed_work_items: int = 0
    initial_successful_unique_ids: int = 0
    successful_unique_ids: int = 0
    unique_ids_acquired: int = 0
    duplicates: int = 0
    temporary_failures: int = 0
    temporary_failure_outcomes: int = 0
    permanent_failures: int = 0
    access_restricted: int = 0
    consecutive_access_restricted: int = 0
    access_restriction_halted: bool = False
    retries: int = 0
    currently_creating: int = 0
    maximum_concurrent_creating: int = 0
    queue_depth: int = 0
    maximum_queue_depth: int = 0
    total_creation_duration_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    lost_queue_ids: int = 0
    effective_target: int = 0
    replacement_blocked: bool = False
    worker_completed: dict[int, int] = field(default_factory=dict)
    worker_successes: dict[int, int] = field(default_factory=dict)
    worker_failures: dict[int, int] = field(default_factory=dict)

    @property
    def sessions_created_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.unique_ids_acquired / self.elapsed_seconds


class QueueSessionCreator:
    """Create and persist one independent Queue-it session with bounded retries."""

    def __init__(
        self,
        *,
        browser_manager: BrowserManager,
        repository: SessionRepository,
        state_store: StateStore,
        staging_url: str,
        state_directory: Path,
        mode: SessionMode,
        browser_backend: BrowserBackendName = BrowserBackendName.CHROME,
        proxy_provider: ProxyProvider = ProxyProvider.NONE,
        proxy_resolver: SessionProxyResolver | None = None,
        proxy_ip_tracker: ProxyIpTracker | None = None,
        proxy_session_id_factory: ProxySessionIdFactory = generate_proxy_session_id,
        live_extractor: QueueItLiveStateExtractor | None = None,
        restriction_detector: AccessRestrictionDetector | None = None,
        transfer_extractor_factory: TransferExtractorFactory = QueueItTransferExtractor,
        retry_policy: CreationRetryPolicy | None = None,
        navigation_timeout_ms: float = 30_000,
        live_page_timeout_seconds: float = 30.0,
        observation_interval_seconds: float = 0.25,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.uniform,
        observability: PrometheusMetrics | None = None,
    ) -> None:
        self._browser_manager = browser_manager
        self._repository = repository
        self._state_store = state_store
        self._staging_url = staging_url
        self._state_directory = state_directory
        self._mode = SessionMode.parse(mode)
        self._browser_backend = BrowserBackendName.parse(browser_backend)
        self._proxy_provider = ProxyProvider.parse(proxy_provider)
        if proxy_resolver is not None:
            if (
                self._proxy_provider is not ProxyProvider.NONE
                and self._proxy_provider is not proxy_resolver.provider
            ):
                raise ValueError("proxy_provider contradicts the session proxy resolver")
            self._proxy_provider = proxy_resolver.provider
        if self._proxy_provider is not ProxyProvider.NONE and proxy_resolver is None:
            # Fail closed at construction: a proxied run can never acquire unproxied.
            raise ValueError("A proxied run requires a session proxy resolver")
        self._proxy_resolver = proxy_resolver
        self._proxy_ip_tracker = proxy_ip_tracker
        self._proxy_session_id_factory = proxy_session_id_factory
        self._live_extractor = live_extractor or QueueItLiveStateExtractor()
        self._restriction_detector = restriction_detector or RenderedAccessRestrictionDetector()
        self._transfer_extractor_factory = transfer_extractor_factory
        self._retry_policy = retry_policy or CreationRetryPolicy()
        self._navigation_timeout_ms = navigation_timeout_ms
        self._live_page_timeout_seconds = live_page_timeout_seconds
        self._observation_interval_seconds = observation_interval_seconds
        self._sleep = sleep
        self._jitter = jitter
        self._observability = observability
        self._attempt_timeout_seconds = (
            navigation_timeout_ms / 1_000 + live_page_timeout_seconds + 10.0
        )
        # Restricted attempts by this creator (automatic acquisition and operator Add/
        # Replace) for the current runtime; an aggregate for the operator status.
        self.access_restricted_attempts = 0

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        outcome = await self._create(work_item)
        if self._observability is not None:
            if outcome.kind is CreationOutcomeKind.SUCCESS:
                self._observability.record_creation_success(outcome.duration_seconds)
            else:
                self._observability.record_creation_failure(outcome.duration_seconds)
        log_event(
            logger,
            logging.INFO if outcome.kind is CreationOutcomeKind.SUCCESS else logging.WARNING,
            "session_creation_completed",
            session_id=work_item.session_id,
            queue_id=outcome.session.queue_id if outcome.session is not None else None,
            status=outcome.session.status.value if outcome.session is not None else None,
            attempt=outcome.attempts,
            duration=outcome.duration_seconds,
            error_type=outcome.failure_code,
        )
        return outcome

    async def _create(self, work_item: CreationWorkItem) -> CreationOutcome:
        started = time.perf_counter()
        reserved_session = await self._reserve_proxy_assignment(work_item)
        temporary_failures = 0
        last_failure = "creation_failed"
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            if self._observability is not None:
                self._observability.record_creation_attempt()
            log_event(
                logger,
                logging.INFO,
                "session_creation_attempt",
                session_id=work_item.session_id,
                attempt=attempt,
            )
            try:
                # A Playwright call can stay pending forever when Chrome dies mid-call;
                # the builtin TimeoutError is an OSError and is retried as transient.
                # await_bounded re-cancels: Playwright can otherwise wait forever for a
                # wedged browser to acknowledge the first cancellation.
                outcome = await await_bounded(
                    self._attempt(
                        work_item,
                        attempt,
                        started,
                        temporary_failures,
                        reserved_session,
                    ),
                    timeout=self._attempt_timeout_seconds,
                )
                if (
                    outcome.kind is CreationOutcomeKind.SUCCESS
                    and outcome.session is not None
                    and self._proxy_ip_tracker is not None
                ):
                    # Baseline exit IP after the Queue ID is persisted and the
                    # creation context has closed; diagnostic only.
                    await self._proxy_ip_tracker.observe_after_check(outcome.session)
                return outcome
            except AccessRestrictedError as exc:
                return await self._access_restricted(
                    work_item,
                    attempt,
                    exc.code,
                    started,
                    temporary_failures,
                    reserved_session,
                )
            except PermanentCreationError as exc:
                if self._observability is not None:
                    self._observability.record_creation_permanent_failure()
                await self._persist_failed(work_item, attempt, exc.code, reserved_session)
                return CreationOutcome(
                    kind=CreationOutcomeKind.PERMANENT_FAILURE,
                    attempts=attempt,
                    temporary_failures=temporary_failures,
                    duration_seconds=time.perf_counter() - started,
                    failure_code=exc.code,
                )
            except (
                TransientCreationError,
                BrowserManagerError,
                OSError,
                *BROWSER_ERROR_TYPES,
            ) as exc:
                temporary_failures += 1
                if self._observability is not None:
                    self._observability.record_creation_transient_failure()
                last_failure = (
                    exc.code
                    if isinstance(exc, TransientCreationError)
                    else "transient_browser_error"
                )
                if attempt < self._retry_policy.max_attempts:
                    if self._observability is not None:
                        self._observability.record_creation_retry()
                    await self._sleep(self._retry_policy.delay(attempt, self._jitter))

        await self._persist_failed(
            work_item,
            self._retry_policy.max_attempts,
            last_failure,
            reserved_session,
        )
        return CreationOutcome(
            kind=CreationOutcomeKind.TEMPORARY_FAILURE,
            attempts=self._retry_policy.max_attempts,
            temporary_failures=temporary_failures,
            duration_seconds=time.perf_counter() - started,
            failure_code=last_failure,
        )

    async def _attempt(
        self,
        work_item: CreationWorkItem,
        attempt: int,
        started: float,
        temporary_failures: int,
        reserved_session: QueueSession | None,
    ) -> CreationOutcome:
        state_saved = False
        state_path = self._state_directory / f"{work_item.session_id}.json"
        # Every attempt (including retries) re-resolves the same persisted assignment
        # before any context exists; a resolution failure makes no target request.
        try:
            proxy = (
                resolve_session_proxy(self._proxy_resolver, reserved_session)
                if reserved_session is not None
                else None
            )
        except ProxyResolutionError as exc:
            raise PermanentCreationError(exc.failure.value.lower()) from None
        if self._proxy_provider is not ProxyProvider.NONE and proxy is None:
            raise PermanentCreationError("proxy_config_missing")
        if proxy is not None and self._proxy_resolver is not None:
            self._proxy_resolver.record_attempt()
        context_scope = (
            self._browser_manager.context(proxy=proxy.browser_proxy())
            if proxy is not None
            else self._browser_manager.context()
        )
        async with context_scope as context:
            page = await context.new_page()
            auth_watch = ProxyAuthWatch.attach(page) if proxy is not None else None
            navigation_started = time.perf_counter()
            try:
                response = await page.goto(
                    self._staging_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
                self._browser_manager.report_navigation(context, responsive=True)
            except BROWSER_TIMEOUT_ERROR_TYPES:
                self._browser_manager.report_navigation(context, responsive=False)
                if self._observability is not None:
                    self._observability.record_navigation_failure(timed_out=True)
                raise
            except BROWSER_ERROR_TYPES as exc:
                if self._observability is not None:
                    self._observability.record_navigation_failure()
                proxy_failure = (
                    classify_proxy_error(exc, auth_watch) if proxy is not None else None
                )
                if proxy_failure is not None:
                    assert self._proxy_resolver is not None
                    self._proxy_resolver.record_failure(proxy_failure)
                    # Retry later through the same sticky session; never rotate it.
                    raise TransientCreationError(proxy_failure.value.lower()) from None
                raise
            finally:
                if self._observability is not None:
                    self._observability.record_navigation_duration(
                        time.perf_counter() - navigation_started
                    )
            if response is not None:
                if response.status >= 400 and self._observability is not None:
                    self._observability.record_navigation_failure()
                if response.status == 407 and proxy is not None:
                    assert self._proxy_resolver is not None
                    self._proxy_resolver.record_failure(ProxyFailure.PROXY_AUTH_FAILED)
                    raise TransientCreationError("proxy_auth_failed")
                if response.status >= 400:
                    # A restriction page may arrive with an error status; classify it
                    # from the rendered page rather than the status code alone.
                    await self._raise_if_access_restricted(page)
                if response.status >= 500 or response.status in {408, 429}:
                    raise TransientCreationError("temporary_http_response")
                if response.status >= 400:
                    raise PermanentCreationError("permanent_http_response")

            progress, transfer = await self._wait_for_live_queue(page, work_item.session_id)
            queue_id = transfer.queue_id
            if queue_id is None or transfer.transfer_url is None:
                raise PermanentCreationError("queue_identity_missing")

            if self._mode is SessionMode.HYBRID:
                storage_state = cast(BrowserState, await context.storage_state())
                try:
                    state_path = await self._state_store.save(
                        work_item.session_id,
                        storage_state,
                    )
                except (OSError, StateStoreError) as exc:
                    if self._observability is not None:
                        self._observability.record_state_persistence_failure()
                    raise TransientCreationError("state_persistence_failed") from exc
                except asyncio.CancelledError:
                    # The write completed before cancellation propagated; no session
                    # row references it yet, so remove it instead of orphaning it.
                    with contextlib.suppress(Exception):
                        await self._state_store.delete(work_item.session_id)
                    raise
                state_saved = True

            observed_at = datetime.now(UTC)
            session = QueueSession(
                session_id=work_item.session_id,
                queue_id=queue_id,
                transfer_url=transfer.transfer_url,
                mode=self._mode,
                browser_backend=self._browser_backend,
                proxy_session_id=(
                    reserved_session.proxy_session_id
                    if reserved_session is not None
                    else None
                ),
                status=QueueStatus.PARKED,
                state_path=state_path,
                created_at=(
                    reserved_session.created_at
                    if reserved_session is not None
                    else observed_at
                ),
                last_checked_at=observed_at,
                last_queue_update=progress.last_updated_at,
                last_progress_change_at=observed_at,
                next_check_at=observed_at,
                attempt_count=attempt,
            )
            try:
                if reserved_session is None:
                    await self._repository.create(session, progress)
                else:
                    await self._repository.update(session, progress)
            except QueueIdConflictError:
                if state_saved:
                    await self._state_store.delete(work_item.session_id)
                if self._observability is not None:
                    self._observability.record_creation_duplicate()
                await self._persist_failed(
                    work_item,
                    attempt,
                    "duplicate_queue_id",
                    reserved_session,
                )
                return CreationOutcome(
                    kind=CreationOutcomeKind.DUPLICATE,
                    attempts=attempt,
                    temporary_failures=temporary_failures,
                    duration_seconds=time.perf_counter() - started,
                    failure_code="duplicate_queue_id",
                )
            except BaseException:
                # A cancelled create may still have committed in its worker thread.
                # Deleting that session's state would strand a valid identity.
                if state_saved and not await self._session_committed(work_item.session_id):
                    await self._state_store.delete(work_item.session_id)
                raise
            return CreationOutcome(
                kind=CreationOutcomeKind.SUCCESS,
                attempts=attempt,
                temporary_failures=temporary_failures,
                duration_seconds=time.perf_counter() - started,
                session=session,
                progress=progress,
            )

    async def _access_restricted(
        self,
        work_item: CreationWorkItem,
        attempt: int,
        failure_code: str,
        started: float,
        temporary_failures: int,
        reserved_session: QueueSession | None,
    ) -> CreationOutcome:
        """End a restricted work item; its context is already closed by the attempt.

        No Queue ID exists, so nothing was committed for this work item. Any state
        document under its session ID can only be uncommitted and is removed.
        """

        self.access_restricted_attempts += 1
        if self._observability is not None:
            self._observability.record_creation_permanent_failure()
            self._observability.record_creation_access_restricted()
        with contextlib.suppress(OSError, StateStoreError):
            await self._state_store.delete(work_item.session_id)
        await self._persist_failed(work_item, attempt, failure_code, reserved_session)
        duration = time.perf_counter() - started
        log_event(
            logger,
            logging.WARNING,
            "acquisition_access_restricted",
            session_id=work_item.session_id,
            attempt=attempt,
            duration=duration,
            status=QueueStatus.FAILED.value,
            classification=AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.value,
            # Not retried in this work item; the controller schedules replacement work.
            retryable=False,
        )
        return CreationOutcome(
            kind=CreationOutcomeKind.PERMANENT_FAILURE,
            attempts=attempt,
            temporary_failures=temporary_failures,
            duration_seconds=duration,
            failure_code=failure_code,
        )

    async def _session_committed(self, session_id: str) -> bool:
        """Return whether the session row exists; assume it does when unknowable."""

        try:
            return await self._repository.get(session_id) is not None
        except Exception:  # noqa: BLE001 - keep state rather than risk stranding it
            return True

    async def _wait_for_live_queue(
        self,
        page: Page,
        session_id: str,
    ) -> tuple[QueueProgress, TransferExtractionResult]:
        deadline = asyncio.get_running_loop().time() + self._live_page_timeout_seconds
        last_transfer_failure: TransferFailure | None = None
        while True:
            # Checked before every observation so a restriction page rendered after
            # navigation is never mistaken for a Queue-it journey.
            await self._raise_if_access_restricted(page)
            progress = await self._live_extractor.extract(page, session_id=session_id)
            if progress.pre_queue is True or progress.active_queue is True:
                transfer = await self._transfer_extractor_factory(page.url).extract(page)
                if transfer.successful:
                    return progress, transfer
                last_transfer_failure = transfer.failure
                if last_transfer_failure in {
                    TransferFailure.MALFORMED_URL,
                    TransferFailure.UNEXPECTED_HOST,
                    TransferFailure.UNEXPECTED_JOURNEY,
                    TransferFailure.AMBIGUOUS_QUEUE_ID,
                    TransferFailure.IDENTITY_MISMATCH,
                }:
                    raise PermanentCreationError("invalid_transfer_identity")
            if asyncio.get_running_loop().time() >= deadline:
                if last_transfer_failure is TransferFailure.QUEUE_ID_MISSING:
                    raise PermanentCreationError("queue_identity_missing")
                raise TransientCreationError("queue_page_not_ready")
            await self._sleep(self._observation_interval_seconds)

    async def _raise_if_access_restricted(self, page: Page) -> None:
        if await self._restriction_detector.detect(page):
            raise AccessRestrictedError()

    async def _persist_failed(
        self,
        work_item: CreationWorkItem,
        attempts: int,
        failure_code: str,
        reserved_session: QueueSession | None = None,
    ) -> None:
        session = QueueSession(
            session_id=work_item.session_id,
            queue_id=None,
            transfer_url="",
            mode=self._mode,
            browser_backend=self._browser_backend,
            proxy_session_id=(
                reserved_session.proxy_session_id if reserved_session is not None else None
            ),
            status=QueueStatus.FAILED,
            state_path=self._state_directory / f"{work_item.session_id}.json",
            attempt_count=attempts,
            last_error=failure_code,
            created_at=(
                reserved_session.created_at
                if reserved_session is not None
                else datetime.now(UTC)
            ),
        )
        if reserved_session is None:
            await self._repository.create(session)
        else:
            await self._repository.update(session)

    async def reserve_session(self, work_item: CreationWorkItem) -> QueueSession:
        """Persist a CREATING reservation for a Manual Strategy acquisition window.

        Unlike automatic acquisition, the row exists for every provider: it carries
        the window's manual ownership, and for a proxied run its own new immutable
        sticky ID, before the window navigates anywhere. Shutdown or a crash leaves
        it as an ordinary orphaned reservation, discarded at the next run start.
        """

        reserved = await self._reserve_proxy_assignment(work_item)
        if reserved is not None:
            return reserved
        return await self._repository.create(self._reservation(work_item, None))

    async def record_failed_attempt(
        self,
        work_item: CreationWorkItem,
        failure_code: str,
        reserved_session: QueueSession,
    ) -> None:
        """Record an unsuccessful attempt as FAILED without a Queue ID (audit row)."""

        if self._observability is not None:
            self._observability.record_creation_permanent_failure()
        await self._persist_failed(work_item, 1, failure_code, reserved_session)

    async def observe_proxy_ip(self, session: QueueSession) -> None:
        """Baseline exit-IP lookup after a persisted acquisition; never raises."""

        if self._proxy_ip_tracker is not None:
            await self._proxy_ip_tracker.observe_after_check(session)

    def record_access_restricted_attempt(self, session_id: str) -> None:
        """Count a restricted attempt that ended without the creator's own context."""

        self.access_restricted_attempts += 1
        if self._observability is not None:
            self._observability.record_creation_access_restricted()
        log_event(
            logger,
            logging.WARNING,
            "acquisition_access_restricted",
            session_id=session_id,
            status=QueueStatus.FAILED.value,
            classification=AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.value,
            retryable=False,
        )

    def _reservation(
        self, work_item: CreationWorkItem, proxy_session_id: str | None
    ) -> QueueSession:
        return QueueSession(
            session_id=work_item.session_id,
            queue_id=None,
            transfer_url="",
            mode=self._mode,
            browser_backend=self._browser_backend,
            proxy_session_id=proxy_session_id,
            status=QueueStatus.CREATING,
            state_path=self._state_directory / f"{work_item.session_id}.json",
        )

    async def _reserve_proxy_assignment(
        self,
        work_item: CreationWorkItem,
    ) -> QueueSession | None:
        """Persist one immutable sticky ID before any external target navigation."""

        if self._proxy_provider is ProxyProvider.NONE:
            return None
        for _ in range(100):
            proxy_session_id = validate_proxy_session_id(
                self._proxy_session_id_factory()
            )
            session = self._reservation(work_item, proxy_session_id)
            try:
                return await self._repository.create(session)
            except ProxySessionIdConflictError:
                continue
        raise RuntimeError("Unable to allocate a unique proxy session ID")


class SessionCreationController:
    """Acquire unique Queue IDs using a bounded queue and fixed worker pool."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        handler: SessionCreationHandler,
        target_queue_ids: int,
        worker_count: int,
        queue_capacity: int | None = None,
        observability: PrometheusMetrics | None = None,
        identity_replacement_limit: int | None = None,
        access_restricted_max_consecutive: int = 25,
    ) -> None:
        if access_restricted_max_consecutive < 1:
            raise ValueError("access_restricted_max_consecutive must be at least 1")
        if identity_replacement_limit is not None and identity_replacement_limit < 0:
            raise ValueError("identity_replacement_limit cannot be negative")
        if target_queue_ids < 0:
            raise ValueError("target_queue_ids cannot be negative")
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if queue_capacity is not None and queue_capacity < 1:
            raise ValueError("queue_capacity must be at least 1")
        self._repository = repository
        self._handler = handler
        self._target = target_queue_ids
        self._worker_count = worker_count
        self._queue_capacity = queue_capacity or worker_count
        self._identity_replacement_limit = identity_replacement_limit
        self._access_restricted_max_consecutive = access_restricted_max_consecutive
        self.metrics = CreationMetrics()
        self._observability = observability
        if observability is not None:
            observability.set_target(target_queue_ids)

    def adjust_target(self, delta: int) -> None:
        """Keep a still-running acquisition loop aligned with operator add/delete."""

        self._target = max(0, self._target + delta)
        if self._observability is not None:
            self._observability.set_target(self._target)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repository: SessionRepository,
        handler: SessionCreationHandler,
        observability: PrometheusMetrics | None = None,
    ) -> Self:
        return cls(
            repository=repository,
            handler=handler,
            target_queue_ids=settings.target_queue_ids,
            worker_count=settings.creation_workers,
            queue_capacity=settings.creation_queue_capacity,
            observability=observability,
            identity_replacement_limit=settings.identity_replacement_limit,
            access_restricted_max_consecutive=settings.access_restricted_max_consecutive,
        )

    async def run(self, stop_event: asyncio.Event | None = None) -> CreationMetrics:
        started = time.perf_counter()
        work_queue: asyncio.Queue[CreationWorkItem | None] = asyncio.Queue(
            maxsize=self._queue_capacity
        )
        result_queue: asyncio.Queue[tuple[int, CreationOutcome]] = asyncio.Queue(
            maxsize=self._worker_count
        )
        workers = [
            asyncio.create_task(
                self._worker(index, work_queue, result_queue),
                name=f"creation-worker-{index}",
            )
            for index in range(self._worker_count)
        ]
        sequence = 0
        in_flight = 0
        try:
            existing = await self._repository.count_successful_queue_ids()
            self.metrics.initial_successful_unique_ids = existing
            self.metrics.successful_unique_ids = existing
            target = await self._effective_target()
            self._set_creation_activity(work_queue)
            while not (stop_event is not None and stop_event.is_set()):
                while self.metrics.successful_unique_ids < target and not (
                    stop_event is not None and stop_event.is_set()
                ):
                    deficit = target - self.metrics.successful_unique_ids
                    desired_in_flight = min(self._worker_count, deficit)
                    while in_flight < desired_in_flight:
                        sequence += 1
                        await work_queue.put(CreationWorkItem(sequence=sequence))
                        in_flight += 1
                        self._set_creation_activity(work_queue)

                    worker_index, outcome = await result_queue.get()
                    in_flight -= 1
                    self._record(outcome, worker_index=worker_index)
                    if self._observability is not None:
                        elapsed = max(time.perf_counter() - started, 1e-9)
                        self._observability.set_creation_rate(
                            self.metrics.unique_ids_acquired / elapsed
                        )
                    if self.metrics.access_restriction_halted:
                        break

                if self.metrics.access_restriction_halted:
                    break

                # Successful outcomes are persisted by the handler, so they can drive
                # the hot loop without an O(target) sequence of COUNT queries. Verify
                # against SQLite before declaring the target complete.
                self.metrics.successful_unique_ids = (
                    await self._repository.count_successful_queue_ids()
                )
                target = await self._effective_target()
                if self.metrics.successful_unique_ids >= target or (
                    stop_event is not None and stop_event.is_set()
                ):
                    break

            if in_flight:
                for _ in range(in_flight):
                    worker_index, outcome = await result_queue.get()
                    self._record(outcome, worker_index=worker_index)
                self.metrics.successful_unique_ids = (
                    await self._repository.count_successful_queue_ids()
                )
        except BaseException:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        else:
            for _ in workers:
                await work_queue.put(None)
            await asyncio.gather(*workers)
        finally:
            self.metrics.elapsed_seconds = time.perf_counter() - started
            self.metrics.queue_depth = 0
            if self._observability is not None:
                self._observability.set_creation_activity(in_flight=0, queue_depth=0)
                self._observability.set_creation_rate(
                    self.metrics.sessions_created_per_second
                )
        return self.metrics

    def _track_access_restriction(self, outcome: CreationOutcome) -> None:
        if outcome.kind is CreationOutcomeKind.SUCCESS:
            self.metrics.consecutive_access_restricted = 0
        elif _is_access_restricted(outcome):
            self.metrics.access_restricted += 1
            self.metrics.consecutive_access_restricted += 1
            if (
                not self.metrics.access_restriction_halted
                and self.metrics.consecutive_access_restricted
                >= self._access_restricted_max_consecutive
            ):
                # Stop scheduling new work; in-flight items drain. A run restart resumes
                # from the persisted successful IDs.
                self.metrics.access_restriction_halted = True
                log_event(
                    logger,
                    logging.ERROR,
                    "acquisition_halted_access_restricted",
                    classification=AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.value,
                    count=self.metrics.consecutive_access_restricted,
                    valid_queue_ids=self.metrics.successful_unique_ids,
                )
        else:
            return
        if self._observability is not None:
            self._observability.set_access_restriction_state(
                consecutive=self.metrics.consecutive_access_restricted,
                halted=self.metrics.access_restriction_halted,
            )

    async def _effective_target(self) -> int:
        """Return how many valid IDs may be pursued without mass identity replacement.

        A Queue ID that later becomes FAILED (for example after an identity
        mismatch) no longer counts as valid. Without a limit, the deficit would be
        refilled with brand-new identities; a systemic fault could then replace a
        large part of the population. Replacements are capped by
        ``identity_replacement_limit``; ``None`` keeps the uncapped behavior.
        """

        if self._identity_replacement_limit is None:
            self.metrics.effective_target = self._target
            return self._target
        lost = await self._repository.count_lost_queue_ids()
        effective = min(self._target, self._target + self._identity_replacement_limit - lost)
        blocked = effective < self._target
        if blocked and not self.metrics.replacement_blocked:
            log_event(
                logger,
                logging.ERROR,
                "identity_replacement_limit_reached",
                lost_queue_ids=lost,
                count=self._identity_replacement_limit,
                valid_queue_ids=self.metrics.successful_unique_ids,
            )
        self.metrics.lost_queue_ids = lost
        self.metrics.effective_target = effective
        self.metrics.replacement_blocked = blocked
        if self._observability is not None:
            self._observability.set_identity_replacement_blocked(blocked)
        return effective

    async def _worker(
        self,
        worker_index: int,
        work_queue: asyncio.Queue[CreationWorkItem | None],
        result_queue: asyncio.Queue[tuple[int, CreationOutcome]],
    ) -> None:
        while True:
            work_item = await work_queue.get()
            try:
                if work_item is None:
                    return
                self._set_creation_activity(work_queue)
                self.metrics.currently_creating += 1
                self.metrics.maximum_concurrent_creating = max(
                    self.metrics.maximum_concurrent_creating,
                    self.metrics.currently_creating,
                )
                self._set_creation_activity(work_queue)
                try:
                    try:
                        outcome = await self._handler.create(work_item)
                    except Exception:  # noqa: BLE001 - isolate one failed worker job
                        if self._observability is not None:
                            self._observability.record_creation_transient_failure()
                            self._observability.record_creation_failure(0.0)
                        outcome = CreationOutcome(
                            kind=CreationOutcomeKind.TEMPORARY_FAILURE,
                            attempts=1,
                            temporary_failures=1,
                            duration_seconds=0.0,
                            failure_code="unexpected_creation_error",
                        )
                finally:
                    self.metrics.currently_creating -= 1
                    self._set_creation_activity(work_queue)
                await result_queue.put((worker_index, outcome))
            finally:
                work_queue.task_done()

    def _record(self, outcome: CreationOutcome, *, worker_index: int) -> None:
        self.metrics.completed_work_items += 1
        self.metrics.worker_completed[worker_index] = (
            self.metrics.worker_completed.get(worker_index, 0) + 1
        )
        self.metrics.attempts += outcome.attempts
        self.metrics.retries += max(0, outcome.attempts - 1)
        self.metrics.temporary_failures += outcome.temporary_failures
        self.metrics.total_creation_duration_seconds += outcome.duration_seconds
        if outcome.kind is CreationOutcomeKind.SUCCESS:
            self.metrics.unique_ids_acquired += 1
            self.metrics.successful_unique_ids += 1
            self.metrics.worker_successes[worker_index] = (
                self.metrics.worker_successes.get(worker_index, 0) + 1
            )
        elif outcome.kind is CreationOutcomeKind.DUPLICATE:
            self.metrics.duplicates += 1
        elif outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE:
            self.metrics.permanent_failures += 1
        elif outcome.kind is CreationOutcomeKind.TEMPORARY_FAILURE:
            self.metrics.temporary_failure_outcomes += 1
        self._track_access_restriction(outcome)
        if outcome.failure_code == "unexpected_creation_error":
            self.metrics.worker_failures[worker_index] = (
                self.metrics.worker_failures.get(worker_index, 0) + 1
            )

    def _set_creation_activity(
        self,
        work_queue: asyncio.Queue[CreationWorkItem | None],
    ) -> None:
        queue_depth = work_queue.qsize()
        self.metrics.queue_depth = queue_depth
        self.metrics.maximum_queue_depth = max(
            self.metrics.maximum_queue_depth,
            queue_depth,
        )
        if self._observability is not None:
            self._observability.set_creation_activity(
                in_flight=self.metrics.currently_creating,
                queue_depth=queue_depth,
            )


def _is_access_restricted(outcome: CreationOutcome) -> bool:
    return outcome.failure_code == AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.code
