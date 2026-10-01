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
from queue_load_test.repository import (
    ManualSessionBusyError,
    ManualSessionCapacityError,
    QueueIdConflictError,
    SessionNotFoundError,
    SessionRepository,
)
from queue_load_test.scheduler import QueueSessionMonitor
from queue_load_test.transfer import OpenedSessionRestore, QueueSessionRestorer

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
    ) -> None:
        self._repository = repository
        self._browser_manager = browser_manager
        self._restorer = restorer
        self._monitor = monitor
        self._capacity = capacity
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = max(1.0, lease_seconds / 3)
        self._open: dict[str, _OpenSession] = {}
        self._opening: dict[str, asyncio.Task[ManualOpenResult]] = {}
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._closing = False

    @property
    def open_count(self) -> int:
        return len(self._open)

    def stop_accepting(self) -> None:
        """Reject new opens immediately; already-open windows stay until close()."""

        self._closing = True

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

    async def _open_new(self, session_id: str) -> ManualOpenResult:
        owner_id = f"manual-{uuid4()}"
        now = datetime.now(UTC)
        try:
            session = await self._repository.acquire_manual_ownership(
                session_id,
                owner_id=owner_id,
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
                capacity=self._capacity,
            )
        except (ManualSessionBusyError, ManualSessionCapacityError) as exc:
            raise ManualOpenError(str(exc)) from exc
        except SessionNotFoundError as exc:
            raise ManualOpenError("Session no longer exists") from exc

        opened: OpenedSessionRestore | None = None
        try:
            await self._ensure_started()
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
            )
            opened.page.on("close", lambda _: record.closed.set())
            opened.owned_context.context.on("close", lambda _: record.closed.set())
            async with self._lock:
                if self._closing:
                    raise ManualOpenError("Manual browser is shutting down")
                self._open[session_id] = record
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
        try:
            while not record.closed.is_set():
                try:
                    await asyncio.wait_for(
                        record.closed.wait(),
                        timeout=self._heartbeat_seconds,
                    )
                except TimeoutError:
                    try:
                        alive = await self._browser_alive(record)
                    except Exception:  # noqa: BLE001 - treat an unreadable pool as lost
                        alive = False
                    if not alive:
                        record.closed.set()
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
                            record.closed.set()
                        continue
                    if not renewed:
                        record.closed.set()
                    else:
                        last_renewed = time.monotonic()
                        if record.identity_pending and not record.adoption_blocked:
                            await self._try_adopt(record)
        finally:
            await self._finalize(record, inspect=True)

    async def close_session(self, session_id: str) -> bool:
        async with self._lock:
            record = self._open.get(session_id)
        if record is None:
            return False
        record.closed.set()
        watcher = record.watcher
        if watcher is not None and watcher is not asyncio.current_task():
            await watcher
        return True

    async def _finalize(self, record: _OpenSession, *, inspect: bool) -> None:
        async with self._lock:
            if self._open.get(record.session_id) is not record:
                return
            self._open.pop(record.session_id, None)
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
                await self._release(record.session_id, record.owner_id)

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
            record.identity_pending = False
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
            record.closed.set()
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
