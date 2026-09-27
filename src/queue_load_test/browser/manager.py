"""Capacity-aware management of browser processes and isolated contexts."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Self

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from queue_load_test.browser.backend import (
    BrowserBackend,
    BrowserBackendDiagnostics,
    ChromeBackend,
    create_browser_backend,
)
from queue_load_test.browser.backend import ContextStorageState as BackendContextStorageState
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics

logger = logging.getLogger(__name__)

type PlaywrightStarter = Callable[[], Awaitable[Playwright]]
type ContextStorageState = BackendContextStorageState


class BrowserManagerError(RuntimeError):
    """Base error for browser resource management."""


class BrowserCapacityError(BrowserManagerError):
    """Raised when no additional context can be allocated."""


class BrowserManagerNotStartedError(BrowserManagerError):
    """Raised when an operation requires a running manager."""


class BrowserContextCapacity:
    """One event-loop-local context budget shared by headless and headed pools."""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("maximum must be at least 1")
        self.maximum = maximum
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def available(self) -> int:
        return max(0, self.maximum - self._active)

    def acquire(self) -> bool:
        if self._active >= self.maximum:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1


@dataclass(frozen=True, slots=True)
class BrowserProcessCapacity:
    """Capacity details for one managed browser process."""

    index: int
    connected: bool
    active_contexts: int
    available_contexts: int


@dataclass(frozen=True, slots=True)
class BrowserCapacity:
    """Point-in-time resource accounting for the manager."""

    # Historical Python API retained for Phase 5 callers. This is not a Prometheus
    # metric name; new code should use browser_processes.
    chrome_processes: int
    connected_processes: int
    active_contexts: int
    available_contexts: int
    maximum_active_contexts: int
    processes: tuple[BrowserProcessCapacity, ...]

    @property
    def browser_processes(self) -> int:
        """Return the number of managed processes for the selected backend."""

        return self.chrome_processes


@dataclass(slots=True)
class _BrowserSlot:
    index: int
    browser: Browser
    contexts: set[OwnedBrowserContext] = field(default_factory=set)
    restart_task: asyncio.Task[bool] | None = None
    consecutive_navigation_timeouts: int = 0
    unresponsive: bool = False


class OwnedBrowserContext:
    """An idempotently closable context owned by a BrowserManager."""

    __slots__ = (
        "_acquisition_wait_seconds",
        "_closed",
        "_context",
        "_creation_duration_seconds",
        "_manager",
        "_shared_capacity_reserved",
        "_slot",
    )

    def __init__(
        self,
        manager: BrowserManager,
        slot: _BrowserSlot,
        context: BrowserContext,
        *,
        creation_duration_seconds: float = 0.0,
        acquisition_wait_seconds: float = 0.0,
        shared_capacity_reserved: bool = False,
    ) -> None:
        self._manager = manager
        self._slot = slot
        self._context = context
        self._closed = False
        self._creation_duration_seconds = creation_duration_seconds
        self._acquisition_wait_seconds = acquisition_wait_seconds
        self._shared_capacity_reserved = shared_capacity_reserved

    @property
    def context(self) -> BrowserContext:
        return self._context

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def browser_id(self) -> int:
        """Return the stable identifier of the browser slot owning this context."""

        return self._slot.index

    @property
    def creation_duration_seconds(self) -> float:
        """Time spent asking the browser to create this context."""

        return self._creation_duration_seconds

    @property
    def acquisition_wait_seconds(self) -> float:
        """Time spent waiting for BrowserManager allocation locks."""

        return self._acquisition_wait_seconds

    async def close(self) -> None:
        await self._manager.close_context(self)

    async def __aenter__(self) -> BrowserContext:
        return self._context

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._shared_capacity_reserved:
            self._shared_capacity_reserved = False
            self._manager._release_shared_capacity()


async def _start_playwright() -> Playwright:
    return await async_playwright().start()


class BrowserManager:
    """Own shared browser processes and isolated visitor contexts."""

    def __init__(
        self,
        *,
        chrome_process_count: int = 1,
        max_contexts_per_browser: int = 5,
        max_active_contexts: int = 5,
        headless: bool = True,
        playwright_starter: PlaywrightStarter = _start_playwright,
        observability: PrometheusMetrics | None = None,
        operation_timeout_seconds: float = 30.0,
        close_timeout_seconds: float = 5.0,
        shared_capacity: BrowserContextCapacity | None = None,
        backend: BrowserBackend | None = None,
    ) -> None:
        if operation_timeout_seconds <= 0 or close_timeout_seconds <= 0:
            raise ValueError("browser operation timeouts must be positive")
        if chrome_process_count < 1:
            raise ValueError("chrome_process_count must be at least 1")
        if max_contexts_per_browser < 1:
            raise ValueError("max_contexts_per_browser must be at least 1")
        if max_active_contexts < 1:
            raise ValueError("max_active_contexts must be at least 1")
        total_capacity = chrome_process_count * max_contexts_per_browser
        if max_active_contexts > total_capacity:
            raise ValueError("max_active_contexts cannot exceed total browser capacity")

        self._browser_process_count = chrome_process_count
        self._max_contexts_per_browser = max_contexts_per_browser
        self._max_active_contexts = max_active_contexts
        self._headless = headless
        self._playwright_starter = playwright_starter
        self._observability = observability
        self._playwright: Playwright | None = None
        self._slots: list[_BrowserSlot] = []
        self._lock = asyncio.Lock()
        self._running = False
        self._restart_count = 0
        self._restart_durations: list[float] = []
        # Playwright calls against a browser process killed mid-call can stay pending
        # forever. Every browser call made while holding the allocation lock, or
        # during restart/shutdown, is therefore bounded.
        self._operation_timeout_seconds = operation_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._shared_capacity = shared_capacity
        self._backend = backend or ChromeBackend()
        self._unresponsive_threshold: int | None = getattr(
            self._backend, "unresponsive_restart_threshold", None
        )
        self._unresponsive_restarts = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        playwright_starter: PlaywrightStarter = _start_playwright,
        observability: PrometheusMetrics | None = None,
        shared_capacity: BrowserContextCapacity | None = None,
    ) -> Self:
        return cls(
            chrome_process_count=settings.chrome_process_count,
            max_contexts_per_browser=settings.max_contexts_per_browser,
            max_active_contexts=settings.max_active_contexts,
            headless=settings.headless,
            playwright_starter=playwright_starter,
            observability=observability,
            shared_capacity=shared_capacity,
            backend=create_browser_backend(settings.browser_backend),
        )

    @property
    def started(self) -> bool:
        return self._running

    @property
    def restart_count(self) -> int:
        """Disconnected browser processes this manager has attempted to replace."""

        return self._restart_count

    @property
    def restart_durations(self) -> tuple[float, ...]:
        """Seconds spent on each completed slot replacement, in completion order."""

        return tuple(self._restart_durations)

    @property
    def unresponsive_restart_count(self) -> int:
        """Connected processes marked for restart after consecutive navigation timeouts."""

        return self._unresponsive_restarts

    def report_navigation(self, context: BrowserContext, *, responsive: bool) -> None:
        """Record whether a navigation in ``context`` completed or timed out.

        Only backends with an ``unresponsive_restart_threshold`` act on this. A slot
        reaching the threshold is treated like a disconnected one: the next
        allocation or capacity check replaces it through the ordinary bounded restart
        path. Synchronous, so it cannot be interrupted between check and mark.
        """

        threshold = self._unresponsive_threshold
        if threshold is None:
            return
        slot = next(
            (
                candidate
                for candidate in self._slots
                if any(owned.context is context for owned in candidate.contexts)
            ),
            None,
        )
        if slot is None or slot.restart_task is not None or slot.unresponsive:
            return
        if responsive:
            slot.consecutive_navigation_timeouts = 0
            return
        slot.consecutive_navigation_timeouts += 1
        if slot.consecutive_navigation_timeouts < threshold:
            return
        slot.unresponsive = True
        self._unresponsive_restarts += 1
        if self._observability is not None:
            self._observability.record_browser_unresponsive()
        log_event(
            logger,
            logging.WARNING,
            "browser_process_unresponsive",
            browser_id=slot.index,
            count=slot.consecutive_navigation_timeouts,
        )

    @property
    def backend_name(self) -> str:
        """Configured backend name for diagnostics and tests."""

        return self._backend.name.value

    @property
    def managed_process_count(self) -> int:
        """Processes currently owned by this manager."""

        return len(self._slots)

    @property
    def active_context_count(self) -> int:
        """Contexts currently owned by this manager."""

        return self._active_context_count()

    def backend_diagnostics(self) -> BrowserBackendDiagnostics:
        """Return package and live browser versions without mutating state."""

        browser = self._slots[0].browser if self._slots else None
        return self._backend.diagnostics(browser)

    async def start(self) -> None:
        """Start Playwright and the configured number of browser processes."""

        async with self._lock:
            if self._running:
                return
            playwright = await self._playwright_starter()
            self._playwright = playwright
            try:
                for index in range(self._browser_process_count):
                    browser = await self._launch_browser()
                    self._slots.append(_BrowserSlot(index=index, browser=browser))
            except BaseException:
                await self._close_started_resources()
                raise
            self._running = True
            self._update_capacity_metrics()
            try:
                diagnostics: BrowserBackendDiagnostics | None = self._backend.diagnostics(
                    self._slots[0].browser
                )
            except Exception:  # noqa: BLE001 - diagnostics must never fail startup
                diagnostics = None
            browser_version = diagnostics.browser_version if diagnostics else None
            if self._observability is not None:
                self._observability.set_browser_backend(
                    self._backend.name.value,
                    browser_build=browser_version or "unknown",
                )
            log_event(
                logger,
                logging.INFO,
                "browser_manager_started",
                browser_backend=self._backend.name.value,
                browser_version=browser_version,
                package_version=diagnostics.package_version if diagnostics else None,
            )

    async def _launch_browser(self) -> Browser:
        if self._playwright is None:
            raise BrowserManagerNotStartedError("Playwright is not running")
        return await asyncio.wait_for(
            self._backend.launch(self._playwright, headless=self._headless),
            timeout=self._operation_timeout_seconds,
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

        started = time.perf_counter()
        try:
            return await self._create_context(storage_state=storage_state)
        finally:
            if self._observability is not None:
                self._observability.record_context_acquisition_duration(
                    time.perf_counter() - started
                )

    async def _create_context(
        self,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> OwnedBrowserContext:
        """Implement allocation separately so all exits receive timing instrumentation."""

        acquisition_wait_seconds = 0.0
        while True:
            restart_tasks: tuple[asyncio.Task[bool], ...] = ()
            wait_for_restart = False
            lock_started = time.perf_counter()
            async with self._lock:
                acquisition_wait_seconds += time.perf_counter() - lock_started
                self._require_started()
                restart_tasks = self._schedule_failed_restarts_locked()
                if self._active_context_count() >= self._max_active_contexts:
                    raise BrowserCapacityError("Global browser context capacity is exhausted")
                if self._shared_capacity is not None and self._shared_capacity.available < 1:
                    raise BrowserCapacityError("Global browser context capacity is exhausted")

                candidates = [
                    slot
                    for slot in self._slots
                    if slot.restart_task is None
                    and not slot.unresponsive
                    and self._backend.is_connected(slot.browser)
                    and len(slot.contexts) < self._max_contexts_per_browser
                ]
                if candidates:
                    slot = min(
                        candidates,
                        key=lambda candidate: (len(candidate.contexts), candidate.index),
                    )
                    creation_started = time.perf_counter()
                    shared_capacity_reserved = False
                    try:
                        if self._shared_capacity is not None:
                            shared_capacity_reserved = self._shared_capacity.acquire()
                            if not shared_capacity_reserved:
                                raise BrowserCapacityError(
                                    "Global browser context capacity is exhausted"
                                )
                        context = await asyncio.wait_for(
                            self._new_context(slot.browser, storage_state),
                            timeout=self._operation_timeout_seconds,
                        )
                    except Exception:
                        if shared_capacity_reserved and self._shared_capacity is not None:
                            self._shared_capacity.release()
                        creation_duration = time.perf_counter() - creation_started
                        if self._observability is not None:
                            self._observability.record_context_creation_failure()
                            self._observability.record_context_creation_duration(
                                creation_duration
                            )
                        if self._backend.is_connected(slot.browser):
                            raise
                        restart_tasks = self._schedule_failed_restarts_locked()
                        wait_for_restart = not any(
                            candidate.restart_task is None
                            and self._backend.is_connected(candidate.browser)
                            and len(candidate.contexts) < self._max_contexts_per_browser
                            for candidate in self._slots
                        )
                    else:
                        creation_duration = time.perf_counter() - creation_started
                        if self._observability is not None:
                            self._observability.record_context_creation_duration(
                                creation_duration
                            )
                            self._observability.record_context_acquisition_wait(
                                acquisition_wait_seconds
                            )
                        owned_context = OwnedBrowserContext(
                            self,
                            slot,
                            context,
                            creation_duration_seconds=creation_duration,
                            acquisition_wait_seconds=acquisition_wait_seconds,
                            shared_capacity_reserved=shared_capacity_reserved,
                        )
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
            # the failed browser process to relaunch.
            if wait_for_restart:
                restart_results = await asyncio.gather(*restart_tasks)
                if not any(restart_results):
                    raise BrowserManagerError(
                        "No disconnected browser process could be restarted"
                    )

    async def _new_context(
        self,
        browser: Browser,
        storage_state: ContextStorageState | None,
    ) -> BrowserContext:
        return await self._backend.new_context(browser, storage_state=storage_state)

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

        # Release capacity synchronously. Waiting for the allocation lock here would
        # let a cancellation (for example a forced shutdown) arrive before the
        # context is discarded, permanently leaking its capacity. No await separates
        # these steps, so they are atomic with respect to other coroutines.
        if owned_context.closed:
            return
        owned_context._slot.contexts.discard(owned_context)
        owned_context._mark_closed()
        self._update_capacity_metrics()
        await self._bounded_close(
            self._backend.close_context(owned_context.context),
            owned_context._slot.index,
        )

    def _release_shared_capacity(self) -> None:
        if self._shared_capacity is not None:
            self._shared_capacity.release()

    async def _bounded_close(self, closing: Awaitable[None], browser_id: int) -> None:
        """Close a browser resource without letting cancellation or a dead process hang us."""

        close = asyncio.ensure_future(closing)
        try:
            await asyncio.wait_for(asyncio.shield(close), timeout=self._close_timeout_seconds)
        except asyncio.CancelledError:
            # Let the browser finish closing in the background.
            close.add_done_callback(lambda task: self._record_background_close(task, browser_id))
            raise
        except TimeoutError as exc:
            close.add_done_callback(lambda task: self._record_background_close(task, browser_id))
            self._record_close_failure(exc, browser_id)
        except Exception as exc:  # noqa: BLE001
            self._record_close_failure(exc, browser_id)

    def _record_background_close(self, task: asyncio.Future[None], browser_id: int) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._record_close_failure(exc, browser_id)

    def _record_close_failure(self, exc: BaseException, browser_id: int) -> None:
        if self._observability is not None:
            self._observability.record_browser_cleanup_failure()
        log_event(
            logger,
            logging.WARNING,
            "browser_context_close_failed",
            browser_id=browser_id,
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
            if self._backend.is_connected(slot.browser) and not slot.unresponsive:
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
        restart_started = time.perf_counter()
        self._restart_count += 1
        if self._observability is not None:
            self._observability.record_browser_crash()
        log_event(
            logger,
            logging.WARNING,
            "browser_process_restarting",
            browser_id=slot.index,
            error_type="BrowserDisconnected",
            lost_contexts=len(lost_contexts),
        )
        for owned_context in lost_contexts:
            await self._bounded_close(
                self._backend.close_context(owned_context.context),
                slot.index,
            )
        try:
            await asyncio.wait_for(
                self._backend.close_browser(old_browser),
                timeout=self._close_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            if self._observability is not None:
                self._observability.record_browser_cleanup_failure()
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
                    slot.unresponsive = False
                    slot.consecutive_navigation_timeouts = 0
                    installed = True
                self._update_capacity_metrics()

        if replacement is not None and not installed:
            try:
                await self._backend.close_browser(replacement)
            except Exception as exc:  # noqa: BLE001
                if self._observability is not None:
                    self._observability.record_browser_cleanup_failure()
                log_event(
                    logger,
                    logging.WARNING,
                    "unused_replacement_browser_cleanup_failed",
                    browser_id=slot.index,
                    error_type=type(exc).__name__,
                )
        restart_duration = time.perf_counter() - restart_started
        self._restart_durations.append(restart_duration)
        if self._observability is not None:
            self._observability.record_browser_restart(
                restart_duration,
                success=installed,
                lost_contexts=len(lost_contexts),
            )
        log_event(
            logger,
            logging.INFO if installed else logging.ERROR,
            "browser_process_restarted" if installed else "browser_process_not_replaced",
            browser_id=slot.index,
            duration=restart_duration,
            lost_contexts=len(lost_contexts),
        )
        return installed

    async def capacity(self, *, repair: bool = True) -> BrowserCapacity:
        """Return resource accounting, optionally repairing dead browser processes.

        Operator dashboard reads pass ``repair=False`` so viewing status cannot start
        browser recovery activity. Runtime and monitoring callers retain repair by
        default.
        """

        async with self._lock:
            self._require_started()
            restart_tasks = self._schedule_failed_restarts_locked() if repair else ()
        if restart_tasks:
            await asyncio.gather(*restart_tasks)

        async with self._lock:
            self._require_started()
            active = self._active_context_count()
            process_capacity = tuple(
                BrowserProcessCapacity(
                    index=slot.index,
                    connected=self._backend.is_connected(slot.browser),
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
            shared_available = (
                self._shared_capacity.available
                if self._shared_capacity is not None
                else self._max_active_contexts - active
            )
            return BrowserCapacity(
                chrome_processes=len(self._slots),
                connected_processes=sum(process.connected for process in process_capacity),
                active_contexts=active,
                available_contexts=min(
                    self._max_active_contexts - active,
                    per_browser_available,
                    shared_available,
                ),
                maximum_active_contexts=self._max_active_contexts,
                processes=process_capacity,
            )

    async def shutdown(self) -> None:
        """Close every context, browser process, and the Playwright controller."""

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
                await self._bounded_close(
                    self._backend.close_context(owned_context.context),
                    slot.index,
                )
        for slot in self._slots:
            try:
                await asyncio.wait_for(
                    self._backend.close_browser(slot.browser),
                    timeout=self._close_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                if self._observability is not None:
                    self._observability.record_browser_cleanup_failure()
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
                await asyncio.wait_for(
                    self._playwright.stop(),
                    timeout=self._close_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                if self._observability is not None:
                    self._observability.record_browser_cleanup_failure()
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
