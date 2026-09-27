"""Small orchestration boundary between the web UI and existing runtime components."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import HttpUrl, TypeAdapter

from queue_load_test.browser import (
    BrowserContextCapacity,
    BrowserManager,
    create_browser_backend,
)
from queue_load_test.browser.manager import BrowserManagerError
from queue_load_test.config import Settings
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.metrics.logging import log_event
from queue_load_test.models import RunConfig
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SessionRepository
from queue_load_test.runtime import ApplicationRuntime
from queue_load_test.scheduler import (
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionCreator,
    QueueSessionMonitor,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer
from queue_load_test.web.actions import (
    OperatorAction,
    OperatorActionKind,
    OperatorActionManager,
)
from queue_load_test.web.manual import ManualChromeSessionManager, ManualOpenResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeCapacity:
    active_contexts: int = 0
    maximum_active_contexts: int = 0
    chrome_processes: int = 0


class RunRuntime(Protocol):
    async def start_run(self, run: RunConfig) -> None: ...

    async def capacity(self) -> RuntimeCapacity: ...

    def error(self) -> str | None: ...

    async def pause_monitoring(self) -> None: ...

    async def resume_monitoring(self) -> None: ...

    async def open_session(self, session_id: str) -> ManualOpenResult: ...

    async def close_session(self, session_id: str) -> bool: ...

    async def request_action(
        self,
        kind: OperatorActionKind,
        session_id: str | None = None,
        *,
        request_token: str | None = None,
    ) -> OperatorAction: ...

    def stop_accepting(self) -> None: ...

    def session_action(self, session_id: str) -> OperatorAction | None: ...

    def latest_add_action(self) -> OperatorAction | None: ...

    async def reset(self) -> None: ...

    async def close(self) -> None: ...


class ApplicationRunRuntime:
    """Assemble one background runtime from an immutable persisted run."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: SessionRepository,
        manual_headless: bool = False,
    ) -> None:
        """``manual_headless`` exists for local installed-Chrome tests only."""

        self._base_settings = settings
        self._manual_headless = manual_headless
        self._repository = repository
        self._browser_manager: BrowserManager | None = None
        self._runtime: ApplicationRuntime | None = None
        self._monitoring_scheduler: ParkedSessionScheduler | None = None
        self._manual_sessions: ManualChromeSessionManager | None = None
        self._shared_capacity: BrowserContextCapacity | None = None
        self._headed_browser_manager: BrowserManager | None = None
        self._operator_actions: OperatorActionManager | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start_run(self, run: RunConfig) -> None:
        async with self._lock:
            if self._task is not None:
                return
            validated_url = TypeAdapter(HttpUrl).validate_python(run.target_url)
            settings = self._base_settings.model_copy(
                update={
                    "staging_url": validated_url,
                    "target_queue_ids": run.requested_sessions,
                }
            )
            metrics = PrometheusMetrics()
            state_store = FileSystemStateStore(settings.state_directory)
            shared_capacity = BrowserContextCapacity(settings.max_active_contexts)
            browser_manager = BrowserManager.from_settings(
                settings,
                observability=metrics,
                shared_capacity=shared_capacity,
            )
            target_url = str(validated_url)
            # Acquisition (setup, Add, Replace) may use its own visible Chrome while
            # monitoring stays headless. The creator closes its context once the
            # Queue ID is persisted, so a session never moves between live browsers.
            creation_browser_manager = browser_manager
            if settings.creation_headless != settings.headless:
                creation_contexts = settings.creation_workers + settings.operator_workers
                creation_browser_manager = BrowserManager(
                    chrome_process_count=1,
                    max_contexts_per_browser=creation_contexts,
                    max_active_contexts=creation_contexts,
                    headless=settings.creation_headless,
                    observability=metrics,
                    shared_capacity=shared_capacity,
                    backend=create_browser_backend(settings.browser_backend),
                )
            creator = QueueSessionCreator(
                browser_manager=creation_browser_manager,
                repository=self._repository,
                state_store=state_store,
                staging_url=target_url,
                state_directory=settings.state_directory,
                mode=settings.session_mode,
                observability=metrics,
            )
            population_adjustment = (
                await self._repository.get_operator_population_adjustment()
            )
            creation = SessionCreationController(
                repository=self._repository,
                handler=creator,
                target_queue_ids=max(0, run.requested_sessions + population_adjustment),
                worker_count=settings.creation_workers,
                queue_capacity=settings.creation_queue_capacity,
                observability=metrics,
                identity_replacement_limit=settings.identity_replacement_limit,
            )
            restorer = QueueSessionRestorer(
                browser_manager=browser_manager,
                repository=self._repository,
                state_store=state_store,
                admission_detector=AdmissionDetector.from_urls(target_url),
                storage_navigation_url=target_url,
                admission_wait_timeout_ms=settings.admission_wait_seconds * 1_000,
                observability=metrics,
            )
            monitor = QueueSessionMonitor(
                repository=self._repository,
                restorer=restorer,
                polling_policy=PollingPolicy.from_settings(settings),
                retry_policy=MonitoringRetryPolicy.from_settings(settings),
                observability=metrics,
            )
            headed_manager = BrowserManager(
                chrome_process_count=1,
                max_contexts_per_browser=settings.max_manual_open_sessions,
                max_active_contexts=settings.max_manual_open_sessions,
                headless=self._manual_headless,
                observability=metrics,
                shared_capacity=shared_capacity,
                backend=create_browser_backend(settings.browser_backend),
            )
            headed_restorer = QueueSessionRestorer(
                browser_manager=headed_manager,
                repository=self._repository,
                state_store=state_store,
                admission_detector=AdmissionDetector.from_urls(target_url),
                storage_navigation_url=target_url,
                admission_wait_timeout_ms=settings.admission_wait_seconds * 1_000,
                observability=metrics,
            )
            manual_sessions = ManualChromeSessionManager(
                repository=self._repository,
                browser_manager=headed_manager,
                restorer=headed_restorer,
                monitor=monitor,
                capacity=settings.max_manual_open_sessions,
                lease_seconds=settings.manual_open_lease_seconds,
            )
            await manual_sessions.recover_stale()
            operator_actions = OperatorActionManager(
                repository=self._repository,
                creator=creator,
                monitor=monitor,
                state_store=state_store,
                target_adjustment=creation,
                worker_count=settings.operator_workers,
                queue_capacity=settings.operator_queue_capacity,
                lease_seconds=settings.operator_lease_seconds,
            )
            await operator_actions.start()
            scheduler = ParkedSessionScheduler.from_settings(
                settings,
                repository=self._repository,
                handler=monitor,
                observability=metrics,
            )
            runtime = ApplicationRuntime(
                browser_manager=browser_manager,
                repository=self._repository,
                monitoring_scheduler=scheduler,
                creation_runner=creation,
                shutdown_timeout_seconds=settings.shutdown_timeout_seconds,
                observability=metrics,
                target_queue_ids=run.requested_sessions,
                # uvicorn owns SIGINT/SIGTERM and runs the lifespan shutdown, which
                # calls close(); installing handlers here would stop the runtime
                # while the web server kept running.
                install_signal_handlers=False,
                # After producers and automatic monitoring stop, but while Chrome is
                # still usable: finish or cancel operator work, then persist and close
                # headed sessions and release their ownership.
                before_browser_shutdown=(
                    (
                        "operator_actions",
                        lambda: operator_actions.close(
                            timeout_seconds=settings.shutdown_timeout_seconds
                        ),
                    ),
                    ("manual_chrome", manual_sessions.close),
                ),
                additional_browser_managers=(
                    (creation_browser_manager,)
                    if creation_browser_manager is not browser_manager
                    else ()
                ),
            )
            self._browser_manager = browser_manager
            self._monitoring_scheduler = scheduler
            self._runtime = runtime
            self._manual_sessions = manual_sessions
            self._shared_capacity = shared_capacity
            self._headed_browser_manager = headed_manager
            self._operator_actions = operator_actions
            self._task = asyncio.create_task(runtime.run(), name=f"run-{run.run_id}")

    async def capacity(self) -> RuntimeCapacity:
        manager = self._browser_manager
        if manager is None:
            return RuntimeCapacity(
                maximum_active_contexts=self._base_settings.max_active_contexts
            )
        try:
            value = await manager.capacity(repair=False)
        except BrowserManagerError:
            return RuntimeCapacity(
                maximum_active_contexts=self._base_settings.max_active_contexts
            )
        shared = self._shared_capacity
        headed = self._headed_browser_manager
        return RuntimeCapacity(
            active_contexts=shared.active if shared is not None else value.active_contexts,
            maximum_active_contexts=value.maximum_active_contexts,
            chrome_processes=value.browser_processes + int(headed is not None and headed.started),
        )

    def error(self) -> str | None:
        task = self._task
        if task is None or not task.done() or task.cancelled():
            return None
        exception = task.exception()
        return type(exception).__name__ if exception is not None else None

    async def pause_monitoring(self) -> None:
        scheduler = self._monitoring_scheduler
        if scheduler is None:
            await self._repository.set_monitoring_paused(True)
            return
        await scheduler.pause_monitoring()

    async def resume_monitoring(self) -> None:
        scheduler = self._monitoring_scheduler
        if scheduler is None:
            await self._repository.set_monitoring_paused(False)
            return
        await scheduler.resume_monitoring()

    async def open_session(self, session_id: str) -> ManualOpenResult:
        manager = self._manual_sessions
        if manager is None:
            raise RuntimeError("Run runtime has not started")
        return await manager.open(session_id)

    async def close_session(self, session_id: str) -> bool:
        manager = self._manual_sessions
        if manager is None:
            return False
        return await manager.close_session(session_id)

    async def request_action(
        self,
        kind: OperatorActionKind,
        session_id: str | None = None,
        *,
        request_token: str | None = None,
    ) -> OperatorAction:
        manager = self._operator_actions
        if manager is None:
            raise RuntimeError("Run runtime has not started")
        return await manager.request(kind, session_id, request_token=request_token)

    def stop_accepting(self) -> None:
        if self._operator_actions is not None:
            self._operator_actions.stop_accepting()
        if self._manual_sessions is not None:
            self._manual_sessions.stop_accepting()

    def session_action(self, session_id: str) -> OperatorAction | None:
        manager = self._operator_actions
        return manager.for_session(session_id) if manager is not None else None

    def latest_add_action(self) -> OperatorAction | None:
        manager = self._operator_actions
        return manager.latest_add() if manager is not None else None

    async def close(self) -> None:
        """Shut down in dependency order without deleting or replacing identities.

        1. reject new operator/headed requests; 2. stop creation and automatic
        claims; 3. drain in-flight automatic checks and release queued leases;
        4. finish or cancel operator work; 5. persist and close headed sessions and
        release their ownership; 6. close Chrome and the repository. Steps 3-6 run
        inside ``ApplicationRuntime.run`` so each is isolated from the others'
        failures.
        """

        self.stop_accepting()
        if self._runtime is not None:
            self._runtime.request_shutdown()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
        # Idempotent fallbacks for a runtime that never reached its shutdown hooks.
        if self._operator_actions is not None:
            await self._operator_actions.close(
                timeout_seconds=self._base_settings.shutdown_timeout_seconds
            )
        if self._manual_sessions is not None:
            await self._manual_sessions.close()

    async def reset(self) -> None:
        """Stop everything, then delete the run, all sessions, and saved browser state.

        The ordered ``close`` runs first so no worker or headed window still owns a
        row being deleted. If the database wipe fails the run is still persisted,
        so the runtime is restarted rather than left stopped.
        """

        await self.close()
        async with self._lock:
            self._task = None
            self._runtime = None
            self._browser_manager = None
            self._monitoring_scheduler = None
            self._manual_sessions = None
            self._shared_capacity = None
            self._headed_browser_manager = None
            self._operator_actions = None
        try:
            await self._repository.reset_all()
        except Exception:
            active = await self._repository.get_active_run()
            if active is not None:
                await self.start_run(active)
            raise
        removed = await FileSystemStateStore(self._base_settings.state_directory).clear()
        log_event(logger, logging.INFO, "run_reset_completed", count=removed)


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    target_url: str
    requested_sessions: int
    valid_managed_sessions: int
    remaining_to_initial_target: int
    creation: str
    monitoring: str
    total_persisted_sessions: int
    valid_queue_ids: int
    due_backlog: int
    active_contexts: int
    maximum_active_contexts: int
    chrome_processes: int


