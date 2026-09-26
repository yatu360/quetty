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
    ) -> Self:
        return cls(
            chrome_process_count=settings.chrome_process_count,
            max_contexts_per_browser=settings.max_contexts_per_browser,
            max_active_contexts=settings.max_active_contexts,
            headless=settings.headless,
            playwright_starter=playwright_starter,
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

        async with self._lock:
            self._require_started()
            await self._restart_failed_browsers_locked()
            if self._active_context_count() >= self._max_active_contexts:
                raise BrowserCapacityError("Global browser context capacity is exhausted")

            candidates = [
                slot
                for slot in self._slots
                if slot.browser.is_connected()
                and len(slot.contexts) < self._max_contexts_per_browser
            ]
            if not candidates:
                raise BrowserCapacityError("Per-browser context capacity is exhausted")
            slot = min(candidates, key=lambda candidate: (len(candidate.contexts), candidate.index))

            try:
                context = await self._new_context(slot.browser, storage_state)
            except Exception:
                if slot.browser.is_connected():
                    raise
                await self._restart_slot(slot)
                context = await self._new_context(slot.browser, storage_state)

            owned_context = OwnedBrowserContext(self, slot, context)
            slot.contexts.add(owned_context)
            return owned_context

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
            try:
                await owned_context.context.close()
            except Exception:
                logger.warning("Browser context close failed", exc_info=True)

    async def restart_failed_browsers(self) -> int:
        """Detect disconnected processes and replace them."""

        async with self._lock:
            self._require_started()
            return await self._restart_failed_browsers_locked()

    async def _restart_failed_browsers_locked(self) -> int:
        restarted = 0
        for slot in self._slots:
            if not slot.browser.is_connected():
                await self._restart_slot(slot)
                restarted += 1
        return restarted

    async def _restart_slot(self, slot: _BrowserSlot) -> None:
        old_browser = slot.browser
        lost_contexts = tuple(slot.contexts)
        slot.contexts.clear()
        for owned_context in lost_contexts:
            owned_context._mark_closed()
            try:
                await owned_context.context.close()
            except Exception:
                logger.warning("Lost browser context cleanup failed", exc_info=True)
        try:
            await old_browser.close()
        except Exception:
            logger.warning("Failed Chrome process cleanup failed", exc_info=True)
        slot.browser = await self._launch_browser()

    async def capacity(self) -> BrowserCapacity:
        """Return current active and available capacity, repairing dead processes first."""

        async with self._lock:
            self._require_started()
            await self._restart_failed_browsers_locked()
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
            await self._close_started_resources()
            self._running = False

    async def _close_started_resources(self) -> None:
        for slot in self._slots:
            contexts = tuple(slot.contexts)
            slot.contexts.clear()
            for owned_context in contexts:
                owned_context._mark_closed()
                try:
                    await owned_context.context.close()
                except Exception:
                    logger.warning("Browser context shutdown failed", exc_info=True)
        for slot in self._slots:
            try:
                await slot.browser.close()
            except Exception:
                logger.warning("Chrome shutdown failed", exc_info=True)
        self._slots.clear()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.warning("Playwright shutdown failed", exc_info=True)
            self._playwright = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.shutdown()
