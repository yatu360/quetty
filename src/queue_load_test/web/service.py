"""Small orchestration boundary between the web UI and existing runtime components."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import HttpUrl, TypeAdapter

from queue_load_test.browser import BrowserManager
from queue_load_test.browser.manager import BrowserManagerError
from queue_load_test.config import Settings
from queue_load_test.metrics import PrometheusMetrics
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


@dataclass(frozen=True, slots=True)
class RuntimeCapacity:
    active_contexts: int = 0
    maximum_active_contexts: int = 0
    chrome_processes: int = 0


class RunRuntime(Protocol):
    async def start_run(self, run: RunConfig) -> None: ...

    async def capacity(self) -> RuntimeCapacity: ...

    def error(self) -> str | None: ...

    async def close(self) -> None: ...


class ApplicationRunRuntime:
    """Assemble one background runtime from an immutable persisted run."""

    def __init__(self, *, settings: Settings, repository: SessionRepository) -> None:
        self._base_settings = settings
        self._repository = repository
        self._browser_manager: BrowserManager | None = None
        self._runtime: ApplicationRuntime | None = None
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
            browser_manager = BrowserManager.from_settings(settings, observability=metrics)
            target_url = str(validated_url)
            creator = QueueSessionCreator(
                browser_manager=browser_manager,
                repository=self._repository,
                state_store=state_store,
                staging_url=target_url,
                state_directory=settings.state_directory,
                mode=settings.session_mode,
                observability=metrics,
            )
            creation = SessionCreationController(
                repository=self._repository,
                handler=creator,
                target_queue_ids=run.requested_sessions,
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
            )
            self._browser_manager = browser_manager
            self._runtime = runtime
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
        return RuntimeCapacity(
            active_contexts=value.active_contexts,
            maximum_active_contexts=value.maximum_active_contexts,
            chrome_processes=value.chrome_processes,
        )

    def error(self) -> str | None:
        task = self._task
        if task is None or not task.done() or task.cancelled():
            return None
        exception = task.exception()
        return type(exception).__name__ if exception is not None else None

    async def close(self) -> None:
        if self._runtime is not None:
            self._runtime.request_shutdown()
        if self._task is not None:
            await self._task


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    target_url: str
    requested_sessions: int
    loaded_sessions: int
    remaining: int
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
        if runtime_error is not None:
            creation = "ERROR"
        elif recovery.valid_queue_ids >= run.requested_sessions:
            creation = "COMPLETE"
        else:
            creation = "RUNNING"
        return DashboardSummary(
            target_url=run.target_url,
            requested_sessions=run.requested_sessions,
            loaded_sessions=recovery.valid_queue_ids,
            remaining=max(0, run.requested_sessions - recovery.valid_queue_ids),
            creation=creation,
            monitoring="RUNNING",
            total_persisted_sessions=recovery.total_persisted_sessions,
            valid_queue_ids=recovery.valid_queue_ids,
            due_backlog=due.count,
            active_contexts=capacity.active_contexts,
            maximum_active_contexts=capacity.maximum_active_contexts,
            chrome_processes=capacity.chrome_processes,
        )
