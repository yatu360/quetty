"""Signal-aware lifecycle coordination for Queue-it application resources."""

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from queue_load_test.browser import BrowserManager
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.repository import RecoverySummary, SessionRepository
from queue_load_test.scheduler import ParkedSessionScheduler

logger = logging.getLogger(__name__)


class CreationRunner(Protocol):
    async def run(self, stop_event: asyncio.Event | None = None) -> object: ...


class ApplicationRuntime:
    """Coordinate graceful stop without deleting persisted Queue-it journeys."""

    def __init__(
        self,
        *,
        browser_manager: BrowserManager,
        repository: SessionRepository,
        monitoring_scheduler: ParkedSessionScheduler,
        creation_runner: CreationRunner | None = None,
        shutdown_timeout_seconds: float = 30.0,
        observability: PrometheusMetrics | None = None,
        target_queue_ids: int | None = None,
        install_signal_handlers: bool = True,
        before_browser_shutdown: Sequence[tuple[str, Callable[[], Awaitable[object]]]] = (),
        additional_browser_managers: Sequence[BrowserManager] = (),
    ) -> None:
        """Create the runtime.

        ``install_signal_handlers=False`` is for hosts such as uvicorn that own
        SIGINT/SIGTERM themselves and call :meth:`request_shutdown` from their own
        shutdown path; replacing their handlers would stop the runtime while the
        host kept running. ``before_browser_shutdown`` steps run, in order, after
        producers and automatic monitoring stop but while Chrome is still usable,
        so dependent work can finish and persist its observations.
        ``additional_browser_managers`` (for example a headed creation pool) start
        after ``browser_manager`` and shut down just before it.
        """

        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._browser_manager = browser_manager
        self._repository = repository
        self._monitoring_scheduler = monitoring_scheduler
        self._creation_runner = creation_runner
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._observability = observability
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[object]] = []
        self._startup_recovery_summary: RecoverySummary | None = None
        self._startup_recovery_seconds: float | None = None
        self._target_queue_ids = target_queue_ids
        self._install_signals = install_signal_handlers
        self._before_browser_shutdown = tuple(before_browser_shutdown)
        self._additional_browser_managers = tuple(additional_browser_managers)

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    @property
    def startup_recovery_summary(self) -> RecoverySummary | None:
        return self._startup_recovery_summary

    @property
    def startup_recovery_seconds(self) -> float | None:
        return self._startup_recovery_seconds

    def request_shutdown(self) -> None:
        """Stop producers; workers are drained by ``run`` within the timeout."""

        self._monitoring_scheduler.stop_scheduling()
        self._stop_event.set()
        log_event(logger, logging.INFO, "shutdown_requested")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        installed_signals = self._install_signal_handlers(loop) if self._install_signals else ()
        try:
            recovery_started = time.perf_counter()
            self._startup_recovery_summary = await self._repository.recovery_summary(
                now=datetime.now(UTC)
            )
            self._startup_recovery_seconds = time.perf_counter() - recovery_started
            summary = self._startup_recovery_summary
            if self._observability is not None:
                self._observability.record_startup_recovery(
                    self._startup_recovery_seconds,
                    summary,
                    target=self._target_queue_ids,
                )
            log_event(
                logger,
                logging.INFO,
                "startup_recovery_summary",
                duration=self._startup_recovery_seconds,
                total_sessions=summary.total_persisted_sessions,
                valid_queue_ids=summary.valid_queue_ids,
                lost_queue_ids=summary.lost_queue_ids,
                leased_sessions=summary.leased_sessions,
                expired_leases=summary.expired_leases,
                sessions_due=summary.sessions_due,
                sessions_requiring_retry=summary.sessions_requiring_retry,
                terminal_sessions=summary.terminal_sessions,
            )
            await self._browser_manager.start()
            for manager in self._additional_browser_managers:
                await manager.start()
            log_event(logger, logging.INFO, "application_runtime_started")
            self._tasks.append(
                asyncio.create_task(
                    self._monitoring_scheduler.run(self._stop_event),
                    name="monitoring-scheduler",
                )
            )
            if self._creation_runner is not None:
                self._tasks.append(
                    asyncio.create_task(
                        self._creation_runner.run(self._stop_event),
                        name="session-creation",
                    )
                )
            await self._stop_event.wait()
        finally:
            self.request_shutdown()
            # Each step is isolated so a failing database cannot prevent Chrome
            # contexts from being closed, and vice versa.
            await self._shutdown_step("tasks", self._finish_tasks())
            await self._shutdown_step(
                "monitoring",
                self._monitoring_scheduler.shutdown(
                    timeout_seconds=self._shutdown_timeout_seconds
                ),
            )
            for operation, step in self._before_browser_shutdown:
                await self._shutdown_step(operation, step())
            for manager in self._additional_browser_managers:
                await self._shutdown_step("browser", manager.shutdown())
            await self._shutdown_step("browser", self._browser_manager.shutdown())
            await self._shutdown_step("repository", self._repository.close())
            log_event(logger, logging.INFO, "application_runtime_stopped")
            self._remove_signal_handlers(loop, installed_signals)

    async def _shutdown_step(self, operation: str, step: Awaitable[object]) -> None:
        try:
            await step
        except Exception as exc:  # noqa: BLE001 - continue releasing other resources
            log_event(
                logger,
                logging.ERROR,
                "shutdown_step_failed",
                operation=operation,
                error_type=type(exc).__name__,
            )

    async def _finish_tasks(self) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(
            self._tasks,
            timeout=self._shutdown_timeout_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    def _install_signal_handlers(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[signal.Signals, ...]:
        installed: list[signal.Signals] = []
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                logger.debug("Async signal handler unavailable for %s", signal_number.name)
            else:
                installed.append(signal_number)
        return tuple(installed)

    @staticmethod
    def _remove_signal_handlers(
        loop: asyncio.AbstractEventLoop,
        installed: tuple[signal.Signals, ...],
    ) -> None:
        for signal_number in installed:
            loop.remove_signal_handler(signal_number)
