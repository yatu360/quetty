"""Restore persisted Queue-it sessions without changing their expected identity."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from queue_load_test.browser import BrowserCapacityError, BrowserManager, OwnedBrowserContext
from queue_load_test.browser.manager import ContextStorageState
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import BrowserBackendName, QueueProgress, QueueSession, SessionMode
from queue_load_test.queue_monitor import (
    AdmissionDetector,
    QueueItLiveStateExtractor,
    QueueItTerminalStateDetector,
    TerminalQueueState,
)
from queue_load_test.repository import SessionRepository
from queue_load_test.state import (
    BrowserState,
    StateStore,
    StateStoreError,
    StateUnreadableError,
)
from queue_load_test.transfer.extractor import (
    QueueItTransferExtractor,
    TransferExtractionResult,
    TransferFailure,
)

type Sleep = Callable[[float], Awaitable[None]]

logger = logging.getLogger(__name__)


class RestoreMethod(StrEnum):
    """Supported ways to resume a persisted Queue-it journey."""

    TRANSFER = "TRANSFER"
    STORAGE_STATE = "STORAGE_STATE"


class RestoreFailure(StrEnum):
    """Sanitized, observable restoration failure reasons."""

    EXPECTED_IDENTITY_MISSING = "EXPECTED_IDENTITY_MISSING"
    TRANSFER_URL_MISSING = "TRANSFER_URL_MISSING"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    HTTP_FAILURE = "HTTP_FAILURE"
    TRANSFER_UNAVAILABLE = "TRANSFER_UNAVAILABLE"
    IDENTITY_UNVERIFIED = "IDENTITY_UNVERIFIED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    STATE_MISSING = "STATE_MISSING"
    STATE_CORRUPT = "STATE_CORRUPT"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"
    STATE_CONTEXT_FAILED = "STATE_CONTEXT_FAILED"
    STATE_REFRESH_FAILED = "STATE_REFRESH_FAILED"
    BACKEND_MISMATCH = "BACKEND_MISMATCH"
    INVALID_TRANSFER_URL = "INVALID_TRANSFER_URL"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    EVENT_CLOSED = "EVENT_CLOSED"


@dataclass(frozen=True, slots=True)
class RestoreAttempt:
    """One transfer or storage-state attempt, without exposing sensitive values."""

    method: RestoreMethod
    success: bool
    observed_queue_id: str | None = field(default=None, repr=False)
    identity_match: bool | None = None
    progress: QueueProgress | None = None
    failure: RestoreFailure | None = None
    state_refreshed: bool = False
    admitted: bool = False
    expired: bool = False


@dataclass(frozen=True, slots=True)
class SessionRestoreResult:
    """Final restoration outcome and the attempts that led to it."""

    method: RestoreMethod
    success: bool
    expected_queue_id: str | None = field(default=None, repr=False)
    observed_queue_id: str | None = field(default=None, repr=False)
    identity_match: bool | None = None
    progress: QueueProgress | None = None
    failure: RestoreFailure | None = None
    state_refreshed: bool = False
    admitted: bool = False
    expired: bool = False
    attempts: tuple[RestoreAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class OpenedSessionRestore:
    """Identity-safe restore result that retains a successful browser context."""

    result: SessionRestoreResult
    owned_context: OwnedBrowserContext | None = None
    page: Page | None = None
    # True when the session had no Queue ID: the window is open for the operator,
    # and an identity observed in it can later be adopted with ``adopt_open``.
    identity_pending: bool = False


def _is_restore_failure(attempt: RestoreAttempt) -> bool:
    """A failed refresh after a verified restore is a storage fault, not a restore fault."""

    return not attempt.success and attempt.failure is not RestoreFailure.STATE_REFRESH_FAILED


class TransferExtractor(Protocol):
    async def extract(
        self,
        page: Page,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult: ...


type TransferExtractorFactory = Callable[[str], TransferExtractor]


class QueueSessionRestorer:
    """Restore a session through supported Queue-it browser mechanisms.

    Storage state is only a fallback for HYBRID sessions. It restores the
    cookies and browser storage represented by Playwright's storage-state
    document; it is deliberately not treated as a complete browser snapshot.
    """

    def __init__(
        self,
        *,
        browser_manager: BrowserManager,
        repository: SessionRepository,
        state_store: StateStore,
        expected_journey_url: str | None = None,
        storage_navigation_url: str | None = None,
        live_extractor: QueueItLiveStateExtractor | None = None,
        admission_detector: AdmissionDetector | None = None,
        terminal_state_detector: QueueItTerminalStateDetector | None = None,
        transfer_extractor_factory: TransferExtractorFactory = QueueItTransferExtractor,
        navigation_timeout_ms: float = 30_000,
        admission_wait_timeout_ms: float = 5_000,
        observation_timeout_seconds: float = 10.0,
        observation_interval_seconds: float = 0.25,
        sleep: Sleep = asyncio.sleep,
        observability: PrometheusMetrics | None = None,
        attempt_timeout_seconds: float | None = None,
        browser_backend: BrowserBackendName = BrowserBackendName.CHROME,
    ) -> None:
        if attempt_timeout_seconds is not None and attempt_timeout_seconds <= 0:
            raise ValueError("attempt_timeout_seconds must be positive")
        if navigation_timeout_ms <= 0:
            raise ValueError("navigation_timeout_ms must be positive")
        if observation_timeout_seconds < 0:
            raise ValueError("observation_timeout_seconds cannot be negative")
        if observation_interval_seconds < 0:
            raise ValueError("observation_interval_seconds cannot be negative")
        if admission_wait_timeout_ms < 0:
            raise ValueError("admission_wait_timeout_ms cannot be negative")
        self._browser_manager = browser_manager
        self._repository = repository
        self._state_store = state_store
        self._expected_journey_url = expected_journey_url
        self._storage_navigation_url = storage_navigation_url
        self._live_extractor = live_extractor or QueueItLiveStateExtractor()
        self._admission_detector = admission_detector
        self._terminal_state_detector = terminal_state_detector or QueueItTerminalStateDetector()
        self._transfer_extractor_factory = transfer_extractor_factory
        self._navigation_timeout_ms = navigation_timeout_ms
        self._admission_wait_timeout_ms = admission_wait_timeout_ms
        self._observation_timeout_seconds = observation_timeout_seconds
        self._observation_interval_seconds = observation_interval_seconds
        self._sleep = sleep
        self._observability = observability
        self._browser_backend = BrowserBackendName.parse(browser_backend)
        # Bound the whole attempt: some Playwright calls (for example new_page after
        # the Chrome process is killed) never settle and have no timeout of their own.
        self._attempt_timeout_seconds = attempt_timeout_seconds or (
            navigation_timeout_ms / 1_000
            + observation_timeout_seconds
            + admission_wait_timeout_ms / 1_000
            + 10.0
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        browser_manager: BrowserManager,
        repository: SessionRepository,
        state_store: StateStore,
        observability: PrometheusMetrics | None = None,
    ) -> "QueueSessionRestorer":
        """Use the configured staging URL solely as the admission destination."""

        return cls(
            browser_manager=browser_manager,
            repository=repository,
            state_store=state_store,
            admission_detector=AdmissionDetector.from_urls(settings.require_staging_url()),
            storage_navigation_url=settings.require_staging_url(),
            admission_wait_timeout_ms=settings.admission_wait_seconds * 1_000,
            observability=observability,
            browser_backend=settings.browser_backend,
        )

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        started = time.perf_counter()
        result = self._backend_mismatch_result(session) or await self._restore(session)
        if result.failure is RestoreFailure.BACKEND_MISMATCH:
            await self._record(session, result)
        self._observe_restore_result(session, result, time.perf_counter() - started)
        return result

    async def restore_with_method(
        self,
        session: QueueSession,
        method: RestoreMethod,
    ) -> SessionRestoreResult:
        """Run exactly one supported restore mechanism for controlled measurement."""

        method = RestoreMethod(method)
        started = time.perf_counter()
        result = self._backend_mismatch_result(session, method=method)
        if result is None:
            result = await self._restore_with_method(session, method)
        else:
            await self._record(session, result)
        self._observe_restore_result(session, result, time.perf_counter() - started)
        return result

    async def restore_open(self, session: QueueSession) -> OpenedSessionRestore:
        """Restore a session while retaining only a verified live context."""

        started = time.perf_counter()
        mismatch = self._backend_mismatch_result(session)
        if mismatch is not None:
            await self._record(session, mismatch)
            self._observe_restore_result(session, mismatch, time.perf_counter() - started)
            return OpenedSessionRestore(mismatch)
        expected_queue_id = session.queue_id
        if expected_queue_id is None:
            return await self._open_unidentified(session)
        if not session.transfer_url.strip():
            opened = OpenedSessionRestore(
                self._result(
                    RestoreAttempt(
                        method=RestoreMethod.TRANSFER,
                        success=False,
                        failure=RestoreFailure.TRANSFER_URL_MISSING,
                    ),
                    expected_queue_id,
                )
            )
        elif not self._valid_transfer_url(session.transfer_url):
            opened = OpenedSessionRestore(
                self._result(
                    RestoreAttempt(
                        method=RestoreMethod.TRANSFER,
                        success=False,
                        failure=RestoreFailure.INVALID_TRANSFER_URL,
                    ),
                    expected_queue_id,
                )
            )
        else:
            transfer = await self._open_browser_attempt(
                session,
                method=RestoreMethod.TRANSFER,
            )
            attempts = [transfer.result.attempts[-1]]
            if (
                transfer.owned_context is not None
                or session.mode is SessionMode.TRANSFER_ONLY
                or transfer.result.failure is RestoreFailure.STATE_REFRESH_FAILED
            ):
                opened = replace(
                    transfer,
                    result=self._result(attempts[-1], expected_queue_id, attempts),
                )
            else:
                state, failure = await self._load_storage_state(session)
                if failure is not None:
                    state_attempt = RestoreAttempt(
                        method=RestoreMethod.STORAGE_STATE,
                        success=False,
                        failure=failure,
                    )
                    attempts.append(state_attempt)
                    opened = OpenedSessionRestore(
                        self._result(state_attempt, expected_queue_id, attempts)
                    )
                else:
                    state_opened = await self._open_browser_attempt(
                        session,
                        method=RestoreMethod.STORAGE_STATE,
                        storage_state=state,
                    )
                    attempts.append(state_opened.result.attempts[-1])
                    opened = replace(
                        state_opened,
                        result=self._result(attempts[-1], expected_queue_id, attempts),
                    )
        await self._record(session, opened.result)
        self._observe_restore_result(session, opened.result, time.perf_counter() - started)
        return opened

    async def _open_unidentified(self, session: QueueSession) -> OpenedSessionRestore:
        """Open a window for a session that never acquired a Queue ID.

        There is no identity to verify, so nothing is recorded: the persisted
        status and error stay as they are until an identity is adopted.
        """

        navigation_url = self._storage_navigation_url
        if navigation_url is None and self._valid_transfer_url(session.transfer_url):
            navigation_url = session.transfer_url
        if navigation_url is None:
            return OpenedSessionRestore(
                self._result(
                    RestoreAttempt(
                        method=RestoreMethod.TRANSFER,
                        success=False,
                        failure=RestoreFailure.TRANSFER_URL_MISSING,
                    ),
                    None,
                )
            )
        storage_state: ContextStorageState | None = None
        if session.mode is SessionMode.HYBRID:
            storage_state, _ = await self._load_storage_state(session)
        owned: OwnedBrowserContext | None = None
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                owned = await self._browser_manager.create_context(storage_state=storage_state)
                page = await owned.context.new_page()
                await page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
        except (asyncio.CancelledError, BrowserCapacityError):
            if owned is not None:
                await owned.close()
            raise
        except Exception:  # noqa: BLE001 - browser adapter boundary, includes timeouts
            if owned is not None:
                await owned.close()
            return OpenedSessionRestore(
                self._result(
                    RestoreAttempt(
                        method=RestoreMethod.TRANSFER,
                        success=False,
                        failure=RestoreFailure.NAVIGATION_FAILED,
                    ),
                    None,
                )
            )
        return OpenedSessionRestore(
            self._result(RestoreAttempt(method=RestoreMethod.TRANSFER, success=True), None),
            owned,
            page,
            identity_pending=True,
        )

    async def adopt_open(
        self,
        session: QueueSession,
        *,
        context: BrowserContext,
        page: Page,
    ) -> SessionRestoreResult | None:
        """Adopt a live Queue-it identity observed in an unidentified open window.

        Returns ``None`` while no live queue with a transfer identity is visible.
        The session is updated in memory only; the caller persists the result.
        """

        progress = await self._live_extractor.extract(page, session_id=session.session_id)
        if progress.pre_queue is not True and progress.active_queue is not True:
            return None
        transfer = await self._transfer_extractor_factory(
            self._expected_journey_url or page.url
        ).extract(page)
        queue_id = transfer.queue_id
        if not transfer.successful or queue_id is None or transfer.transfer_url is None:
            return None
        session.queue_id = queue_id
        session.transfer_url = transfer.transfer_url
        session.last_error = None
        attempt = RestoreAttempt(
            method=RestoreMethod.TRANSFER,
            success=True,
            observed_queue_id=queue_id,
            identity_match=True,
            progress=progress,
        )
        if session.mode is SessionMode.HYBRID:
            attempt = await self._refresh_state(session, context, attempt)
        return self._result(attempt, queue_id)

    async def discard_state(self, session: QueueSession) -> None:
        """Remove storage state saved for an identity that could not be adopted."""

        await self._state_store.delete(session.session_id)

    async def inspect_open(
        self,
        session: QueueSession,
        *,
        context: BrowserContext,
        page: Page,
    ) -> SessionRestoreResult:
        """Inspect and refresh a still-open verified manual session."""

        attempt = await self._observe(page, session, RestoreMethod.TRANSFER)
        if attempt.success and session.mode is SessionMode.HYBRID:
            attempt = await self._refresh_state(session, context, attempt)
        return self._result(attempt, session.queue_id)

    def _observe_restore_result(
        self,
        session: QueueSession,
        result: SessionRestoreResult,
        duration: float,
    ) -> None:
        if self._observability is not None:
            self._observability.record_restore(
                duration,
                success=result.success,
                used_storage_state=result.method is RestoreMethod.STORAGE_STATE,
                identity_mismatch=any(
                    attempt.identity_match is False for attempt in result.attempts
                ),
                transfer_failures=sum(
                    _is_restore_failure(attempt) and attempt.method is RestoreMethod.TRANSFER
                    for attempt in result.attempts
                ),
                state_failures=sum(
                    _is_restore_failure(attempt)
                    and attempt.method is RestoreMethod.STORAGE_STATE
                    for attempt in result.attempts
                ),
                state_refresh_failures=sum(
                    attempt.failure is RestoreFailure.STATE_REFRESH_FAILED
                    for attempt in result.attempts
                ),
            )
        log_event(
            logger,
            logging.INFO if result.success else logging.WARNING,
            "session_restore_completed",
            session_id=session.session_id,
            queue_id=session.queue_id,
            status=session.status.value,
            worker_id=session.worker_id,
            attempt=session.attempt_count,
            restore_method=result.method.value,
            duration=duration,
            error_type=result.failure.value if result.failure is not None else None,
        )

    async def _restore_with_method(
        self,
        session: QueueSession,
        method: RestoreMethod,
    ) -> SessionRestoreResult:
        expected_queue_id = session.queue_id
        if expected_queue_id is None:
            attempt = RestoreAttempt(
                method=method,
                success=False,
                failure=RestoreFailure.EXPECTED_IDENTITY_MISSING,
            )
        elif method is RestoreMethod.TRANSFER:
            if not session.transfer_url.strip():
                attempt = RestoreAttempt(
                    method=method,
                    success=False,
                    failure=RestoreFailure.TRANSFER_URL_MISSING,
                )
            elif not self._valid_transfer_url(session.transfer_url):
                attempt = RestoreAttempt(
                    method=method,
                    success=False,
                    failure=RestoreFailure.INVALID_TRANSFER_URL,
                )
            else:
                attempt = await self._browser_attempt(
                    session,
                    method=method,
                    refresh_state=False,
                )
        else:
            attempt = await self._storage_state_attempt(session, refresh_state=False)
        result = self._result(attempt, expected_queue_id)
        await self._record(session, result)
        return result

    async def _restore(self, session: QueueSession) -> SessionRestoreResult:
        """Restore one session, preserving its persisted Queue-it identity."""

        expected_queue_id = session.queue_id
        if expected_queue_id is None:
            result = self._result(
                RestoreAttempt(
                    method=RestoreMethod.TRANSFER,
                    success=False,
                    failure=RestoreFailure.EXPECTED_IDENTITY_MISSING,
                ),
                expected_queue_id,
            )
            await self._record(session, result)
            return result
        if not session.transfer_url.strip():
            result = self._result(
                RestoreAttempt(
                    method=RestoreMethod.TRANSFER,
                    success=False,
                    failure=RestoreFailure.TRANSFER_URL_MISSING,
                ),
                expected_queue_id,
            )
            await self._record(session, result)
            return result
        if not self._valid_transfer_url(session.transfer_url):
            result = self._result(
                RestoreAttempt(
                    method=RestoreMethod.TRANSFER,
                    success=False,
                    failure=RestoreFailure.INVALID_TRANSFER_URL,
                ),
                expected_queue_id,
            )
            await self._record(session, result)
            return result

        attempts: list[RestoreAttempt] = []
        transfer_attempt = await self._browser_attempt(
            session,
            method=RestoreMethod.TRANSFER,
        )
        attempts.append(transfer_attempt)
        # A verified transfer whose only fault was saving refreshed state must not
        # fall back: the identity was already observed, and a storage-state retry
        # would add browser load while the state store is unavailable.
        if (
            transfer_attempt.success
            or transfer_attempt.failure is RestoreFailure.STATE_REFRESH_FAILED
            or session.mode is SessionMode.TRANSFER_ONLY
        ):
            result = self._result(transfer_attempt, expected_queue_id, attempts)
            await self._record(session, result)
            return result

        state_attempt = await self._storage_state_attempt(session)
        attempts.append(state_attempt)
        result = self._result(state_attempt, expected_queue_id, attempts)
        await self._record(session, result)
        return result

    async def _storage_state_attempt(
        self,
        session: QueueSession,
        *,
        refresh_state: bool = True,
    ) -> RestoreAttempt:
        state, failure = await self._load_storage_state(session)
        if failure is not None:
            return RestoreAttempt(method=RestoreMethod.STORAGE_STATE, success=False, failure=failure)
        return await self._browser_attempt(
            session,
            method=RestoreMethod.STORAGE_STATE,
            storage_state=cast(ContextStorageState, state),
            refresh_state=refresh_state,
        )

    async def _load_storage_state(
        self, session: QueueSession
    ) -> tuple[ContextStorageState | None, RestoreFailure | None]:
        if session.browser_backend is not self._browser_backend:
            return None, RestoreFailure.BACKEND_MISMATCH
        try:
            state = await self._state_store.load(session.session_id)
        except (StateUnreadableError, OSError):
            return None, RestoreFailure.STATE_UNAVAILABLE
        except StateStoreError:
            return None, RestoreFailure.STATE_CORRUPT
        if state is None:
            return None, RestoreFailure.STATE_MISSING
        return cast(ContextStorageState, state), None

    def _backend_mismatch_result(
        self,
        session: QueueSession,
        *,
        method: RestoreMethod = RestoreMethod.TRANSFER,
    ) -> SessionRestoreResult | None:
        """Reject a session created by another backend without exposing state."""

        if session.browser_backend is self._browser_backend:
            return None
        return self._result(
            RestoreAttempt(
                method=method,
                success=False,
                failure=RestoreFailure.BACKEND_MISMATCH,
            ),
            session.queue_id,
        )

    async def _browser_attempt(
        self,
        session: QueueSession,
        *,
        method: RestoreMethod,
        storage_state: ContextStorageState | None = None,
        refresh_state: bool = True,
    ) -> RestoreAttempt:
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                return await self._unbounded_browser_attempt(
                    session,
                    method=method,
                    storage_state=storage_state,
                    refresh_state=refresh_state,
                )
        except TimeoutError:
            if self._observability is not None:
                self._observability.record_browser_operation_timeout()
            log_event(
                logger,
                logging.WARNING,
                "browser_attempt_timed_out",
                session_id=session.session_id,
                queue_id=session.queue_id,
                restore_method=method.value,
                duration=self._attempt_timeout_seconds,
            )
            failure = (
                RestoreFailure.STATE_CONTEXT_FAILED
                if method is RestoreMethod.STORAGE_STATE
                else RestoreFailure.NAVIGATION_FAILED
            )
            return RestoreAttempt(method=method, success=False, failure=failure)

    async def _open_browser_attempt(
        self,
        session: QueueSession,
        *,
        method: RestoreMethod,
        storage_state: ContextStorageState | None = None,
    ) -> OpenedSessionRestore:
        try:
            async with asyncio.timeout(self._attempt_timeout_seconds):
                return await self._unbounded_open_browser_attempt(
                    session,
                    method=method,
                    storage_state=storage_state,
                )
        except TimeoutError:
            if self._observability is not None:
                self._observability.record_browser_operation_timeout()
            failure = (
                RestoreFailure.STATE_CONTEXT_FAILED
                if method is RestoreMethod.STORAGE_STATE
                else RestoreFailure.NAVIGATION_FAILED
            )
            attempt = RestoreAttempt(method=method, success=False, failure=failure)
            return OpenedSessionRestore(self._result(attempt, session.queue_id))

    async def _unbounded_open_browser_attempt(
        self,
        session: QueueSession,
        *,
        method: RestoreMethod,
        storage_state: ContextStorageState | None,
    ) -> OpenedSessionRestore:
        owned: OwnedBrowserContext | None = None
        try:
            owned = await self._browser_manager.create_context(storage_state=storage_state)
            context = owned.context
            page = await context.new_page()
            navigation_url = (
                self._storage_navigation_url or session.transfer_url
                if method is RestoreMethod.STORAGE_STATE
                else session.transfer_url
            )
            navigation_started = time.perf_counter()
            try:
                response = await page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
            except PlaywrightTimeoutError:
                if self._observability is not None:
                    self._observability.record_navigation_failure(timed_out=True)
                failure = (
                    RestoreFailure.STATE_CONTEXT_FAILED
                    if method is RestoreMethod.STORAGE_STATE
                    else RestoreFailure.NAVIGATION_FAILED
                )
                attempt = RestoreAttempt(method=method, success=False, failure=failure)
            except Exception:  # noqa: BLE001
                if self._observability is not None:
                    self._observability.record_navigation_failure()
                failure = (
                    RestoreFailure.STATE_CONTEXT_FAILED
                    if method is RestoreMethod.STORAGE_STATE
                    else RestoreFailure.NAVIGATION_FAILED
                )
                attempt = RestoreAttempt(method=method, success=False, failure=failure)
            else:
                if response is not None and response.status == 410:
                    attempt = RestoreAttempt(
                        method=method,
                        success=False,
                        failure=RestoreFailure.SESSION_EXPIRED,
                        expired=True,
                    )
                elif response is not None and response.status >= 400:
                    failure = (
                        RestoreFailure.HTTP_FAILURE
                        if response.status >= 500 or response.status in {408, 429}
                        else RestoreFailure.INVALID_TRANSFER_URL
                    )
                    attempt = RestoreAttempt(method=method, success=False, failure=failure)
                elif self._admission_detector is not None and await self._admission_detector.detect(
                    page
                ):
                    attempt = RestoreAttempt(
                        method=method,
                        success=True,
                        identity_match=True,
                        admitted=True,
                    )
                else:
                    attempt = await self._observe(page, session, method)
                    if attempt.success and session.mode is SessionMode.HYBRID:
                        attempt = await self._refresh_state(session, context, attempt)
            finally:
                if self._observability is not None:
                    self._observability.record_navigation_duration(
                        time.perf_counter() - navigation_started
                    )
            result = self._result(attempt, session.queue_id)
            keep_open = result.success or (
                result.failure is RestoreFailure.STATE_REFRESH_FAILED
                and result.identity_match is True
                and (result.progress is not None or result.admitted)
            )
            if keep_open:
                return OpenedSessionRestore(result, owned, page)
        except asyncio.CancelledError:
            if owned is not None:
                await owned.close()
            raise
        except BrowserCapacityError:
            if owned is not None:
                await owned.close()
            raise
        except Exception:  # noqa: BLE001
            failure = (
                RestoreFailure.STATE_CONTEXT_FAILED
                if method is RestoreMethod.STORAGE_STATE
                else RestoreFailure.NAVIGATION_FAILED
            )
            result = self._result(
                RestoreAttempt(method=method, success=False, failure=failure),
                session.queue_id,
            )
        if owned is not None:
            await owned.close()
        return OpenedSessionRestore(result)

    async def _refresh_state(
        self,
        session: QueueSession,
        context: BrowserContext,
        attempt: RestoreAttempt,
    ) -> RestoreAttempt:
        try:
            state = cast(BrowserState, await context.storage_state())
            session.state_path = await self._state_store.save(session.session_id, state)
        except Exception:  # noqa: BLE001
            return replace(
                attempt,
                success=False,
                failure=RestoreFailure.STATE_REFRESH_FAILED,
            )
        return replace(attempt, state_refreshed=True)

    async def _unbounded_browser_attempt(
        self,
        session: QueueSession,
        *,
        method: RestoreMethod,
        storage_state: ContextStorageState | None,
        refresh_state: bool,
    ) -> RestoreAttempt:
        try:
            async with self._browser_manager.context(storage_state=storage_state) as context:
                page = await context.new_page()
                navigation_url = (
                    self._storage_navigation_url or session.transfer_url
                    if method is RestoreMethod.STORAGE_STATE
                    else session.transfer_url
                )
                navigation_started = time.perf_counter()
                try:
                    response = await page.goto(
                        navigation_url,
                        wait_until="domcontentloaded",
                        timeout=self._navigation_timeout_ms,
                    )
                except PlaywrightTimeoutError:
                    if self._observability is not None:
                        self._observability.record_navigation_failure(timed_out=True)
                    failure = (
                        RestoreFailure.STATE_CONTEXT_FAILED
                        if method is RestoreMethod.STORAGE_STATE
                        else RestoreFailure.NAVIGATION_FAILED
                    )
                    return RestoreAttempt(method=method, success=False, failure=failure)
                except Exception:  # noqa: BLE001 - browser adapter boundary
                    if self._observability is not None:
                        self._observability.record_navigation_failure()
                    failure = (
                        RestoreFailure.STATE_CONTEXT_FAILED
                        if method is RestoreMethod.STORAGE_STATE
                        else RestoreFailure.NAVIGATION_FAILED
                    )
                    return RestoreAttempt(method=method, success=False, failure=failure)
                finally:
                    if self._observability is not None:
                        self._observability.record_navigation_duration(
                            time.perf_counter() - navigation_started
                        )
                if response is not None and response.status == 410:
                    if self._observability is not None:
                        self._observability.record_navigation_failure()
                    return RestoreAttempt(
                        method=method,
                        success=False,
                        failure=RestoreFailure.SESSION_EXPIRED,
                        expired=True,
                    )
                if response is not None and response.status >= 400:
                    if self._observability is not None:
                        self._observability.record_navigation_failure()
                    failure = (
                        RestoreFailure.HTTP_FAILURE
                        if response.status >= 500 or response.status in {408, 429}
                        else RestoreFailure.INVALID_TRANSFER_URL
                    )
                    return RestoreAttempt(
                        method=method,
                        success=False,
                        failure=failure,
                    )
                if self._admission_detector is not None and await self._admission_detector.detect(
                    page
                ):
                    return RestoreAttempt(
                        method=method,
                        success=True,
                        identity_match=True,
                        admitted=True,
                    )
                attempt = await self._observe(page, session, method)
                if attempt.success and session.mode is SessionMode.HYBRID and refresh_state:
                    try:
                        state = cast(BrowserState, await context.storage_state())
                        session.state_path = await self._state_store.save(
                            session.session_id,
                            state,
                        )
                    except Exception:  # noqa: BLE001
                        return replace(
                            attempt,
                            success=False,
                            failure=RestoreFailure.STATE_REFRESH_FAILED,
                        )
                    return replace(attempt, state_refreshed=True)
                return attempt
        # This is the browser adapter boundary: third-party context/page
        # implementations can surface more than Playwright's public errors.
        except PlaywrightTimeoutError:
            failure = (
                RestoreFailure.STATE_CONTEXT_FAILED
                if method is RestoreMethod.STORAGE_STATE
                else RestoreFailure.NAVIGATION_FAILED
            )
            return RestoreAttempt(method=method, success=False, failure=failure)
        except Exception:  # noqa: BLE001
            failure = (
                RestoreFailure.STATE_CONTEXT_FAILED
                if method is RestoreMethod.STORAGE_STATE
                else RestoreFailure.NAVIGATION_FAILED
            )
            return RestoreAttempt(method=method, success=False, failure=failure)

    async def _observe(
        self,
        page: Page,
        session: QueueSession,
        method: RestoreMethod,
    ) -> RestoreAttempt:
        deadline = asyncio.get_running_loop().time() + self._observation_timeout_seconds
        latest_progress: QueueProgress | None = None
        latest_transfer: TransferExtractionResult | None = None
        extractor = self._transfer_extractor_factory(
            self._expected_journey_url or session.transfer_url
        )
        while True:
            terminal_state = await self._terminal_state_detector.detect(page)
            if terminal_state is not None:
                failure = (
                    RestoreFailure.SESSION_EXPIRED
                    if terminal_state is TerminalQueueState.EXPIRED
                    else RestoreFailure.EVENT_CLOSED
                )
                return RestoreAttempt(
                    method=method,
                    success=False,
                    failure=failure,
                    expired=True,
                )
            if self._admission_detector is not None and await self._admission_detector.detect(page):
                return RestoreAttempt(
                    method=method,
                    success=True,
                    identity_match=True,
                    admitted=True,
                )
            latest_progress = await self._live_extractor.extract(
                page,
                session_id=session.session_id,
            )
            if latest_progress.turn_started is True:
                if self._admission_detector is not None and await self._admission_detector.detect(
                    page,
                    wait_timeout_ms=self._admission_wait_timeout_ms,
                ):
                    return RestoreAttempt(
                        method=method,
                        success=True,
                        identity_match=True,
                        progress=latest_progress,
                        admitted=True,
                    )
                return RestoreAttempt(
                    method=method,
                    success=True,
                    observed_queue_id=session.queue_id,
                    identity_match=True,
                    progress=latest_progress,
                )
            latest_transfer = await extractor.extract(
                page,
                expected_queue_id=session.queue_id,
            )
            if latest_transfer.identity_mismatch:
                return RestoreAttempt(
                    method=method,
                    success=False,
                    observed_queue_id=latest_transfer.observed_queue_id,
                    identity_match=False,
                    progress=latest_progress,
                    failure=RestoreFailure.IDENTITY_MISMATCH,
                )
            if latest_transfer.successful and latest_transfer.observed_queue_id is not None:
                identity_match = latest_transfer.observed_queue_id == session.queue_id
                return RestoreAttempt(
                    method=method,
                    success=identity_match,
                    observed_queue_id=latest_transfer.observed_queue_id,
                    identity_match=identity_match,
                    progress=latest_progress,
                    failure=None if identity_match else RestoreFailure.IDENTITY_MISMATCH,
                )
            if latest_transfer.failure in {
                TransferFailure.MALFORMED_URL,
                TransferFailure.UNEXPECTED_HOST,
                TransferFailure.UNEXPECTED_JOURNEY,
                TransferFailure.AMBIGUOUS_QUEUE_ID,
            }:
                return RestoreAttempt(
                    method=method,
                    success=False,
                    observed_queue_id=latest_transfer.observed_queue_id,
                    progress=latest_progress,
                    failure=RestoreFailure.INVALID_TRANSFER_URL,
                )
            if asyncio.get_running_loop().time() >= deadline:
                failure = (
                    RestoreFailure.IDENTITY_UNVERIFIED
                    if latest_transfer.successful
                    else RestoreFailure.TRANSFER_UNAVAILABLE
                )
                return RestoreAttempt(
                    method=method,
                    success=False,
                    observed_queue_id=latest_transfer.observed_queue_id,
                    progress=latest_progress,
                    failure=failure,
                )
            await self._sleep(self._observation_interval_seconds)

    async def _record(
        self,
        session: QueueSession,
        result: SessionRestoreResult,
    ) -> None:
        session.last_checked_at = datetime.now(UTC)
        session.attempt_count += 1
        failure_code = result.failure.value if result.failure is not None else "UNKNOWN"
        session.last_error = None if result.success else f"restore:{failure_code}"
        await self._repository.update(session, result.progress)

    @staticmethod
    def _valid_transfer_url(value: str) -> bool:
        if any(character.isspace() for character in value):
            return False
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        )

    @staticmethod
    def _result(
        final_attempt: RestoreAttempt,
        expected_queue_id: str | None,
        attempts: list[RestoreAttempt] | None = None,
    ) -> SessionRestoreResult:
        return SessionRestoreResult(
            method=final_attempt.method,
            success=final_attempt.success,
            expected_queue_id=expected_queue_id,
            observed_queue_id=final_attempt.observed_queue_id,
            identity_match=final_attempt.identity_match,
            progress=final_attempt.progress,
            failure=final_attempt.failure,
            state_refreshed=final_attempt.state_refreshed,
            admitted=final_attempt.admitted,
            expired=final_attempt.expired,
            attempts=tuple(attempts or (final_attempt,)),
        )
