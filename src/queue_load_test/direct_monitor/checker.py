"""One bounded direct visitor-status check with conservative classification."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from queue_load_test.direct_monitor.harvest import LOCAL_SIMULATOR_SCOPE, is_loopback_url
from queue_load_test.direct_monitor.models import (
    HARD_FAILURES,
    DirectAttempt,
    DirectCapability,
    DirectFallbackReason,
)
from queue_load_test.direct_monitor.store import (
    DirectMonitorRecord,
    DirectMonitorStateError,
    DirectMonitorStateStore,
)
from queue_load_test.direct_replay import (
    DirectStatusReplayClient,
    HeaderProfile,
    ReplayCookie,
    ReplayEvidenceError,
    ReplayFailure,
    ReplayResult,
    ReplayStateError,
    cookies_from_browser_state,
)
from queue_load_test.direct_replay.client import DETAIL_CONTENT_TYPE
from queue_load_test.metrics.logging import log_event
from queue_load_test.models import (
    MonitoringObservation,
    QueueSession,
    QueueStatus,
    can_transition,
    evaluate_monitoring_observation,
)
from queue_load_test.observation_equivalence import (
    DirectObservationError,
    DirectObservationFailure,
    DirectResponseParser,
)
from queue_load_test.state import BrowserState, StateStoreError

logger = logging.getLogger(__name__)

type Clock = Callable[[], datetime]

# Only in-queue states are persisted from a direct response. Admission, expiry,
# connection loss, and unknown lifecycle always go to the browser monitor, so a
# direct field can never on its own mark a session ADMITTED, EXPIRED, or FAILED.
DIRECT_PERSISTABLE_STATUSES = frozenset(
    {
        QueueStatus.PRE_QUEUE,
        QueueStatus.ACTIVE_QUEUE,
        QueueStatus.PAUSED,
        QueueStatus.SERVICED_SOON,
        QueueStatus.TURN_STARTED,
        QueueStatus.READY,
    }
)

_REPLAY_FAILURES = {
    ReplayFailure.NETWORK: DirectFallbackReason.NETWORK,
    ReplayFailure.TIMEOUT: DirectFallbackReason.TIMEOUT,
    ReplayFailure.HTTP: DirectFallbackReason.UNEXPECTED_HTTP_STATUS,
    ReplayFailure.REDIRECT: DirectFallbackReason.UNEXPECTED_REDIRECT,
    ReplayFailure.STATE: DirectFallbackReason.REJECTED_SESSION_STATE,
    ReplayFailure.IDENTITY: DirectFallbackReason.IDENTITY_MISMATCH,
}
_PARSE_FAILURES = {
    DirectObservationFailure.SCHEMA: DirectFallbackReason.SCHEMA_INCOMPATIBLE,
    DirectObservationFailure.IDENTITY_AMBIGUITY: DirectFallbackReason.IDENTITY_AMBIGUITY,
    DirectObservationFailure.IDENTITY_MISMATCH: DirectFallbackReason.IDENTITY_MISMATCH,
}


class BrowserStateSource(Protocol):
    def path_for(self, session_id: str) -> Path: ...

    async def load(self, session_id: str) -> BrowserState | None: ...


class DirectStatusChecker:
    """Replay a session's own recipe and validate it before anything is persisted."""

    def __init__(
        self,
        *,
        store: DirectMonitorStateStore,
        browser_state: BrowserStateSource,
        parser: DirectResponseParser | None,
        accepted_scopes: Collection[str],
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 65_536,
        failure_threshold: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes < 1 or failure_threshold < 1:
            raise ValueError("direct check bounds must be positive")
        self._store = store
        self._browser_state = browser_state
        self._parser = parser
        self._scopes = frozenset(accepted_scopes)
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._failure_threshold = failure_threshold
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def enabled(self) -> bool:
        """Whether a reviewed schema exists; without one every check falls back."""

        return self._parser is not None

    async def attempt(self, session: QueueSession) -> DirectAttempt:
        queue_id = session.queue_id
        if queue_id is None:
            return DirectAttempt(fallback_reason=DirectFallbackReason.NO_QUEUE_ID)
        parser = self._parser
        if parser is None:
            return DirectAttempt(fallback_reason=DirectFallbackReason.SCHEMA_UNAVAILABLE)
        try:
            record = await self._store.load(session.session_id)
        except DirectMonitorStateError:
            # Report-only: a corrupt record is left in place for the consistency
            # report and never used. Only a new legitimate browser observation may
            # replace it.
            return DirectAttempt(fallback_reason=DirectFallbackReason.RECIPE_UNCERTAIN)
        if record is None or record.capability is DirectCapability.DISCOVERY_REQUIRED:
            return DirectAttempt(fallback_reason=DirectFallbackReason.DISCOVERY_REQUIRED)
        if record.expected_queue_id != queue_id:
            # Never reconciled and never reused: a record for another identity stays
            # as evidence until a new observation for this identity replaces it.
            return DirectAttempt(fallback_reason=DirectFallbackReason.RECIPE_UNCERTAIN)
        if record.capability is DirectCapability.DIRECT_UNAVAILABLE:
            return DirectAttempt(fallback_reason=DirectFallbackReason.DIRECT_UNAVAILABLE)
        started = time.perf_counter()
        reason, observation = await self._replay(session, queue_id, record, parser)
        elapsed = time.perf_counter() - started
        if reason is not None:
            await self._record_failure(record, reason)
            return DirectAttempt(
                fallback_reason=reason,
                rejected_observation=observation,
                request_seconds=elapsed,
            )
        assert observation is not None
        try:
            await self._store.record_success(record)
        except Exception as exc:  # noqa: BLE001 - bookkeeping never discards a valid check
            log_event(
                logger,
                logging.WARNING,
                "direct_monitor_record_save_failed",
                session_id=record.session_id,
                operation="success",
                error_type=type(exc).__name__,
            )
        return DirectAttempt(observation=observation, request_seconds=elapsed)

    async def _replay(
        self,
        session: QueueSession,
        queue_id: str,
        record: DirectMonitorRecord,
        parser: DirectResponseParser,
    ) -> tuple[DirectFallbackReason | None, MonitoringObservation | None]:
        recipe = record.recipe
        if (
            recipe is None
            or recipe.source_scope not in self._scopes
            or (recipe.source_scope == LOCAL_SIMULATOR_SCOPE and not is_loopback_url(recipe.url))
        ):
            return DirectFallbackReason.RECIPE_UNCERTAIN, None
        cookies = await self._cookies(session.session_id, recipe.fingerprint)
        if cookies is None:
            return DirectFallbackReason.MISSING_VISITOR_STATE, None
        try:
            async with DirectStatusReplayClient(
                recipe=recipe,
                cookies=cookies,
                state_store=self._store.cookies,
                timeout_seconds=self._timeout_seconds,
                max_response_bytes=self._max_response_bytes,
                max_connections=1,
                max_concurrency=1,
                transport=self._transport,
            ) as client:
                result = await client.replay(HeaderProfile.FULL_DERIVED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. protected cookie persistence failed
            # Only the exception type is recorded: HTTP-library messages can carry
            # the request URL or headers.
            log_event(
                logger,
                logging.WARNING,
                "direct_monitor_request_error",
                session_id=session.session_id,
                error_type=type(exc).__name__,
            )
            return DirectFallbackReason.UNCERTAIN, None
        if not result.succeeded or result.response_json is None:
            return _replay_reason(result), None
        try:
            observation = parser.parse(
                result.response_json,
                session_id=session.session_id,
                expected_queue_id=queue_id,
                observed_at=self._clock(),
            )
        except DirectObservationError as exc:
            return _PARSE_FAILURES[exc.failure], None
        return validate_direct_observation(session, observation), observation

    async def _cookies(
        self, session_id: str, fingerprint: str
    ) -> tuple[ReplayCookie, ...] | None:
        """Use whichever of browser state or replay-refreshed cookies is newer."""

        replay_cookies: tuple[ReplayCookie, ...] | None = None
        try:
            replay_cookies = await self._store.cookies.load(
                session_id=session_id, recipe_fingerprint=fingerprint
            )
        except ReplayStateError:
            with contextlib.suppress(OSError, ReplayStateError):
                await self._store.cookies.delete(session_id)
        if replay_cookies is not None and _mtime(self._store.cookie_path(session_id)) >= _mtime(
            self._browser_state.path_for(session_id)
        ):
            return replay_cookies
        try:
            state = await self._browser_state.load(session_id)
        except (StateStoreError, OSError, ValueError):
            return replay_cookies
        if state is None:
            return replay_cookies
        try:
            return cookies_from_browser_state(state)
        except ReplayEvidenceError:
            return replay_cookies

    async def _record_failure(
        self, record: DirectMonitorRecord, reason: DirectFallbackReason
    ) -> None:
        make_unavailable = (
            reason in HARD_FAILURES
            or record.consecutive_failures + 1 >= self._failure_threshold
        )
        try:
            updated = await self._store.record_failure(
                record, reason=reason, make_unavailable=make_unavailable
            )
        except Exception as exc:  # noqa: BLE001 - the fallback still runs; nothing is lost
            log_event(
                logger,
                logging.WARNING,
                "direct_monitor_record_save_failed",
                session_id=record.session_id,
                operation="failure",
                error_type=type(exc).__name__,
            )
            return
        if updated.capability is DirectCapability.DIRECT_UNAVAILABLE:
            log_event(
                logger,
                logging.WARNING,
                "direct_capability_unavailable",
                session_id=record.session_id,
                error_type=reason.value,
                count=updated.consecutive_failures,
            )


def validate_direct_observation(
    session: QueueSession, observation: MonitoringObservation
) -> DirectFallbackReason | None:
    """Accept only an unambiguous in-queue observation the lifecycle allows."""

    progress, page = observation.progress, observation.page
    if observation.identity_match is not True or observation.observed_queue_id != session.queue_id:
        return DirectFallbackReason.IDENTITY_MISMATCH
    pre = page.pre_queue is True or progress.pre_queue is True
    active = page.active_queue is True or progress.active_queue is True
    late = any(
        value is True
        for value in (progress.serviced_soon, progress.first_in_line, progress.turn_started)
    )
    if (pre and active) or (pre and late) or (page.admitted is True and page.expired is True):
        return DirectFallbackReason.CONTRADICTORY_LIFECYCLE
    if page.expired is True:
        return DirectFallbackReason.REJECTED_SESSION_STATE
    if observation.redirect_present is True or page.admitted is True:
        return DirectFallbackReason.UNSUPPORTED_ADMISSION
    if progress.connection_lost is True:
        return DirectFallbackReason.UNKNOWN_LIFECYCLE
    status = evaluate_monitoring_observation(observation)
    if status not in DIRECT_PERSISTABLE_STATUSES:
        return DirectFallbackReason.UNKNOWN_LIFECYCLE
    if not can_transition(session.status, status):
        return DirectFallbackReason.CONTRADICTORY_LIFECYCLE
    return None


def _replay_reason(result: ReplayResult) -> DirectFallbackReason:
    failure = result.failure
    if failure is None:
        return DirectFallbackReason.MALFORMED_RESPONSE
    if failure is ReplayFailure.SCHEMA:
        if result.detail == DETAIL_CONTENT_TYPE:
            return DirectFallbackReason.UNEXPECTED_CONTENT_TYPE
        return DirectFallbackReason.MALFORMED_RESPONSE
    return _REPLAY_FAILURES[failure]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
