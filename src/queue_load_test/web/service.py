"""Small orchestration boundary between the web UI and existing runtime components."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import HttpUrl, TypeAdapter

from queue_load_test.browser import (
    CAMOUFOX_BROWSER_VERSION,
    BrowserContextCapacity,
    BrowserManager,
    create_browser_backend,
    manual_pool_topology,
)
from queue_load_test.browser.manager import BrowserManagerError
from queue_load_test.config import Settings
from queue_load_test.direct_monitor import (
    DirectMonitoringHandler,
    DirectMonitoringMetrics,
    DirectMonitorStateStore,
    DirectStatusChecker,
    DiscoveryRecipeHarvester,
    accepted_evidence_scopes,
)
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.metrics.logging import log_event
from queue_load_test.models import (
    BrowserBackendName,
    DirectCapability,
    MonitoringStrategy,
    ProxyProvider,
    RunConfig,
)
from queue_load_test.observation_equivalence import (
    DirectResponseParser,
    DirectResponseSchema,
    DirectSchemaError,
    load_direct_response_schema,
)
from queue_load_test.proxy import ProxyIpObserver, SessionProxyResolver
from queue_load_test.proxy.ip_tracker import ProxyIpStore, ProxyIpTracker
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import DirectMonitorMetadataRepository, SessionRepository
from queue_load_test.runtime import ApplicationRuntime
from queue_load_test.scheduler import (
    MonitoringHandler,
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionCreator,
    QueueSessionMonitor,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.status_discovery import StatusDiscoveryFactory
from queue_load_test.transfer import QueueSessionRestorer
from queue_load_test.web.actions import (
    OperatorAction,
    OperatorActionKind,
    OperatorActionManager,
)
from queue_load_test.web.manual import ManualChromeSessionManager, ManualOpenResult

logger = logging.getLogger(__name__)


def _automatic_monitor_for_strategy(
    strategy: MonitoringStrategy,
    *,
    browser_monitor: QueueSessionMonitor,
    direct_handler: MonitoringHandler | None = None,
) -> MonitoringHandler:
    """Select the run's monitoring handler from its immutable persisted strategy.

    Headed Window Strategy uses the browser monitor exactly as before. Direct
    Monitoring Strategy uses the direct handler, whose browser fallback is that
    same browser monitor; a Direct run is never silently reassembled as Headed.
    """

    if strategy is MonitoringStrategy.HEADED_WINDOW:
        return browser_monitor
    if strategy is MonitoringStrategy.DIRECT:
        if direct_handler is None:
            raise ValueError("Direct Monitoring Strategy requires its direct handler")
        return direct_handler
    raise ValueError(f"Unsupported monitoring strategy: {strategy!r}")


def _direct_response_schema(settings: Settings, target_url: str) -> DirectResponseSchema | None:
    """Load the reviewed response schema, accepting only evidence valid for this target."""

    path = settings.direct_monitor_schema_path
    if path is None:
        log_event(
            logger,
            logging.WARNING,
            "direct_monitor_schema_unavailable",
            operation="not_configured",
        )
        return None
    try:
        schema = load_direct_response_schema(path, require_authorized_staging=False)
    except DirectSchemaError:
        log_event(
            logger, logging.WARNING, "direct_monitor_schema_unavailable", operation="invalid"
        )
        return None
    if schema.source_scope not in accepted_evidence_scopes(target_url):
        log_event(
            logger,
            logging.WARNING,
            "direct_monitor_schema_unavailable",
            operation="scope_not_accepted",
        )
        return None
    return schema


def _direct_handler_for_run(
    run: RunConfig,
    settings: Settings,
    *,
    browser_monitor: QueueSessionMonitor,
    state_store: FileSystemStateStore,
    target_url: str,
    metadata: DirectMonitorMetadataRepository | None = None,
    observability: PrometheusMetrics | None = None,
    proxy_resolver: SessionProxyResolver | None = None,
) -> DirectMonitoringHandler | None:
    if run.monitoring_strategy is not MonitoringStrategy.DIRECT:
        return None
    scopes = accepted_evidence_scopes(target_url)
    schema = _direct_response_schema(settings, target_url)
    parser = (
        DirectResponseParser(
            schema, admission_matcher=AdmissionDetector.from_urls(target_url).matches
        )
        if schema is not None
        else None
    )
    store = DirectMonitorStateStore(settings.direct_monitor_directory)
    harvester = (
        DiscoveryRecipeHarvester(
            evidence_directory=settings.status_discovery_directory,
            parser=parser,
            accepted_scopes=scopes,
        )
        if parser is not None and settings.status_discovery_enabled
        else None
    )
    return DirectMonitoringHandler(
        browser_monitor=browser_monitor,
        checker=DirectStatusChecker(
            store=store,
            browser_state=state_store,
            parser=parser,
            accepted_scopes=scopes,
            timeout_seconds=settings.direct_monitor_timeout_seconds,
            max_response_bytes=settings.direct_monitor_max_response_bytes,
            failure_threshold=settings.direct_monitor_failure_threshold,
            proxy_resolver=proxy_resolver,
        ),
        store=store,
        harvester=harvester,
        metadata=metadata,
        observability=observability,
        readopt_cooldown_seconds=settings.direct_monitor_readopt_cooldown_seconds,
        discovery_retention=settings.direct_monitor_discovery_retention,
    )


def _metadata_repository(repository: SessionRepository) -> DirectMonitorMetadataRepository | None:
    if hasattr(repository, "record_direct_monitor_status") and hasattr(
        repository, "direct_capability_counts"
    ):
        return cast(DirectMonitorMetadataRepository, repository)
    return None


def _status_discovery_for_run(
    run: RunConfig,
    settings: Settings,
) -> StatusDiscoveryFactory | None:
    """Enable sensitive evidence only for an explicitly opted-in Direct run."""

    if (
        run.monitoring_strategy is not MonitoringStrategy.DIRECT
        or not settings.status_discovery_enabled
    ):
        return None
    return StatusDiscoveryFactory(
        evidence_directory=settings.status_discovery_directory,
        scope=settings.status_discovery_scope,
        max_exchanges=settings.status_discovery_max_exchanges,
        max_body_bytes=settings.status_discovery_max_body_bytes,
        event_queue_capacity=settings.status_discovery_event_queue_capacity,
        cleanup_timeout_seconds=settings.status_discovery_cleanup_timeout_seconds,
        observe_seconds=settings.status_discovery_observe_seconds,
    )


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
        """``manual_headless`` exists for controlled local browser tests only."""

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
        self._direct_handler: DirectMonitoringHandler | None = None
        self._creator: QueueSessionCreator | None = None
        self._metrics: PrometheusMetrics | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start_run(self, run: RunConfig) -> None:
        async with self._lock:
            if self._task is not None:
                return
            if run.proxy_provider is ProxyProvider.IPROYAL:
                if not run.proxy_country or not run.proxy_lifetime:
                    raise ValueError(
                        "Persisted IPRoyal run provenance is incomplete; use Stop & Reset Run"
                    )
                if run.browser_backend is BrowserBackendName.CAMOUFOX:
                    raise ValueError(
                        "Persisted IPRoyal runs require Patchright or Chrome; "
                        "use Stop & Reset Run"
                    )
                # Account values remain environment-only. Resolve them before any
                # browser manager starts so a missing restart credential fails closed.
                self._base_settings.require_iproyal_credentials()
            if (
                run.browser_backend is BrowserBackendName.CAMOUFOX
                and run.browser_build is not None
                and run.browser_build != CAMOUFOX_BROWSER_VERSION
            ):
                # Never silent: the run keeps its backend, and Queue ID verification
                # still guards every restore, but the operator must know the pinned
                # build changed underneath a persisted run.
                log_event(
                    logger,
                    logging.WARNING,
                    "run_browser_build_changed",
                    browser_backend=run.browser_backend.value,
                    recorded_build=run.browser_build,
                    installed_build=CAMOUFOX_BROWSER_VERSION,
                )
            validated_url = TypeAdapter(HttpUrl).validate_python(run.target_url)
            settings = self._base_settings.model_copy(
                update={
                    "staging_url": validated_url,
                    "target_queue_ids": run.requested_sessions,
                    "browser_backend": run.browser_backend,
                    "monitoring_strategy": run.monitoring_strategy,
                }
            )
            metrics = PrometheusMetrics()
            metrics.set_proxy_provider(run.proxy_provider.value)
            # The single authority for every per-session proxy in this run: acquisition,
            # restore, monitoring, Refresh Now, Manual Open, and Direct requests.
            proxy_resolver = SessionProxyResolver.for_run(run, self._base_settings, observer=metrics)
            proxy_ip_tracker = (
                ProxyIpTracker(
                    resolver=proxy_resolver,
                    observer=ProxyIpObserver(
                        endpoint=self._base_settings.proxy_ip_endpoint,
                        timeout_seconds=self._base_settings.proxy_ip_timeout_seconds,
                        connect_timeout_seconds=(
                            self._base_settings.proxy_ip_connect_timeout_seconds
                        ),
                    ),
                    store=cast(ProxyIpStore, self._repository),
                    observability=metrics,
                )
                if proxy_resolver.enabled and hasattr(self._repository, "record_proxy_ip")
                else None
            )
            state_store = FileSystemStateStore(settings.state_directory)
            # Before any creation or operator work starts in this runtime, so no live
            # creator can own a matching row.
            await self._discard_orphaned_reservations(state_store, settings)
            shared_capacity = BrowserContextCapacity(settings.max_active_contexts)
            browser_manager = BrowserManager.from_settings(
                settings,
                observability=metrics,
                shared_capacity=shared_capacity,
            )
            target_url = str(validated_url)
            status_discovery = _status_discovery_for_run(run, settings)
            # Acquisition (setup, Add, Replace) may use its own visible browser while
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
                    timezone_id=settings.browser_timezone,
                )
            creator = QueueSessionCreator(
                browser_manager=creation_browser_manager,
                repository=self._repository,
                state_store=state_store,
                staging_url=target_url,
                state_directory=settings.state_directory,
                mode=settings.session_mode,
                browser_backend=run.browser_backend,
                proxy_resolver=proxy_resolver,
                proxy_ip_tracker=proxy_ip_tracker,
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
                browser_backend=run.browser_backend,
                status_discovery_factory=status_discovery,
                proxy_resolver=proxy_resolver,
            )
            monitor = QueueSessionMonitor(
                repository=self._repository,
                restorer=restorer,
                polling_policy=PollingPolicy.from_settings(settings),
                retry_policy=MonitoringRetryPolicy.from_settings(settings),
                observability=metrics,
                proxy_ip_tracker=proxy_ip_tracker,
            )
            direct_handler = _direct_handler_for_run(
                run,
                settings,
                browser_monitor=monitor,
                state_store=state_store,
                target_url=target_url,
                metadata=_metadata_repository(self._repository),
                observability=metrics,
                proxy_resolver=proxy_resolver,
            )
            metrics.set_monitoring_strategy(run.monitoring_strategy.value)
            automatic_monitor = _automatic_monitor_for_strategy(
                run.monitoring_strategy,
                browser_monitor=monitor,
                direct_handler=direct_handler,
            )
            manual_processes, manual_contexts_per_process = manual_pool_topology(
                settings.browser_backend, settings.max_manual_open_sessions
            )
            headed_manager = BrowserManager(
                chrome_process_count=manual_processes,
                max_contexts_per_browser=manual_contexts_per_process,
                max_active_contexts=settings.max_manual_open_sessions,
                headless=self._manual_headless,
                observability=metrics,
                shared_capacity=shared_capacity,
                backend=create_browser_backend(settings.browser_backend),
                timezone_id=settings.browser_timezone,
            )
            headed_restorer = QueueSessionRestorer(
                browser_manager=headed_manager,
                repository=self._repository,
                state_store=state_store,
                admission_detector=AdmissionDetector.from_urls(target_url),
                storage_navigation_url=target_url,
                admission_wait_timeout_ms=settings.admission_wait_seconds * 1_000,
                observability=metrics,
                browser_backend=run.browser_backend,
                proxy_resolver=proxy_resolver,
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
            # Refresh Now uses the run's selected strategy (Direct keeps its browser
            # fallback); Manual Open above always uses the browser monitor.
            operator_actions = OperatorActionManager(
                repository=self._repository,
                creator=creator,
                monitor=automatic_monitor,
                state_store=state_store,
                session_cleanup=DirectMonitorStateStore(
                    settings.direct_monitor_directory
                ).delete,
                target_adjustment=creation,
                worker_count=settings.operator_workers,
                queue_capacity=settings.operator_queue_capacity,
                lease_seconds=settings.operator_lease_seconds,
            )
            await operator_actions.start()
            scheduler = ParkedSessionScheduler.from_settings(
                settings,
                repository=self._repository,
                handler=automatic_monitor,
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
                # After producers and automatic monitoring stop, but while the browser is
                # still usable: finish or cancel operator work, then persist and close
                # headed sessions and release their ownership.
                before_browser_shutdown=(
                    (
                        "operator_actions",
                        lambda: operator_actions.close(
                            timeout_seconds=settings.shutdown_timeout_seconds
                        ),
                    ),
                    ("manual_browser", manual_sessions.close),
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
            self._direct_handler = direct_handler
            self._creator = creator
            self._metrics = metrics
            self._task = asyncio.create_task(runtime.run(), name=f"run-{run.run_id}")

    async def _discard_orphaned_reservations(
        self, state_store: FileSystemStateStore, settings: Settings
    ) -> None:
        """Remove creation reservations a previous process left unfinished."""

        discard = getattr(self._repository, "discard_orphaned_reservations", None)
        if discard is None:
            return
        session_ids: tuple[str, ...] = await discard()
        if not session_ids:
            return
        direct_store = DirectMonitorStateStore(settings.direct_monitor_directory)
        for session_id in session_ids:
            # A crash between saving state and persisting the Queue ID can leave a file.
            with contextlib.suppress(Exception):
                await state_store.delete(session_id)
            with contextlib.suppress(Exception):
                await direct_store.delete(session_id)
        log_event(
            logger,
            logging.WARNING,
            "orphaned_creation_reservations_discarded",
            count=len(session_ids),
        )

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

    async def metrics_exposition(self) -> bytes:
        """Prometheus text for the current run; aggregate, low-cardinality only."""

        metrics = self._metrics
        if metrics is None:
            return b""
        metadata = _metadata_repository(self._repository)
        if self._direct_handler is not None and metadata is not None:
            try:
                metrics.set_direct_capability_counts(await metadata.direct_capability_counts())
            except Exception as exc:  # noqa: BLE001 - serve last known values
                metrics.record_repository_error("status")
                log_event(
                    logger,
                    logging.WARNING,
                    "status_snapshot_failed",
                    operation="direct_capability",
                    error_type=type(exc).__name__,
                )
        return metrics.render()

    @property
    def access_restriction_status(self) -> AccessRestrictionStatus:
        """Aggregate pre-Queue restriction state of the current runtime only."""

        creator = self._creator
        return AccessRestrictionStatus(
            attempts=creator.access_restricted_attempts if creator is not None else 0,
        )

    @property
    def direct_monitoring_metrics(self) -> DirectMonitoringMetrics | None:
        """In-process Direct strategy counters; ``None`` for a Headed Window run."""

        handler = self._direct_handler
        return handler.metrics if handler is not None else None

    async def close(self) -> None:
        """Shut down in dependency order without deleting or replacing identities.

        1. reject new operator/headed requests; 2. stop creation and automatic
        claims; 3. drain in-flight automatic checks and release queued leases;
        4. finish or cancel operator work; 5. persist and close headed sessions and
        release their ownership; 6. close browsers and the repository. Steps 3-6 run
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
            self._direct_handler = None
            self._creator = None
            self._metrics = None
        try:
            await self._repository.reset_all()
        except Exception:
            active = await self._repository.get_active_run()
            if active is not None:
                await self.start_run(active)
            raise
        removed = await FileSystemStateStore(self._base_settings.state_directory).clear()
        removed += await DirectMonitorStateStore(
            self._base_settings.direct_monitor_directory
        ).clear()
        log_event(logger, logging.INFO, "run_reset_completed", count=removed)


