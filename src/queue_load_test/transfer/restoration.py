"""Restore persisted Queue-it sessions without changing their expected identity."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page

from queue_load_test.browser import BrowserCapacityError, BrowserManager, OwnedBrowserContext
from queue_load_test.browser.errors import BROWSER_TIMEOUT_ERROR_TYPES
from queue_load_test.browser.manager import ContextStorageState
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import (
    BrowserBackendName,
    QueueProgress,
    QueueSession,
    SessionMode,
    evaluate_queue_status,
)
from queue_load_test.proxy import (
    ProxyAuthWatch,
    ProxyFailure,
    ProxyResolutionError,
    ResolvedSessionProxy,
    SessionProxyResolver,
    classify_proxy_error,
    resolve_session_proxy,
)
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
from queue_load_test.status_discovery import (
    DiscoveryDomSnapshot,
    StatusDiscoveryFactoryProtocol,
)
from queue_load_test.transfer.extractor import (
    QueueItTransferExtractor,
    TransferExtractionResult,
    TransferFailure,
)
from queue_load_test.utils.asyncio_tools import AbandonedOperationError, await_bounded

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
    # Transport failures of a session's persisted proxy (Phase 9). Transient: they
    # never change the Queue ID and are retried later through the same sticky session.
    PROXY_CONFIG_MISSING = ProxyFailure.PROXY_CONFIG_MISSING.value
    PROXY_ASSIGNMENT_MISSING = ProxyFailure.PROXY_ASSIGNMENT_MISSING.value
    PROXY_ASSIGNMENT_INVALID = ProxyFailure.PROXY_ASSIGNMENT_INVALID.value
    PROXY_AUTH_FAILED = ProxyFailure.PROXY_AUTH_FAILED.value
    PROXY_CONNECT_FAILED = ProxyFailure.PROXY_CONNECT_FAILED.value
    PROXY_UNSUPPORTED_BACKEND = ProxyFailure.PROXY_UNSUPPORTED_BACKEND.value


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
        status_discovery_factory: StatusDiscoveryFactoryProtocol | None = None,
        proxy_resolver: SessionProxyResolver | None = None,
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
        self._status_discovery_factory = status_discovery_factory
        self._proxy_resolver = proxy_resolver
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
        result = self._backend_mismatch_result(session)
        if result is None:
            proxy, result = self._resolve_proxy(session)
            if result is None:
                result = await self._restore(session, proxy=proxy)
            else:
                await self._record(session, result)
        elif result.failure is RestoreFailure.BACKEND_MISMATCH:
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
        proxy: ResolvedSessionProxy | None = None
        if result is None:
            proxy, result = self._resolve_proxy(session, method=method)
        if result is None:
            result = await self._restore_with_method(session, method, proxy=proxy)
        else:
            await self._record(session, result)
        self._observe_restore_result(session, result, time.perf_counter() - started)
        return result

    async def restore_open(
        self,
        session: QueueSession,
        *,
        keep_open_on_navigation_failure: bool = False,
    ) -> OpenedSessionRestore:
        """Restore a session while retaining only a verified live context.

        ``keep_open_on_navigation_failure`` applies to a session without a Queue ID
        (a Manual Strategy acquisition window): a failed first navigation leaves the
        operator-owned window open instead of discarding it.
        """

        started = time.perf_counter()
        mismatch = self._backend_mismatch_result(session)
        if mismatch is not None:
            await self._record(session, mismatch)
            self._observe_restore_result(session, mismatch, time.perf_counter() - started)
            return OpenedSessionRestore(mismatch)
        expected_queue_id = session.queue_id
        proxy, proxy_failure = self._resolve_proxy(session)
        if proxy_failure is not None:
            # Fail closed before any context: no unproxied window is ever opened.
            if expected_queue_id is not None:
                await self._record(session, proxy_failure)
            self._observe_restore_result(session, proxy_failure, time.perf_counter() - started)
            return OpenedSessionRestore(proxy_failure)
        if expected_queue_id is None:
            return await self._open_unidentified(
                session,
                proxy=proxy,
                keep_open_on_navigation_failure=keep_open_on_navigation_failure,
            )
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
                proxy=proxy,
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
                        proxy=proxy,
                    )
                    attempts.append(state_opened.result.attempts[-1])
                    opened = replace(
                        state_opened,
                        result=self._result(attempts[-1], expected_queue_id, attempts),
                    )
        await self._record(session, opened.result)
        self._observe_restore_result(session, opened.result, time.perf_counter() - started)
        return opened

    async def _open_unidentified(
        self,
        session: QueueSession,
        *,
        proxy: ResolvedSessionProxy | None,
        keep_open_on_navigation_failure: bool = False,
    ) -> OpenedSessionRestore:
        """Open a window for a session that never acquired a Queue ID.

        There is no identity to verify, so nothing is recorded: the persisted
        status and error stay as they are until an identity is adopted. With
        ``keep_open_on_navigation_failure`` a failed navigation (timeout, network or
        proxy error) is classified and counted, but the window stays open for the
        operator, who may reload it or close it.
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

        auth_watch: ProxyAuthWatch | None = None

        async def navigate() -> tuple[OwnedBrowserContext, Page]:
            nonlocal auth_watch
            owned = await self._browser_manager.create_context(
                storage_state=storage_state, **self._proxy_options(proxy)
            )
            try:
                page = await owned.context.new_page()
                auth_watch = self._watch_proxy_auth(page, proxy)
            except BaseException:
                await owned.close()
                raise
            try:
                await page.goto(
                    navigation_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
            except asyncio.CancelledError:
                await owned.close()
                raise
            except Exception as exc:  # browser adapter boundary, includes timeouts
                if not keep_open_on_navigation_failure:
                    await owned.close()
                    raise
                failure = (
                    self._proxy_navigation_failure(proxy, exc, auth_watch)
                    or RestoreFailure.NAVIGATION_FAILED
                )
                if self._observability is not None:
                    self._observability.record_navigation_failure()
                log_event(
                    logger,
                    logging.WARNING,
                    "manual_window_navigation_failed",
                    session_id=session.session_id,
                    error_type=failure.value,
                )
            except BaseException:
                await owned.close()
                raise
            return owned, page

        async def discard(opened: tuple[OwnedBrowserContext, Page]) -> None:
            await opened[0].close()

        try:
            owned, page = await await_bounded(
                navigate(), timeout=self._attempt_timeout_seconds, discard=discard
            )
        except (asyncio.CancelledError, BrowserCapacityError):
            raise
        except Exception as exc:  # noqa: BLE001 - browser adapter boundary, includes timeouts
            return OpenedSessionRestore(
                self._result(
                    RestoreAttempt(
                        method=RestoreMethod.TRANSFER,
                        success=False,
                        failure=self._proxy_navigation_failure(proxy, exc, auth_watch)
                        or RestoreFailure.NAVIGATION_FAILED,
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
        require_live_queue: bool = True,
    ) -> SessionRestoreResult | None:
        """Adopt a live Queue-it identity observed in an unidentified open window.

        Returns ``None`` while no live queue with a transfer identity is visible.
        The session is updated in memory only; the caller persists the result.
        A live page that stops answering is bounded and reported as "nothing seen".
        With ``require_live_queue=False`` (Manual Strategy acquisition) a valid page
        transfer identity is adopted even when no pre-queue or active-queue marker
        is visible; the identity-only lifecycle fallback then applies.
        """

        original = (session.queue_id, session.transfer_url, session.last_error)
        try:
            return await await_bounded(
                self._adopt_open(
                    session,
                    context=context,
                    page=page,
                    require_live_queue=require_live_queue,
                ),
                timeout=self._attempt_timeout_seconds,
            )
        except TimeoutError as exc:
            self._record_live_page_timeout(context, exc)
            session.queue_id, session.transfer_url, session.last_error = original
            return None

    def _record_live_page_timeout(self, context: BrowserContext, exc: TimeoutError) -> None:
        """Count a live-page timeout; an unstoppable call means the process is wedged."""

        if self._observability is not None:
            self._observability.record_browser_operation_timeout()
        if isinstance(exc, AbandonedOperationError):
            self._browser_manager.report_abandoned_operation(context)

    async def _adopt_open(
        self,
        session: QueueSession,
        *,
        context: BrowserContext,
        page: Page,
        require_live_queue: bool = True,
    ) -> SessionRestoreResult | None:
        progress = await self._live_extractor.extract(page, session_id=session.session_id)
        if (
            require_live_queue
            and progress.pre_queue is not True
            and progress.active_queue is not True
        ):
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
        """Inspect and refresh a still-open verified manual session.

        Bounded like every restore attempt: a live page whose browser has stopped
        answering (for example a wedged Camoufox process that still reports
        connected) must not block Close or application shutdown indefinitely. A
        timeout is a transient observation failure; the expected Queue ID is kept.
        """


        async def inspect() -> RestoreAttempt:
            attempt = await self._observe(page, session, RestoreMethod.TRANSFER)
            if attempt.success and session.mode is SessionMode.HYBRID:
                attempt = await self._refresh_state(session, context, attempt)
            return attempt

        try:
            attempt = await await_bounded(inspect(), timeout=self._attempt_timeout_seconds)
        except TimeoutError as exc:
            self._record_live_page_timeout(context, exc)
            attempt = RestoreAttempt(
                method=RestoreMethod.TRANSFER,
                success=False,
                failure=RestoreFailure.NAVIGATION_FAILED,
            )
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
        *,
        proxy: ResolvedSessionProxy | None = None,
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
                    proxy=proxy,
                )
        else:
            attempt = await self._storage_state_attempt(
                session, refresh_state=False, proxy=proxy
            )
        result = self._result(attempt, expected_queue_id)
        await self._record(session, result)
        return result

    async def _restore(
        self,
        session: QueueSession,
        *,
        proxy: ResolvedSessionProxy | None = None,
    ) -> SessionRestoreResult:
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
            proxy=proxy,
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

        state_attempt = await self._storage_state_attempt(session, proxy=proxy)
        attempts.append(state_attempt)
        result = self._result(state_attempt, expected_queue_id, attempts)
        await self._record(session, result)
        return result

    async def _storage_state_attempt(
        self,
        session: QueueSession,
        *,
        refresh_state: bool = True,
        proxy: ResolvedSessionProxy | None = None,
    ) -> RestoreAttempt:
        state, failure = await self._load_storage_state(session)
        if failure is not None:
            return RestoreAttempt(method=RestoreMethod.STORAGE_STATE, success=False, failure=failure)
        return await self._browser_attempt(
            session,
            method=RestoreMethod.STORAGE_STATE,
            storage_state=cast(ContextStorageState, state),
            refresh_state=refresh_state,
            proxy=proxy,
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
        proxy: ResolvedSessionProxy | None = None,
    ) -> RestoreAttempt:
        try:
            return await await_bounded(
                self._unbounded_browser_attempt(
                    session,
                    method=method,
                    storage_state=storage_state,
                    refresh_state=refresh_state,
                    proxy=proxy,
                ),
                timeout=self._attempt_timeout_seconds,
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
        proxy: ResolvedSessionProxy | None = None,
    ) -> OpenedSessionRestore:
        async def discard(opened: OpenedSessionRestore) -> None:
            if opened.owned_context is not None:
                await opened.owned_context.close()

        try:
            return await await_bounded(
                self._unbounded_open_browser_attempt(
                    session,
                    method=method,
                    storage_state=storage_state,
                    proxy=proxy,
                ),
                timeout=self._attempt_timeout_seconds,
                discard=discard,
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
        proxy: ResolvedSessionProxy | None = None,
    ) -> OpenedSessionRestore:
        owned: OwnedBrowserContext | None = None
        try:
            owned = await self._browser_manager.create_context(
                storage_state=storage_state, **self._proxy_options(proxy)
            )
            context = owned.context
            page = await context.new_page()
            auth_watch = self._watch_proxy_auth(page, proxy)
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
                self._browser_manager.report_navigation(context, responsive=True)
            except BROWSER_TIMEOUT_ERROR_TYPES:
                self._browser_manager.report_navigation(context, responsive=False)
                if self._observability is not None:
                    self._observability.record_navigation_failure(timed_out=True)
                failure = (
                    RestoreFailure.STATE_CONTEXT_FAILED
                    if method is RestoreMethod.STORAGE_STATE
                    else RestoreFailure.NAVIGATION_FAILED
                )
                attempt = RestoreAttempt(method=method, success=False, failure=failure)
            except Exception as exc:  # noqa: BLE001
                if self._observability is not None:
                    self._observability.record_navigation_failure()
                failure = self._proxy_navigation_failure(proxy, exc, auth_watch) or (
                    RestoreFailure.STATE_CONTEXT_FAILED
                    if method is RestoreMethod.STORAGE_STATE
                    else RestoreFailure.NAVIGATION_FAILED
                )
                attempt = RestoreAttempt(method=method, success=False, failure=failure)
            else:
                if response is not None and response.status == 407 and proxy is not None:
                    attempt = RestoreAttempt(
                        method=method,
                        success=False,
                        failure=self._record_proxy_failure(ProxyFailure.PROXY_AUTH_FAILED),
                    )
                elif response is not None and response.status == 410:
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
                    page, queue_url=session.transfer_url
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
        proxy: ResolvedSessionProxy | None = None,
    ) -> RestoreAttempt:
        try:
            async with self._browser_manager.context(
                storage_state=storage_state, **self._proxy_options(proxy)
            ) as context:
                page = await context.new_page()
                factory = self._status_discovery_factory
                if factory is None:
                    return await self._inspect_browser_page(
                        context,
                        page,
                        session,
                        method=method,
                        refresh_state=refresh_state,
                        proxy=proxy,
                    )
                observation = factory.create(
                    page=page,
                    context=context,
                    session_id=session.session_id,
                    expected_queue_id=session.queue_id,
                    dom_snapshot_provider=lambda: self._discovery_dom_snapshot(page, session),
                )
                async with observation:
                    return await self._inspect_browser_page(
                        context,
                        page,
                        session,
                        method=method,
                        refresh_state=refresh_state,
                        proxy=proxy,
                    )
        # This is the browser adapter boundary: third-party context/page
        # implementations can surface more than Playwright's public errors.
        except BROWSER_TIMEOUT_ERROR_TYPES:
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

    async def _inspect_browser_page(
        self,
        context: BrowserContext,
        page: Page,
        session: QueueSession,
        *,
        method: RestoreMethod,
        refresh_state: bool,
        proxy: ResolvedSessionProxy | None = None,
    ) -> RestoreAttempt:
        """Run the existing browser restore path, optionally surrounded by observation."""

        auth_watch = self._watch_proxy_auth(page, proxy)
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
            self._browser_manager.report_navigation(context, responsive=True)
        except BROWSER_TIMEOUT_ERROR_TYPES:
            self._browser_manager.report_navigation(context, responsive=False)
            if self._observability is not None:
                self._observability.record_navigation_failure(timed_out=True)
            failure = (
                RestoreFailure.STATE_CONTEXT_FAILED
                if method is RestoreMethod.STORAGE_STATE
                else RestoreFailure.NAVIGATION_FAILED
            )
            return RestoreAttempt(method=method, success=False, failure=failure)
        except Exception as exc:  # noqa: BLE001 - browser adapter boundary
            if self._observability is not None:
                self._observability.record_navigation_failure()
            failure = self._proxy_navigation_failure(proxy, exc, auth_watch) or (
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
        if response is not None and response.status == 407 and proxy is not None:
            if self._observability is not None:
                self._observability.record_navigation_failure()
            return RestoreAttempt(
                method=method,
                success=False,
                failure=self._record_proxy_failure(ProxyFailure.PROXY_AUTH_FAILED),
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
            return RestoreAttempt(method=method, success=False, failure=failure)
        if self._admission_detector is not None and await self._admission_detector.detect(
            page, queue_url=session.transfer_url
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

    async def _discovery_dom_snapshot(
        self,
        page: Page,
        session: QueueSession,
    ) -> DiscoveryDomSnapshot:
        progress = await self._live_extractor.extract(page, session_id=session.session_id)
        lifecycle = evaluate_queue_status(progress).value
        return DiscoveryDomSnapshot(
            observed_at=datetime.now(UTC).isoformat(),
            page_url=page.url,
            lifecycle_status=lifecycle,
            queue_number=progress.queue_number,
            users_ahead=progress.users_ahead,
            progress_percentage=progress.progress_percentage,
            estimated_wait_text=progress.estimated_wait_text,
            expected_service_time=(
                progress.expected_service_time.isoformat()
                if progress.expected_service_time is not None
                else None
            ),
            queue_paused=progress.queue_paused,
            serviced_soon=progress.serviced_soon,
            turn_started=progress.turn_started,
            pre_queue=progress.pre_queue,
            active_queue=progress.active_queue,
        )

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
            if self._admission_detector is not None and await self._admission_detector.detect(
                page, queue_url=session.transfer_url
            ):
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
                    queue_url=session.transfer_url,
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

    def _resolve_proxy(
        self,
        session: QueueSession,
        *,
        method: RestoreMethod = RestoreMethod.TRANSFER,
    ) -> tuple[ResolvedSessionProxy | None, SessionRestoreResult | None]:
        """Resolve the session's persisted sticky session, or a fail-closed result."""

        try:
            return resolve_session_proxy(self._proxy_resolver, session), None
        except ProxyResolutionError as exc:
            attempt = RestoreAttempt(
                method=method, success=False, failure=RestoreFailure(exc.failure.value)
            )
            return None, self._result(attempt, session.queue_id)

    def _proxy_options(self, proxy: ResolvedSessionProxy | None) -> dict[str, Any]:
        """Context options for one proxied context; empty (unchanged call) when unproxied."""

        if proxy is None:
            return {}
        if self._proxy_resolver is not None:
            self._proxy_resolver.record_attempt()
        return {"proxy": proxy.browser_proxy()}

    def _record_proxy_failure(self, failure: ProxyFailure) -> RestoreFailure:
        if self._proxy_resolver is not None:
            self._proxy_resolver.record_failure(failure)
        return RestoreFailure(failure.value)

    @staticmethod
    def _watch_proxy_auth(
        page: Page, proxy: ResolvedSessionProxy | None
    ) -> ProxyAuthWatch | None:
        return ProxyAuthWatch.attach(page) if proxy is not None else None

    def _proxy_navigation_failure(
        self,
        proxy: ResolvedSessionProxy | None,
        exc: BaseException,
        watch: ProxyAuthWatch | None = None,
    ) -> RestoreFailure | None:
        """Classify a proxied navigation error; its text is never logged or kept."""

        if proxy is None:
            return None
        failure = classify_proxy_error(exc, watch)
        return self._record_proxy_failure(failure) if failure is not None else None

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