class DashboardService:
    def __init__(self, repository: SessionRepository, runtime: RunRuntime) -> None:
        self._repository = repository
        self._runtime = runtime

    async def summary(self, run: RunConfig) -> DashboardSummary:
        recovery = await self._repository.recovery_summary(now=datetime.now(UTC))
        due = await self._repository.due_session_summary(now=datetime.now(UTC))
        capacity = await self._runtime.capacity()
        runtime_error = self._runtime.error()
        monitoring_paused = await self._repository.is_monitoring_paused()
        population_adjustment = (
            await self._repository.get_operator_population_adjustment()
        )
        effective_target = max(0, run.requested_sessions + population_adjustment)
        if runtime_error is not None:
            creation = "ERROR"
        elif recovery.valid_queue_ids >= effective_target:
            creation = "COMPLETE"
        else:
            creation = "RUNNING"
        return DashboardSummary(
            target_url=run.target_url,
            requested_sessions=run.requested_sessions,
            valid_managed_sessions=recovery.valid_queue_ids,
            remaining_to_initial_target=max(
                0, run.requested_sessions - recovery.valid_queue_ids
            ),
            creation=creation,
            monitoring="PAUSED" if monitoring_paused else "RUNNING",
            total_persisted_sessions=recovery.total_persisted_sessions,
            valid_queue_ids=recovery.valid_queue_ids,
            due_backlog=due.count,
            active_contexts=capacity.active_contexts,
            maximum_active_contexts=capacity.maximum_active_contexts,
            chrome_processes=capacity.chrome_processes,
        )