def browser_build_label(run: RunConfig) -> str:
    """Operator-facing build provenance; a changed pinned build is shown, never hidden."""

    if run.browser_backend is BrowserBackendName.CHROME:
        return "installed Google Chrome"
    if run.browser_backend is BrowserBackendName.PATCHRIGHT:
        if run.browser_build is None:
            return "installed Google Chrome via Patchright (run build not recorded)"
        return f"{run.browser_build} via Patchright"
    if run.browser_build is None:
        return f"{CAMOUFOX_BROWSER_VERSION} (run build not recorded)"
    if run.browser_build != CAMOUFOX_BROWSER_VERSION:
        return f"{CAMOUFOX_BROWSER_VERSION} (run created with {run.browser_build})"
    return run.browser_build


@dataclass(frozen=True, slots=True)
class AccessRestrictionStatus:
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    target_url: str
    requested_sessions: int
    browser_backend: str
    browser_build: str
    monitoring_strategy: str
    proxy_provider: str
    proxy_country: str | None
    proxy_lifetime: str | None
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
    # Direct Monitoring Strategy aggregates only (None for Headed Window runs).
    direct_capability: tuple[tuple[str, int], ...] | None = None
    direct_checks: str | None = None
    # Pre-Queue access-restriction attempts in the current runtime (aggregate only).
    access_restricted_attempts: int = 0


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
        direct_capability, direct_checks = await self._direct_summary(run)
        restriction = getattr(self._runtime, "access_restriction_status", None)
        if not isinstance(restriction, AccessRestrictionStatus):
            restriction = AccessRestrictionStatus()
        if runtime_error is not None:
            creation = "ERROR"
        elif recovery.valid_queue_ids >= effective_target:
            creation = "COMPLETE"
        else:
            creation = "RUNNING"
        return DashboardSummary(
            target_url=run.target_url,
            requested_sessions=run.requested_sessions,
            browser_backend=run.browser_backend.value,
            browser_build=browser_build_label(run),
            monitoring_strategy=run.monitoring_strategy.label,
            proxy_provider=run.proxy_provider.label,
            proxy_country=(run.proxy_country.upper() if run.proxy_country else None),
            proxy_lifetime=run.proxy_lifetime,
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
            direct_capability=direct_capability,
            direct_checks=direct_checks,
            access_restricted_attempts=restriction.attempts,
        )

    async def _direct_summary(
        self, run: RunConfig
    ) -> tuple[tuple[tuple[str, int], ...] | None, str | None]:
        if run.monitoring_strategy is not MonitoringStrategy.DIRECT:
            return None, None
        metadata = _metadata_repository(self._repository)
        counts = await metadata.direct_capability_counts() if metadata is not None else {}
        capability = tuple(
            (value.value, counts.get(value.value, 0)) for value in DirectCapability
        )
        metrics = getattr(self._runtime, "direct_monitoring_metrics", None)
        checks = (
            f"{metrics.direct_successes} direct / {metrics.browser_fallbacks} fallback"
            if isinstance(metrics, DirectMonitoringMetrics)
            else None
        )
        return capability, checks
