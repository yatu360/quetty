"""Bounded headed-Chrome ownership for operator-opened persisted sessions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from playwright.async_api import Page

from queue_load_test.browser import BrowserCapacityError, BrowserManager, OwnedBrowserContext
from queue_load_test.repository import (
    ManualSessionBusyError,
    ManualSessionCapacityError,
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

    async def recover_stale(self) -> int:
        return await self._repository.recover_stale_manual_ownership(
            now=datetime.now(UTC)
        )

    async def open(self, session_id: str) -> ManualOpenResult:
        async with self._lock:
            if self._closing:
                raise ManualOpenError("Manual Chrome is shutting down")
            if session_id in self._open:
                return ManualOpenResult(
                    ManualOpenStatus.ALREADY_OPEN,
                    "Session is already open in Chrome",
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
            )
            opened.page.on("close", lambda _: record.closed.set())
            opened.owned_context.context.on("close", lambda _: record.closed.set())
            async with self._lock:
                if self._closing:
                    raise ManualOpenError("Manual Chrome is shutting down")
                self._open[session_id] = record
                record.watcher = asyncio.create_task(
                    self._watch(record),
                    name=f"manual-watch-{session_id}",
                )
            return ManualOpenResult(ManualOpenStatus.OPENED, "Opened in Chrome")
        except BrowserCapacityError as exc:
            raise ManualOpenError("Browser capacity currently unavailable") from exc
        except ManualOpenError:
            raise
        except Exception as exc:
            raise ManualOpenError("Unable to open session in Chrome") from exc
        finally:
            async with self._lock:
                retained = session_id in self._open
            if not retained:
                if opened is not None and opened.owned_context is not None:
                    await opened.owned_context.close()
                await self._repository.release_manual_ownership(
                    session_id,
                    owner_id=owner_id,
                )

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if not self._browser_manager.started:
                await self._browser_manager.start()

    async def _watch(self, record: _OpenSession) -> None:
        try:
            while not record.closed.is_set():
                try:
                    await asyncio.wait_for(
                        record.closed.wait(),
                        timeout=self._heartbeat_seconds,
                    )
                except TimeoutError:
                    try:
                        await self._browser_manager.capacity()
                    except Exception:  # noqa: BLE001
                        record.closed.set()
                        continue
                    if record.owned_context.closed:
                        record.closed.set()
                        continue
                    renewed = await self._repository.renew_manual_ownership(
                        record.session_id,
                        owner_id=record.owner_id,
                        lease_until=datetime.now(UTC)
                        + timedelta(seconds=self._lease_seconds),
                    )
                    if not renewed:
                        record.closed.set()
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
            if inspect and not record.page.is_closed() and not record.owned_context.closed:
                session = await self._repository.get(record.session_id)
                if session is not None and session.manual_owner_id == record.owner_id:
                    result = await self._restorer.inspect_open(
                        session,
                        context=record.owned_context.context,
                        page=record.page,
                    )
                    await self._monitor.apply_restore_result(session, result)
        except Exception as exc:  # noqa: BLE001 - preserve state and release ownership
            logger.warning(
                "Final manual Chrome inspection failed for %s: %s",
                record.session_id,
                type(exc).__name__,
            )
        finally:
            try:
                await self._repository.release_manual_ownership(
                    record.session_id,
                    owner_id=record.owner_id,
                )
            finally:
                await record.owned_context.close()

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
            await self._browser_manager.shutdown()
