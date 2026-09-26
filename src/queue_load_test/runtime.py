"""Signal-aware lifecycle coordination for Queue-it application resources."""

import asyncio
import logging
import signal
from typing import Protocol

from queue_load_test.browser import BrowserManager
from queue_load_test.repository import SessionRepository
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
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._browser_manager = browser_manager
        self._repository = repository
        self._monitoring_scheduler = monitoring_scheduler
        self._creation_runner = creation_runner
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[object]] = []

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    def request_shutdown(self) -> None:
        """Stop producers; workers are drained by ``run`` within the timeout."""

        self._monitoring_scheduler.stop_scheduling()
        self._stop_event.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        installed_signals = self._install_signal_handlers(loop)
        try:
            await self._browser_manager.start()
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
            await self._finish_tasks()
            await self._monitoring_scheduler.shutdown(
                timeout_seconds=self._shutdown_timeout_seconds
            )
            await self._browser_manager.shutdown()
            await self._repository.close()
            self._remove_signal_handlers(loop, installed_signals)

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
