"""Capacity-aware management of Chrome processes and isolated contexts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from playwright.async_api import Browser, BrowserContext, Playwright, StorageState, async_playwright

from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics

logger = logging.getLogger(__name__)

type PlaywrightStarter = Callable[[], Awaitable[Playwright]]
type ContextStorageState = str | Path | StorageState


class BrowserManagerError(RuntimeError):
    """Base error for browser resource management."""


class BrowserCapacityError(BrowserManagerError):
    """Raised when no additional context can be allocated."""


class BrowserManagerNotStartedError(BrowserManagerError):
    """Raised when an operation requires a running manager."""


@dataclass(frozen=True, slots=True)
class BrowserProcessCapacity:
    """Capacity details for one managed Chrome process."""

    index: int
    connected: bool
    active_contexts: int
    available_contexts: int


@dataclass(frozen=True, slots=True)
class BrowserCapacity:
    """Point-in-time resource accounting for the manager."""

    chrome_processes: int
    connected_processes: int
    active_contexts: int
    available_contexts: int
    maximum_active_contexts: int
    processes: tuple[BrowserProcessCapacity, ...]


@dataclass(slots=True)
class _BrowserSlot:
    index: int
    browser: Browser
    contexts: set[OwnedBrowserContext] = field(default_factory=set)
    restart_task: asyncio.Task[bool] | None = None


class OwnedBrowserContext:
    """An idempotently closable context owned by a BrowserManager."""

    __slots__ = ("_closed", "_context", "_manager", "_slot")

    def __init__(
        self,
        manager: BrowserManager,
        slot: _BrowserSlot,
        context: BrowserContext,
    ) -> None:
        self._manager = manager
        self._slot = slot
        self._context = context
        self._closed = False

    @property
    def context(self) -> BrowserContext:
        return self._context

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def browser_id(self) -> int:
        """Return the stable identifier of the Chrome slot owning this context."""

        return self._slot.index

    async def close(self) -> None:
        await self._manager.close_context(self)

    async def __aenter__(self) -> BrowserContext:
        return self._context

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _mark_closed(self) -> None:
        self._closed = True


async def _start_playwright() -> Playwright:
    return await async_playwright().start()


class BrowserManager:
    """Own shared Chrome processes and isolated visitor contexts."""

    def __init__(
        self,
        *,
        chrome_process_count: int = 1,
        max_contexts_per_browser: int = 5,
        max_active_contexts: int = 5,
        headless: bool = True,
        playwright_starter: PlaywrightStarter = _start_playwright,
        observability: PrometheusMetrics | None = None,
    ) -> None:
        if chrome_process_count < 1:
            raise ValueError("chrome_process_count must be at least 1")
        if max_contexts_per_browser < 1:
            raise ValueError("max_contexts_per_browser must be at least 1")
        if max_active_contexts < 1:
            raise ValueError("max_active_contexts must be at least 1")
        total_capacity = chrome_process_count * max_contexts_per_browser
        if max_active_contexts > total_capacity:
            raise ValueError("max_active_contexts cannot exceed total browser capacity")

        self._chrome_process_count = chrome_process_count
        self._max_contexts_per_browser = max_contexts_per_browser
        self._max_active_contexts = max_active_contexts
        self._headless = headless
        self._playwright_starter = playwright_starter
        self._observability = observability
        self._playwright: Playwright | None = None
        self._slots: list[_BrowserSlot] = []
        self._lock = asyncio.Lock()
        self._running = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        playwright_starter: PlaywrightStarter = _start_playwright,
        observability: PrometheusMetrics | None = None,
    ) -> Self:
        return cls(
            chrome_process_count=settings.chrome_process_count,
            max_contexts_per_browser=settings.max_contexts_per_browser,
            max_active_contexts=settings.max_active_contexts,
            headless=settings.headless,
            playwright_starter=playwright_starter,
            observability=observability,
        )

    @property
    def started(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start Playwright and the configured number of Google Chrome processes."""

        async with self._lock:
            if self._running:
                return
            playwright = await self._playwright_starter()
            self._playwright = playwright
            try:
                for index in range(self._chrome_process_count):
                    browser = await self._launch_browser()
                    self._slots.append(_BrowserSlot(index=index, browser=browser))
            except BaseException:
                await self._close_started_resources()
                raise
            self._running = True
            self._update_capacity_metrics()
            log_event(logger, logging.INFO, "browser_manager_started")

    async def _launch_browser(self) -> Browser:
        if self._playwright is None:
            raise BrowserManagerNotStartedError("Playwright is not running")
        return await self._playwright.chromium.launch(
            channel="chrome",
            headless=self._headless,
        )

    def _active_context_count(self) -> int:
        return sum(len(slot.contexts) for slot in self._slots)

    def _require_started(self) -> None:
        if not self._running:
            raise BrowserManagerNotStartedError("BrowserManager has not been started")

    async def create_context(
        self,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> OwnedBrowserContext:
        """Allocate one isolated visitor context with explicit ownership."""

        while True:
            restart_tasks: tuple[asyncio.Task[bool], ...] = ()
            wait_for_restart = False
            async with self._lock:
                self._require_started()
                restart_tasks = self._schedule_failed_restarts_locked()
                if self._active_context_count() >= self._max_active_contexts:
                    raise BrowserCapacityError("Global browser context capacity is exhausted")

                candidates = [
                    slot
                    for slot in self._slots
                    if slot.restart_task is None
                    and slot.browser.is_connected()
                    and len(slot.contexts) < self._max_contexts_per_browser
                ]
                if candidates:
                    slot = min(
                        candidates,
                        key=lambda candidate: (len(candidate.contexts), candidate.index),
                    )
                    try:
                        context = await self._new_context(slot.browser, storage_state)
                    except Exception:
                        if slot.browser.is_connected():
                            raise
                        restart_tasks = self._schedule_failed_restarts_locked()
                        wait_for_restart = not any(
                            candidate.restart_task is None
                            and candidate.browser.is_connected()
                            and len(candidate.contexts) < self._max_contexts_per_browser
                            for candidate in self._slots
                        )
                    else:
                        owned_context = OwnedBrowserContext(self, slot, context)
                        slot.contexts.add(owned_context)
                        self._update_capacity_metrics()
                        log_event(
                            logger,
                            logging.INFO,
                            "browser_context_created",
                            browser_id=slot.index,
                        )
                        return owned_context
                elif not restart_tasks:
                    raise BrowserCapacityError("Per-browser context capacity is exhausted")
                else:
                    wait_for_restart = True

            # No healthy slot was available, or the chosen slot disconnected while
            # creating its context. Wait only when recovery is required to proceed.
            # When another slot is healthy the loop selects it without waiting for
            # the failed Chrome process to relaunch.
            if wait_for_restart:
                restart_results = await asyncio.gather(*restart_tasks)
                if not any(restart_results):
                    raise BrowserManagerError(
                        "No disconnected browser process could be restarted"
                    )

    @staticmethod
    async def _new_context(
        browser: Browser,
        storage_state: ContextStorageState | None,
    ) -> BrowserContext:
        if storage_state is None:
            return await browser.new_context()
        return await browser.new_context(storage_state=storage_state)

    @asynccontextmanager
    async def context(
        self,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> AsyncIterator[BrowserContext]:
        """Yield an isolated context and always release it on scope exit."""

        owned_context = await self.create_context(storage_state=storage_state)
        try:
            yield owned_context.context
        finally:
            await owned_context.close()

    async def close_context(self, owned_context: OwnedBrowserContext) -> None:
        """Close and release a context; repeated calls are safe."""

        async with self._lock:
            if owned_context.closed:
                return
            owned_context._slot.contexts.discard(owned_context)
            owned_context._mark_closed()
            self._update_capacity_metrics()
        try:
            await owned_context.context.close()
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger,
                logging.WARNING,
                "browser_context_close_failed",
                browser_id=owned_context._slot.index,
                error_type=type(exc).__name__,
            )

    async def restart_failed_browsers(self) -> int:
        """Detect disconnected processes and replace them."""

        async with self._lock:
            self._require_started()
            restart_tasks = self._schedule_failed_restarts_locked()
        if not restart_tasks:
            return 0
        return sum(await asyncio.gather(*restart_tasks))

    def _schedule_failed_restarts_locked(self) -> tuple[asyncio.Task[bool], ...]:
        restart_tasks: list[asyncio.Task[bool]] = []
        for slot in self._slots:
            if slot.browser.is_connected():
                continue
            if slot.restart_task is None:
                lost_contexts = tuple(slot.contexts)
                slot.contexts.clear()
                for owned_context in lost_contexts:
                    owned_context._mark_closed()
                slot.restart_task = asyncio.create_task(
                    self._restart_slot(slot, slot.browser, lost_contexts),
                    name=f"browser-restart-{slot.index}",
                )
                self._update_capacity_metrics()
            restart_tasks.append(slot.restart_task)
        return tuple(restart_tasks)

    async def _restart_slot(
        self,
        slot: _BrowserSlot,
        old_browser: Browser,
        lost_contexts: tuple[OwnedBrowserContext, ...],
    ) -> bool:
        if self._observability is not None:
            self._observability.record_browser_crash()
        log_event(
            logger,
            logging.WARNING,
            "browser_process_restarting",
            browser_id=slot.index,
            error_type="BrowserDisconnected",
        )
        for owned_context in lost_contexts:
            try:
                await owned_context.context.close()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    logging.WARNING,
                    "lost_browser_context_cleanup_failed",
                    browser_id=slot.index,
                    error_type=type(exc).__name__,
                )
        try:
            await old_browser.close()
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger,
                logging.WARNING,
                "failed_browser_process_cleanup_failed",
                browser_id=slot.index,
                error_type=type(exc).__name__,
            )
        replacement: Browser | None = None
        try:
            replacement = await self._launch_browser()
        except Exception as exc:  # noqa: BLE001
            log_event(
                logger,
                logging.ERROR,
                "browser_process_restart_failed",
                browser_id=slot.index,
                error_type=type(exc).__name__,
            )

        installed = False
        current_task = asyncio.current_task()
        async with self._lock:
            if slot.restart_task is current_task:
                slot.restart_task = None
                slot_is_managed = any(candidate is slot for candidate in self._slots)
                if replacement is not None and self._running and slot_is_managed:
                    slot.browser = replacement
                    installed = True
                self._update_capacity_metrics()

        if replacement is not None and not installed:
            try:
                await replacement.close()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    logging.WARNING,
                    "unused_replacement_browser_cleanup_failed",
                    browser_id=slot.index,
                    error_type=type(exc).__name__,
                )
        return installed

    async def capacity(self) -> BrowserCapacity:
        """Return current active and available capacity, repairing dead processes first."""

        async with self._lock:
            self._require_started()
            restart_tasks = self._schedule_failed_restarts_locked()
        if restart_tasks:
            await asyncio.gather(*restart_tasks)

        async with self._lock:
            self._require_started()
            active = self._active_context_count()
            process_capacity = tuple(
                BrowserProcessCapacity(
                    index=slot.index,
                    connected=slot.browser.is_connected(),
                    active_contexts=len(slot.contexts),
                    available_contexts=max(
                        0,
                        self._max_contexts_per_browser - len(slot.contexts),
                    ),
                )
                for slot in self._slots
            )
            per_browser_available = sum(
                process.available_contexts for process in process_capacity if process.connected
            )
            return BrowserCapacity(
                chrome_processes=len(self._slots),
                connected_processes=sum(process.connected for process in process_capacity),
                active_contexts=active,
                available_contexts=min(
                    self._max_active_contexts - active,
                    per_browser_available,
                ),
                maximum_active_contexts=self._max_active_contexts,
                processes=process_capacity,
            )

    async def shutdown(self) -> None:
        """Close every context, Chrome process, and the Playwright controller."""

        async with self._lock:
            self._running = False
            restart_tasks = tuple(
                slot.restart_task for slot in self._slots if slot.restart_task is not None
            )
        if restart_tasks:
            await asyncio.gather(*restart_tasks)

        async with self._lock:
            await self._close_started_resources()
            self._update_capacity_metrics()
            log_event(logger, logging.INFO, "browser_manager_stopped")

    async def _close_started_resources(self) -> None:
        for slot in self._slots:
            contexts = tuple(slot.contexts)
            slot.contexts.clear()
            for owned_context in contexts:
                owned_context._mark_closed()
                try:
                    await owned_context.context.close()
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        logger,
                        logging.WARNING,
                        "browser_context_shutdown_failed",
                        browser_id=slot.index,
                        error_type=type(exc).__name__,
                    )
        for slot in self._slots:
            try:
                await slot.browser.close()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    logging.WARNING,
                    "browser_shutdown_failed",
                    browser_id=slot.index,
                    error_type=type(exc).__name__,
                )
        self._slots.clear()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    logging.WARNING,
                    "playwright_shutdown_failed",
                    error_type=type(exc).__name__,
                )
            self._playwright = None

    def _update_capacity_metrics(self) -> None:
        if self._observability is not None:
            self._observability.set_browser_capacity(
                active_contexts=self._active_context_count(),
                processes=len(self._slots),
            )

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.shutdown()
