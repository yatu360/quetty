"""Manual Strategy acquisition: one operator-owned visible window at a time."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from queue_load_test.metrics.logging import log_event
from queue_load_test.models import QueueSession, QueueStatus
from queue_load_test.repository import SessionRepository
from queue_load_test.scheduler import QueueSessionCreator
from queue_load_test.scheduler.creation import (
    AcquisitionFailure,
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
)
from queue_load_test.state import StateStore
from queue_load_test.web.manual import (
    ManualChromeSessionManager,
    ManualCloseReason,
    ManualOpenError,
    ManualWindowResult,
)

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], Awaitable[None]]

# Sanitized FAILED-row reasons for a window that ended without a Queue ID.
MANUAL_WINDOW_CLOSED = "manual_window_closed_before_queue_id"
MANUAL_BROWSER_LOST = "manual_browser_lost_before_queue_id"
MANUAL_OWNERSHIP_LOST = "manual_ownership_lost_before_queue_id"
MANUAL_WINDOW_OPEN_FAILED = "manual_window_open_failed"
MANUAL_ACQUISITION_INTERRUPTED = "manual_acquisition_interrupted"

_CLOSE_FAILURES = {
    ManualCloseReason.OPERATOR_CLOSED: MANUAL_WINDOW_CLOSED,
    ManualCloseReason.DASHBOARD_CLOSE: MANUAL_WINDOW_CLOSED,
    ManualCloseReason.BROWSER_LOST: MANUAL_BROWSER_LOST,
    ManualCloseReason.OWNERSHIP_LOST: MANUAL_OWNERSHIP_LOST,
}


class ManualAcquisitionHandler:
    """Creation handler for Manual Strategy runs.

    The run's ``SessionCreationController`` drives it with exactly one worker and a
    one-slot queue, so window N+1 is only requested after ``create`` returns for
    window N, and ``create`` returns only after window N has ended. Each attempt is a
    fresh reserved QueueSession (its own sticky proxy ID on a proxied run); a valid
    Queue ID is persisted while the window stays open, and a window closed without
    one is recorded as a FAILED attempt that never counts toward the target.
    """

    def __init__(
        self,
        *,
        creator: QueueSessionCreator,
        manual_sessions: ManualChromeSessionManager,
        repository: SessionRepository,
        state_store: StateStore,
        open_failure_delay_seconds: float = 5.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if open_failure_delay_seconds < 0:
            raise ValueError("open_failure_delay_seconds cannot be negative")
        self._creator = creator
        self._manual_sessions = manual_sessions
        self._repository = repository
        self._state_store = state_store
        self._open_failure_delay_seconds = open_failure_delay_seconds
        self._sleep = sleep
        # Defence in depth beside the one-worker controller: a second work item waits
        # here, before any reservation or window, until the current window has ended.
        self._sequential = asyncio.Lock()

    async def create(self, work_item: CreationWorkItem) -> CreationOutcome:
        async with self._sequential:
            return await self._create_one(work_item)

    async def _create_one(self, work_item: CreationWorkItem) -> CreationOutcome:
        started = time.perf_counter()
        try:
            return await self._create(work_item, started)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never spin: a failing repository or browser would otherwise make the
            # controller retry immediately.
            await self._sleep(self._open_failure_delay_seconds)
            raise

    async def _create(self, work_item: CreationWorkItem, started: float) -> CreationOutcome:
        reserved = await self._creator.reserve_session(work_item)
        try:
            window = await self._manual_sessions.run_acquisition_window(reserved.session_id)
        except ManualOpenError:
            if self._manual_sessions.closing:
                await self._discard(reserved.session_id)
                return self._outcome(
                    CreationOutcomeKind.TEMPORARY_FAILURE,
                    started,
                    MANUAL_ACQUISITION_INTERRUPTED,
                )
            await self._creator.record_failed_attempt(
                work_item, MANUAL_WINDOW_OPEN_FAILED, reserved
            )
            self._log(reserved.session_id, None, MANUAL_WINDOW_OPEN_FAILED, started)
            await self._sleep(self._open_failure_delay_seconds)
            return self._outcome(
                CreationOutcomeKind.TEMPORARY_FAILURE, started, MANUAL_WINDOW_OPEN_FAILED
            )
        return await self._classify(work_item, reserved, window, started)

    async def _classify(
        self,
        work_item: CreationWorkItem,
        reserved: QueueSession,
        window: ManualWindowResult,
        started: float,
    ) -> CreationOutcome:
        reserved_session_id = reserved.session_id
        session = await self._repository.get(reserved_session_id)
        if session is not None and session.queue_id is not None:
            if session.status is QueueStatus.FAILED:
                # The identity was persisted, then lost (for example a mismatch
                # observed in the window). The row keeps its history untouched.
                self._log(session.session_id, window, "queue_identity_lost", started)
                return self._outcome(
                    CreationOutcomeKind.PERMANENT_FAILURE, started, "queue_identity_lost"
                )
            self._log(session.session_id, window, None, started, status=session.status)
            # The same post-acquisition baseline automatic creation takes, once the
            # window has closed; diagnostic only.
            await self._creator.observe_proxy_ip(session)
            return CreationOutcome(
                kind=CreationOutcomeKind.SUCCESS,
                attempts=1,
                temporary_failures=0,
                duration_seconds=time.perf_counter() - started,
                session=session,
                progress=await self._repository.get_progress(session.session_id),
            )
        if window.close_reason is ManualCloseReason.SHUTDOWN:
            # Interrupted, not unsuccessful: nothing is counted or recorded, and the
            # still-unmet target is resumed with a fresh attempt after restart.
            await self._discard(reserved_session_id)
            self._log(reserved_session_id, window, MANUAL_ACQUISITION_INTERRUPTED, started)
            return self._outcome(
                CreationOutcomeKind.TEMPORARY_FAILURE, started, MANUAL_ACQUISITION_INTERRUPTED
            )
        if window.duplicate_identity:
            code = "duplicate_queue_id"
            kind = CreationOutcomeKind.DUPLICATE
        elif window.access_restricted:
            code = AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.code
            kind = CreationOutcomeKind.PERMANENT_FAILURE
            self._creator.record_access_restricted_attempt(reserved_session_id)
        else:
            code = _CLOSE_FAILURES[window.close_reason]
            kind = CreationOutcomeKind.PERMANENT_FAILURE
        await self._creator.record_failed_attempt(work_item, code, reserved)
        self._log(reserved_session_id, window, code, started)
        return self._outcome(kind, started, code)

    async def _discard(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._repository.delete_unowned_session(session_id)
        with contextlib.suppress(Exception):
            await self._state_store.delete(session_id)

    @staticmethod
    def _outcome(kind: CreationOutcomeKind, started: float, code: str) -> CreationOutcome:
        return CreationOutcome(
            kind=kind,
            attempts=1,
            temporary_failures=1 if kind is CreationOutcomeKind.TEMPORARY_FAILURE else 0,
            duration_seconds=time.perf_counter() - started,
            failure_code=code,
        )

    @staticmethod
    def _log(
        session_id: str,
        window: ManualWindowResult | None,
        failure_code: str | None,
        started: float,
        *,
        status: QueueStatus | None = None,
    ) -> None:
        log_event(
            logger,
            logging.INFO if failure_code is None else logging.WARNING,
            "manual_acquisition_window_ended",
            session_id=session_id,
            status=(status or QueueStatus.FAILED).value,
            classification=window.close_reason.value if window is not None else None,
            error_type=failure_code,
            duration=time.perf_counter() - started,
        )
