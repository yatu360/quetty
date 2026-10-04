"""Bounded headed-browser ownership for operator-opened persisted sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from playwright.async_api import Page

from queue_load_test.browser import BrowserCapacityError, BrowserManager, OwnedBrowserContext
from queue_load_test.metrics.logging import log_event
from queue_load_test.models import QueueProgress, QueueStatus
from queue_load_test.queue_monitor import (
    AccessRestrictionDetector,
    RenderedAccessRestrictionDetector,
)
from queue_load_test.repository import (
    ManualSessionBusyError,
    ManualSessionCapacityError,
    QueueIdConflictError,
    SessionNotFoundError,
    SessionRepository,
)
from queue_load_test.scheduler import QueueSessionMonitor
from queue_load_test.scheduler.monitoring import MonitoringOutcome, is_verified_observation
from queue_load_test.transfer import OpenedSessionRestore, QueueSessionRestorer
from queue_load_test.utils.asyncio_tools import await_bounded

logger = logging.getLogger(__name__)


class ManualOpenStatus(StrEnum):
    OPENED = "OPENED"
    ALREADY_OPEN = "ALREADY_OPEN"


@dataclass(frozen=True, slots=True)
class ManualOpenResult:
    status: ManualOpenStatus
    message: str


class ManualOpenError(RuntimeError):
    """Sanitized operator-visible failure."""


class ManualCloseReason(StrEnum):
    """Why a headed window stopped being owned; only the operator advances acquisition."""

    # The operator closed the window (its last page or its context) in the browser.
    OPERATOR_CLOSED = "OPERATOR_CLOSED"
    # The operator pressed Close on the dashboard.
    DASHBOARD_CLOSE = "DASHBOARD_CLOSE"
    # The headed browser process disconnected or crashed.
    BROWSER_LOST = "BROWSER_LOST"
    # The persisted manual lease could not be renewed.
    OWNERSHIP_LOST = "OWNERSHIP_LOST"
    # Application shutdown, Stop & Reset, or runtime close.
    SHUTDOWN = "SHUTDOWN"


class ManualAcquisitionState(StrEnum):
    """Sanitized state of the open Manual Strategy acquisition window."""

    AWAITING_QUEUE_ID = "AWAITING_QUEUE_ID"
    QUEUE_ID_ACQUIRED = "QUEUE_ID_ACQUIRED"
    ACCESS_RESTRICTED_BEFORE_QUEUE = "ACCESS_RESTRICTED_BEFORE_QUEUE"


@dataclass(frozen=True, slots=True)
class ManualWindowResult:
    """How one Manual Strategy acquisition window ended; never contains identities."""

    session_id: str
    close_reason: ManualCloseReason
    identity_adopted: bool
    access_restricted: bool
    duplicate_identity: bool


@dataclass(slots=True)
class _OpenSession:
    session_id: str
    owner_id: str
    owned_context: OwnedBrowserContext
    page: Page
    closed: asyncio.Event
    watcher: asyncio.Task[None] | None = None
    identity_pending: bool = False
    adoption_blocked: bool = False
    # Manual Strategy acquisition window: operator-owned until the operator closes it.
    acquisition: bool = False
    close_reason: ManualCloseReason | None = None
    access_restricted: bool = False
    identity_adopted: bool = False
    last_inspected: float = 0.0
    finalizing: bool = False


class ManualChromeSessionManager:
    """Own one shared headed pool and persisted per-session operator leases."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        browser_manager: BrowserManager,
        restorer: QueueSessionRestorer,
        monitor: QueueSessionMonitor,
        capacity: int,
        lease_seconds: float,
        restriction_detector: AccessRestrictionDetector | None = None,
        acquisition_poll_seconds: float = 2.0,
        restriction_check_timeout_seconds: float = 10.0,
    ) -> None:
        if acquisition_poll_seconds <= 0:
            raise ValueError("acquisition_poll_seconds must be positive")
        self._repository = repository
        self._browser_manager = browser_manager
        self._restorer = restorer
        self._monitor = monitor
        self._capacity = capacity
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = max(1.0, lease_seconds / 3)
        self._restriction_detector = restriction_detector or RenderedAccessRestrictionDetector()
        self._acquisition_poll_seconds = acquisition_poll_seconds
        self._restriction_check_timeout_seconds = restriction_check_timeout_seconds
        self._open: dict[str, _OpenSession] = {}
        self._opening: dict[str, asyncio.Task[ManualOpenResult]] = {}
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._closing = False
        # At most one Manual Strategy acquisition window exists at any time.
        self._acquisition_lock = asyncio.Lock()
        self._acquisition_record: _OpenSession | None = None

    @property
    def open_count(self) -> int:
        return len(self._open)

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def acquisition_state(self) -> ManualAcquisitionState | None:
        """State of the open acquisition window, or ``None`` when there is none."""

        record = self._acquisition_record
        if record is None:
            return None
        if record.identity_adopted or not record.identity_pending:
            return ManualAcquisitionState.QUEUE_ID_ACQUIRED
        if record.access_restricted:
            return ManualAcquisitionState.ACCESS_RESTRICTED_BEFORE_QUEUE
        return ManualAcquisitionState.AWAITING_QUEUE_ID

    def stop_accepting(self) -> None:
        """Reject new opens immediately; already-open windows stay until close().

        The Manual Strategy acquisition window belongs to acquisition, which stops
        first during shutdown, so it is released now rather than waited on.
        """

        self._closing = True
        record = self._acquisition_record
        if record is not None:
            self._set_closed(record, ManualCloseReason.SHUTDOWN)

    async def recover_stale(self) -> int:
        return await self._repository.recover_stale_manual_ownership(
            now=datetime.now(UTC)
        )

    async def open(self, session_id: str) -> ManualOpenResult:
        async with self._lock:
            if self._closing:
                raise ManualOpenError("Manual browser is shutting down")
            if session_id in self._open:
                return ManualOpenResult(
                    ManualOpenStatus.ALREADY_OPEN,
                    "Session is already open in a browser",
                )
            task = self._opening.get(session_id)
            if task is None:
                task = asyncio.create_task(
                    self._open_new(session_id),
                    name=f"manual-open-{session_id}",
                )
                self._opening[session_id] = task
        try:
            return await task
        finally:
            async with self._lock:
                if self._opening.get(session_id) is task:
                    self._opening.pop(session_id, None)

    async def run_acquisition_window(self, session_id: str) -> ManualWindowResult:
        """Open one visible Manual Strategy acquisition window and wait for its end.

        The window opens at the run target for a reserved row without a Queue ID. A
        Queue ID that appears is persisted while the window stays open, and the
        session is then observed through the shared inspection path. Nothing here
        closes a healthy window: only the operator (browser or dashboard Close),
        browser loss, lost ownership, or shutdown ends the wait, which has no timer.
        """

        if self._acquisition_lock.locked():
            raise ManualOpenError("A manual acquisition window is already open")
        async with self._acquisition_lock:
            if self._closing:
                raise ManualOpenError("Manual browser is shutting down")
            await self._open_new(session_id, acquisition=True)
            record = self._acquisition_record
            if record is None:  # pragma: no cover - set by _open_new on success
                raise ManualOpenError("Unable to open session in browser")
            try:
                watcher = record.watcher
                if watcher is not None:
                    # Shielded: cancelling the waiting creator must not interrupt the
                    # final persistence and release, which the watcher still performs.
                    await asyncio.shield(watcher)
            except asyncio.CancelledError:
                self._set_closed(record, ManualCloseReason.SHUTDOWN)
                raise
            finally:
                self._acquisition_record = None
            return ManualWindowResult(
                session_id=session_id,
                close_reason=record.close_reason or ManualCloseReason.OPERATOR_CLOSED,
                identity_adopted=record.identity_adopted,
                access_restricted=record.access_restricted and record.identity_pending,
                duplicate_identity=record.adoption_blocked,
            )

    async def _open_new(self, session_id: str, *, acquisition: bool = False) -> ManualOpenResult:
        owner_id = f"manual-{uuid4()}"
        now = datetime.now(UTC)
        try:
            session = await self._repository.acquire_manual_ownership(
                session_id,
                owner_id=owner_id,
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                # The acquisition window has its own slot beside operator Open windows.
                capacity=self._capacity + (1 if acquisition else 0),
            )
        except (ManualSessionBusyError, ManualSessionCapacityError) as exc:
            raise ManualOpenError(str(exc)) from exc
        except SessionNotFoundError as exc:
            raise ManualOpenError("Session no longer exists") from exc

        opened: OpenedSessionRestore | None = None
        try:
            await self._ensure_started()
            if acquisition:
                opened = await self._restorer.restore_open(
                    session, keep_open_on_navigation_failure=True
                )
            else:
                opened = await self._restorer.restore_open(session)
            if opened.owned_context is None or opened.page is None:
                failure = opened.result.failure
                message = failure.value.replace("_", " ").title() if failure else "Restore failed"
                raise ManualOpenError(message)
            record = _OpenSession(
                session_id=session_id,
                owner_id=owner_id,
                owned_context=opened.owned_context,
                page=opened.page,
                closed=asyncio.Event(),
                identity_pending=opened.identity_pending,
                acquisition=acquisition,
            )
            if acquisition:
                # The window is closed only when its last page or its context closes;
                # navigation, redirects, reloads, and extra tabs never end it.
                self._track_page(record, opened.page)
                opened.owned_context.context.on(
                    "page", lambda page: self._track_page(record, page)
                )
            else:
                opened.page.on(
                    "close",
                    lambda _: self._set_closed(record, ManualCloseReason.OPERATOR_CLOSED),
                )
            opened.owned_context.context.on(
                "close", lambda _: self._set_closed(record, self._closed_reason(record))
            )
            async with self._lock:
                if self._closing:
                    raise ManualOpenError("Manual browser is shutting down")
                self._open[session_id] = record
                if acquisition:
                    self._acquisition_record = record
                record.watcher = asyncio.create_task(
                    self._watch(record),
                    name=f"manual-watch-{session_id}",
                )
            if record.identity_pending:
                return ManualOpenResult(
                    ManualOpenStatus.OPENED,
                    "Opened in browser (no Queue ID yet; it will be captured if one appears)",
                )
            return ManualOpenResult(ManualOpenStatus.OPENED, "Opened in browser")
        except BrowserCapacityError as exc:
            raise ManualOpenError("Browser capacity currently unavailable") from exc
        except ManualOpenError:
            raise
        except Exception as exc:
            raise ManualOpenError("Unable to open session in browser") from exc
        finally:
            async with self._lock:
                retained = session_id in self._open
            if not retained:
                if opened is not None and opened.owned_context is not None:
                    await opened.owned_context.close()
                await self._release(session_id, owner_id)

    @staticmethod
    def _set_closed(record: _OpenSession, reason: ManualCloseReason) -> None:
        """End ownership of a window; the first recorded reason wins."""

        if record.close_reason is None:
            record.close_reason = reason
        record.closed.set()

    @staticmethod
    def _closed_reason(record: _OpenSession) -> ManualCloseReason:
        """A close while the browser is still connected was the operator's."""

        try:
            browser = record.owned_context.context.browser
            connected = browser is None or browser.is_connected()
        except Exception:  # noqa: BLE001 - an unreadable browser counts as lost
            connected = False
        return ManualCloseReason.OPERATOR_CLOSED if connected else ManualCloseReason.BROWSER_LOST

    def _track_page(self, record: _OpenSession, page: Page) -> None:
        page.on("close", lambda closed_page: self._page_closed(record, closed_page))

    def _page_closed(self, record: _OpenSession, page: Page) -> None:
        remaining = [
            candidate
            for candidate in record.owned_context.context.pages
            if candidate is not page and not candidate.is_closed()
        ]
        if remaining:
            # Another tab of the same window is still open: keep observing it.
            if record.page is page:
                record.page = remaining[-1]
            return
        self._set_closed(record, self._closed_reason(record))

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if not self._browser_manager.started:
                await self._browser_manager.start()

    async def _browser_alive(self, record: _OpenSession) -> bool:
        """Check the owning headed browser without triggering a restart.

        Repairing here would relaunch a visible browser window after a crash even
        though no operator asked for one; the pool is restarted lazily by the next
        Open instead.
        """

        if record.owned_context.closed or record.page.is_closed():
            return False
        capacity = await self._browser_manager.capacity(repair=False)
        return any(
            process.index == record.owned_context.browser_id and process.connected
            for process in capacity.processes
        )

    async def _watch(self, record: _OpenSession) -> None:
        last_renewed = time.monotonic()
        # Stop before the lease can lapse: after that the scheduler may take the
        # session over, and two browsers must never drive one identity.
        renewal_deadline = max(
            self._heartbeat_seconds, self._lease_seconds - self._heartbeat_seconds
        )
        # An acquisition window is observed more often so a Queue ID is persisted
        # promptly; the wait itself is event-driven and has no overall deadline.
        tick = self._acquisition_poll_seconds if record.acquisition else self._heartbeat_seconds
        try:
            while not record.closed.is_set():
                try:
                    await asyncio.wait_for(record.closed.wait(), timeout=tick)
                except TimeoutError:
                    try:
                        alive = await self._browser_alive(record)
                    except Exception:  # noqa: BLE001 - treat an unreadable pool as lost
                        alive = False
                    if not alive:
                        self._set_closed(
                            record,
                            self._closed_reason(record)
                            if record.page.is_closed()
                            else ManualCloseReason.BROWSER_LOST,
                        )
                        continue
                    if (
                        record.acquisition
                        and time.monotonic() - last_renewed < self._heartbeat_seconds
                    ):
                        await self._observe_acquisition(record)
                        continue
                    try:
                        renewed = await self._repository.renew_manual_ownership(
                            record.session_id,
                            owner_id=record.owner_id,
                            lease_until=datetime.now(UTC)
                            + timedelta(seconds=self._lease_seconds),
                        )
                    except Exception as exc:  # noqa: BLE001 - transient database outage
                        log_event(
                            logger,
                            logging.WARNING,
                            "manual_lease_renewal_failed",
                            session_id=record.session_id,
                            error_type=type(exc).__name__,
                        )
                        if time.monotonic() - last_renewed >= renewal_deadline:
                            self._set_closed(record, ManualCloseReason.OWNERSHIP_LOST)
                        continue
                    if not renewed:
                        self._set_closed(record, ManualCloseReason.OWNERSHIP_LOST)
                    else:
                        last_renewed = time.monotonic()
                        if record.acquisition:
                            await self._observe_acquisition(record)
                        elif record.identity_pending and not record.adoption_blocked:
                            await self._try_adopt(record)
        finally:
            await self._finalize(record, inspect=True)

    async def _observe_acquisition(self, record: _OpenSession) -> None:
        """One observation of the open acquisition window; it never closes the window.

        Before a Queue ID: surface an access-restriction page, otherwise adopt any
        valid page transfer identity (no live-queue marker required) and inspect it
        at once so its lifecycle is persisted while the window stays open. After:
        re-inspect at the heartbeat cadence.
        """

        if record.closed.is_set():
            return
        if record.identity_pending:
            if record.adoption_blocked:
                return
            record.access_restricted = await self._detect_restriction(record)
            if record.access_restricted or record.closed.is_set():
                return
            await self._try_adopt(record)
            if record.identity_pending:
                return
        elif time.monotonic() - record.last_inspected < self._heartbeat_seconds:
            return
        record.last_inspected = time.monotonic()
        await self._inspect_live(record)

    async def _detect_restriction(self, record: _OpenSession) -> bool:
        try:
            return await await_bounded(
                self._restriction_detector.detect(record.page),
                timeout=self._restriction_check_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - an unreadable page is never "restricted"
            return False

    async def _inspect_live(self, record: _OpenSession) -> MonitoringOutcome | None:
        """Persist a verified observation of a still-open acquisition window.

        Uses the same restorer inspection and lifecycle evaluator as every other
        browser check, so a Queue ID with no usable state is persisted as PRE_QUEUE.
        Unverified observations are not written while the operator still has the
        window; the final close inspection keeps its existing semantics.
        """

        try:
            if record.page.is_closed() or record.owned_context.closed:
                return None
            session = await self._repository.get(record.session_id)
            if session is None or session.manual_owner_id != record.owner_id:
                return None
            result = await self._restorer.inspect_open(
                session,
                context=record.owned_context.context,
                page=record.page,
            )
            if not (is_verified_observation(result) or result.admitted or result.expired):
                return None
            outcome, _ = await self._monitor.apply_restore_result(session, result)
            return outcome
        except Exception as exc:  # noqa: BLE001 - retried on the next observation
            log_event(
                logger,
                logging.WARNING,
                "manual_acquisition_inspection_failed",
                session_id=record.session_id,
                error_type=type(exc).__name__,
            )
            return None

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            record = self._open.get(session_id)
        if record is None:
            return False
        self._set_closed(record, ManualCloseReason.DASHBOARD_CLOSE)
        watcher = record.watcher
        if watcher is not None and watcher is not asyncio.current_task():
            await watcher
        return True

    async def _finalize(self, record: _OpenSession, *, inspect: bool) -> None:
        async with self._lock:
            if self._open.get(record.session_id) is not record or record.finalizing:
                return
            # Stays counted until ownership is released, so ``open_count == 0`` means
            # no window and no manual lease remain.
            record.finalizing = True
        try:
            if not inspect or record.page.is_closed() or record.owned_context.closed:
                pass
            elif record.identity_pending:
                # Without an identity there is nothing to verify; a failed inspection
                # must not overwrite the row, so only a newly observed ID is saved.
                if not record.adoption_blocked:
                    await self._try_adopt(record)
            else:
                session = await self._repository.get(record.session_id)
                if session is not None and session.manual_owner_id == record.owner_id:
                    result = await self._restorer.inspect_open(
                        session,
                        context=record.owned_context.context,
                        page=record.page,
                    )
                    outcome, _ = await self._monitor.apply_restore_result(session, result)
                    if outcome.success:
                        # Manual Open policy: one exit-IP lookup at the final close
                        # inspection, never polling while the window stays open.
                        await self._monitor.observe_proxy_ip(session)
        except Exception as exc:  # noqa: BLE001 - preserve state and release ownership
            logger.warning(
                "Final manual browser inspection failed for %s: %s",
                record.session_id,
                type(exc).__name__,
            )
        finally:
            try:
                await record.owned_context.close()
            finally:
                try:
                    await self._release(record.session_id, record.owner_id)
                finally:
                    async with self._lock:
                        if self._open.get(record.session_id) is record:
                            self._open.pop(record.session_id, None)

    async def _try_adopt(self, record: _OpenSession) -> None:
        """Persist a Queue-it identity that appeared in an unidentified window.

        FAILED only re-enters the lifecycle through CREATING, so adoption follows
        the creation path: CREATING (which rejects a duplicate Queue ID), then
        PARKED and due, leaving the first verified inspection to set live status.
        """

        try:
            session = await self._repository.get(record.session_id)
            if session is None or session.manual_owner_id != record.owner_id:
                return
            progress: QueueProgress | None = None
            if session.queue_id is None:
                had_state = await asyncio.to_thread(session.state_path.exists)
                result = await self._restorer.adopt_open(
                    session,
                    context=record.owned_context.context,
                    page=record.page,
                    # A Manual Strategy window adopts any valid page identity; the
                    # identity-only lifecycle fallback then classifies it.
                    require_live_queue=not record.acquisition,
                )
                if result is None:
                    return
                progress = result.progress
                session.status = QueueStatus.CREATING
                try:
                    await self._repository.update(session)
                except QueueIdConflictError:
                    record.adoption_blocked = True
                    if result.state_refreshed and not had_state:
                        await self._restorer.discard_state(session)
                    log_event(
                        logger,
                        logging.WARNING,
                        "manual_identity_adoption_conflict",
                        session_id=record.session_id,
                    )
                    return
            if session.status is QueueStatus.CREATING:
                # Also resumes an adoption whose PARKED write failed last heartbeat.
                observed_at = datetime.now(UTC)
                session.status = QueueStatus.PARKED
                session.last_checked_at = observed_at
                session.last_progress_change_at = observed_at
                session.next_check_at = observed_at
                if progress is not None:
                    session.last_queue_update = progress.last_updated_at
                await self._repository.update(session, progress)
                log_event(
                    logger,
                    logging.INFO,
                    "manual_identity_adopted",
                    session_id=record.session_id,
                    queue_id=session.queue_id,
                )
                record.identity_adopted = True
            record.identity_pending = False
            record.access_restricted = False
        except Exception as exc:  # noqa: BLE001 - retried on the next heartbeat
            log_event(
                logger,
                logging.WARNING,
                "manual_identity_adoption_failed",
                session_id=record.session_id,
                error_type=type(exc).__name__,
            )

    async def _release(self, session_id: str, owner_id: str) -> None:
        try:
            await self._repository.release_manual_ownership(session_id, owner_id=owner_id)
        except Exception as exc:  # noqa: BLE001
            # The short manual lease expires, and restart clears it unconditionally.
            log_event(
                logger,
                logging.ERROR,
                "manual_ownership_release_failed",
                session_id=session_id,
                error_type=type(exc).__name__,
            )

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            opening = tuple(self._opening.values())
            records = tuple(self._open.values())
        for task in opening:
            task.cancel()
        if opening:
            await asyncio.gather(*opening, return_exceptions=True)
        for record in records:
            self._set_closed(record, ManualCloseReason.SHUTDOWN)
        watchers = tuple(record.watcher for record in records if record.watcher is not None)
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        if self._browser_manager.started:
            try:
                await self._browser_manager.shutdown()
            except Exception as exc:  # noqa: BLE001 - ownership is already released
                log_event(
                    logger,
                    logging.WARNING,
                    "manual_browser_shutdown_failed",
                    error_type=type(exc).__name__,
                )
