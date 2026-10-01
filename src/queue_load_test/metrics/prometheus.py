"""Low-cardinality Prometheus instrumentation for the Phase 1 runtime."""

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from queue_load_test.models import (
    DirectCapability,
    DirectFallbackReason,
    QueueProgress,
    QueueSession,
    QueueStatus,
)
from queue_load_test.proxy import ProxyFailure, ProxyPurpose
from queue_load_test.repository import PROGRESS_BUCKETS, RecoverySummary

_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

REPOSITORY_ERROR_OPERATIONS: tuple[str, ...] = (
    "schedule",
    "release",
    "repark",
    "status",
    "other",
)
"""Fixed label values for repository errors; unknown operations collapse to ``other``."""

FORBIDDEN_LABEL_NAMES = frozenset(
    {"queue_id", "session_id", "transfer_url", "state_path", "proxy_session_id", "ip"}
)


@dataclass(frozen=True, slots=True)
class CheckStatistics:
    checks_total: int
    checks_per_second: float
    average_check_duration_seconds: float


class PrometheusMetrics:
    """Own application metrics without per-session or per-Queue-ID labels."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.queue_sessions_requested = Gauge(
            "queue_sessions_requested",
            "Configured unique Queue ID target.",
            registry=self.registry,
        )
        self.queue_sessions_created_total = Counter(
            "queue_sessions_created_total",
            "Successfully persisted Queue-it sessions.",
            registry=self.registry,
        )
        self.queue_ids_acquired_total = Counter(
            "queue_ids_acquired_total",
            "Unique Queue IDs acquired by this process.",
            registry=self.registry,
        )
        self.queue_creation_attempts_total = Counter(
            "queue_creation_attempts_total",
            "Browser session creation attempts.",
            registry=self.registry,
        )
        self.queue_creation_failures_total = Counter(
            "queue_creation_failures_total",
            "Session creation attempts ending in failure.",
            registry=self.registry,
        )
        self.queue_creation_duplicates_total = Counter(
            "queue_creation_duplicates_total",
            "Creation results rejected because the Queue ID already exists.",
            registry=self.registry,
        )
        self.queue_creation_transient_failures_total = Counter(
            "queue_creation_transient_failures_total",
            "Transient creation failures, including failures recovered by retry.",
            registry=self.registry,
        )
        self.queue_creation_retries_total = Counter(
            "queue_creation_retries_total",
            "Creation retries started after a transient failure.",
            registry=self.registry,
        )
        self.queue_creation_permanent_failures_total = Counter(
            "queue_creation_permanent_failures_total",
            "Permanent creation failures that are not retried.",
            registry=self.registry,
        )
        self.queue_creation_access_restricted_total = Counter(
            "queue_creation_access_restricted_total",
            "Creation attempts that rendered the access-restriction page before a Queue ID.",
            registry=self.registry,
        )
        self.state_persistence_failures_total = Counter(
            "state_persistence_failures_total",
            "Browser storage-state writes that failed during session creation.",
            registry=self.registry,
        )
        self.queue_creation_in_flight = Gauge(
            "queue_creation_in_flight",
            "Creation workers currently processing a session.",
            registry=self.registry,
        )
        self.queue_creation_queue_depth = Gauge(
            "queue_creation_queue_depth",
            "Creation work items waiting in the bounded queue.",
            registry=self.registry,
        )
        self.queue_sessions_created_per_second = Gauge(
            "queue_sessions_created_per_second",
            "Unique Queue IDs acquired per second during the current creation run.",
            registry=self.registry,
        )
        self.active_browser_contexts = Gauge(
            "active_browser_contexts",
            "Currently allocated browser contexts.",
            registry=self.registry,
        )
        self.active_browser_contexts_peak = Gauge(
            "active_browser_contexts_peak",
            "Peak simultaneously allocated browser contexts in this process.",
            registry=self.registry,
        )
        self.browser_processes = Gauge(
            "browser_processes",
            "Managed browser processes.",
            registry=self.registry,
        )
        self.browser_crashes_total = Counter(
            "browser_crashes_total",
            "Detected browser process failures.",
            registry=self.registry,
        )
        self.browser_context_creation_failures_total = Counter(
            "browser_context_creation_failures_total",
            "BrowserContext creation calls that failed.",
            registry=self.registry,
        )
        self.browser_cleanup_failures_total = Counter(
            "browser_cleanup_failures_total",
            "BrowserContext, browser process, or Playwright cleanup calls that failed.",
            registry=self.registry,
        )
        self.browser_context_creation_duration_seconds = Histogram(
            "browser_context_creation_duration_seconds",
            "Time spent inside the selected Chrome process creating a BrowserContext.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.browser_context_acquisition_duration_seconds = Histogram(
            "browser_context_acquisition_duration_seconds",
            "Time spent acquiring a BrowserContext, including manager contention.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.browser_context_acquisition_wait_seconds = Histogram(
            "browser_context_acquisition_wait_seconds",
            "Time spent waiting for BrowserManager allocation locks.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.session_creation_duration_seconds = Histogram(
            "session_creation_duration_seconds",
            "Queue-it session creation duration.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.session_restore_duration_seconds = Histogram(
            "session_restore_duration_seconds",
            "Queue-it session restoration duration.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.queue_check_duration_seconds = Histogram(
            "queue_check_duration_seconds",
            "End-to-end parked-session check duration.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.state_restore_failures_total = Counter(
            "state_restore_failures_total",
            "Storage-state restoration failures.",
            registry=self.registry,
        )
        self.transfer_restore_failures_total = Counter(
            "transfer_restore_failures_total",
            "Official transfer restoration failures.",
            registry=self.registry,
        )
        self.identity_mismatches_total = Counter(
            "identity_mismatches_total",
            "Observed Queue IDs that did not match expected identities.",
            registry=self.registry,
        )
        self.navigation_timeouts_total = Counter(
            "navigation_timeouts_total",
            "Browser navigation timeouts.",
            registry=self.registry,
        )
        self.navigation_failures_total = Counter(
            "navigation_failures_total",
            "Browser navigation calls that failed, including timeouts.",
            registry=self.registry,
        )
        self.navigation_duration_seconds = Histogram(
            "navigation_duration_seconds",
            "Browser page navigation duration.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.checks_total = Counter(
            "checks_total",
            "Completed parked-session checks.",
            registry=self.registry,
        )
        self.monitoring_workers_active = Gauge(
            "monitoring_workers_active",
            "Monitoring workers currently checking a leased session.",
            registry=self.registry,
        )
        self.monitoring_queue_depth = Gauge(
            "monitoring_queue_depth",
            "Leased monitoring sessions waiting in the bounded work queue.",
            registry=self.registry,
        )
        self.monitoring_due_backlog = Gauge(
            "monitoring_due_backlog",
            "Due, currently unleased sessions waiting to be claimed.",
            registry=self.registry,
        )
        self.monitoring_overdue_sessions = Gauge(
            "monitoring_overdue_sessions",
            "Claimable sessions whose next check time has arrived.",
            registry=self.registry,
        )
        self.monitoring_oldest_overdue_seconds = Gauge(
            "monitoring_oldest_overdue_seconds",
            "Age in seconds of the oldest currently claimable session.",
            registry=self.registry,
        )
        self.monitoring_sessions_claimed_total = Counter(
            "monitoring_sessions_claimed_total",
            "Sessions successfully claimed for monitoring.",
            registry=self.registry,
        )
        self.monitoring_lease_conflicts_total = Counter(
            "monitoring_lease_conflicts_total",
            "Expected monitoring claims or lease releases lost to another owner.",
            registry=self.registry,
        )
        self.queue_progress_percentage = Histogram(
            "queue_progress_percentage",
            "Aggregate observed Queue-it progress percentage without session labels.",
            buckets=(0, 10, 25, 50, 75, 90, 100),
            registry=self.registry,
        )
        self.queue_users_ahead = Histogram(
            "queue_users_ahead",
            "Aggregate observed users-ahead values without session labels.",
            buckets=(0, 1, 10, 100, 1_000, 10_000, 100_000),
            registry=self.registry,
        )
        self.queue_ids_valid = Gauge(
            "queue_ids_valid",
            "Persisted unique Queue IDs that currently count toward the target.",
            registry=self.registry,
        )
        self.queue_ids_remaining = Gauge(
            "queue_ids_remaining",
            "Queue IDs still required to reach the configured target.",
            registry=self.registry,
        )
        self.queue_ids_lost = Gauge(
            "queue_ids_lost",
            "Acquired Queue IDs whose sessions later became FAILED.",
            registry=self.registry,
        )
        self.queue_sessions_persisted = Gauge(
            "queue_sessions_persisted",
            "All persisted session rows, including failed creation attempts.",
            registry=self.registry,
        )
        self.queue_sessions_requiring_retry = Gauge(
            "queue_sessions_requiring_retry",
            "Non-terminal sessions with a recorded error or incomplete creation.",
            registry=self.registry,
        )
        self.queue_identity_replacement_blocked = Gauge(
            "queue_identity_replacement_blocked",
            "1 when creation stopped because replacing lost identities hit its limit.",
            registry=self.registry,
        )
        self.queue_sessions_progress_bucket = Gauge(
            "queue_sessions_progress_bucket",
            "Non-terminal persisted sessions by last observed progress range.",
            ["bucket"],
            registry=self.registry,
        )
        self.monitoring_leases_active = Gauge(
            "monitoring_leases_active",
            "Persisted sessions holding an unexpired monitoring lease.",
            registry=self.registry,
        )
        self.monitoring_leases_expired = Gauge(
            "monitoring_leases_expired",
            "Persisted sessions whose lease expired without release.",
            registry=self.registry,
        )
        self.monitoring_lease_recoveries_total = Counter(
            "monitoring_lease_recoveries_total",
            "Expired leases taken over by a new claim after the owner stopped.",
            registry=self.registry,
        )
        self.monitoring_checks_per_second = Gauge(
            "monitoring_checks_per_second",
            "Average parked-session checks per second since process start.",
            registry=self.registry,
        )
        self.monitoring_check_duration_average_seconds = Gauge(
            "monitoring_check_duration_average_seconds",
            "Average end-to-end check duration since process start.",
            registry=self.registry,
        )
        self.monitoring_workers_configured = Gauge(
            "monitoring_workers_configured",
            "Fixed monitoring worker tasks in this single-machine process.",
            registry=self.registry,
        )
        self.creation_workers_configured = Gauge(
            "creation_workers_configured",
            "Fixed creation worker tasks in this single-machine process.",
            registry=self.registry,
        )
        self.repository_errors_total = Counter(
            "repository_errors_total",
            "Repository operations that raised, by fixed operation name.",
            ["operation"],
            registry=self.registry,
        )
        self.state_refresh_failures_total = Counter(
            "state_refresh_failures_total",
            "Verified observations whose HYBRID storage-state refresh could not be saved.",
            registry=self.registry,
        )
        self.browser_restart_duration_seconds = Histogram(
            "browser_restart_duration_seconds",
            "Time to replace one disconnected Chrome process.",
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.browser_restart_failures_total = Counter(
            "browser_restart_failures_total",
            "Chrome process replacements that failed to launch or install.",
            registry=self.registry,
        )
        self.browser_operation_timeouts_total = Counter(
            "browser_operation_timeouts_total",
            "Browser attempts abandoned because a Playwright call exceeded its deadline.",
            registry=self.registry,
        )
        self.browser_backend_info = Gauge(
            "browser_backend_info",
            "Constant 1 labelled with the running browser backend and browser build.",
            labelnames=("backend", "browser_build"),
            registry=self.registry,
        )
        self.browser_unresponsive_restarts_total = Counter(
            "browser_unresponsive_restarts_total",
            "Connected browser processes restarted after consecutive navigation timeouts.",
            registry=self.registry,
        )
        self.browser_contexts_lost_total = Counter(
            "browser_contexts_lost_total",
            "BrowserContexts invalidated because their browser process disconnected.",
            registry=self.registry,
        )
        self.startup_recovery_duration_seconds = Gauge(
            "startup_recovery_duration_seconds",
            "Duration of the aggregate startup recovery query.",
            registry=self.registry,
        )
        self.startup_expired_leases = Gauge(
            "startup_expired_leases",
            "Expired leases found at startup and awaiting recovery.",
            registry=self.registry,
        )
        self.startup_due_backlog = Gauge(
            "startup_due_backlog",
            "Claimable due sessions found at startup.",
            registry=self.registry,
        )
        # Per-session IPRoyal proxy (Phase 9). Labels are closed enums only; never a
        # provider session ID, QueueSession ID, Queue ID, or IP address.
        self.proxy_provider_info = Gauge(
            "proxy_provider_info",
            "Constant 1 labelled with the current run's persisted proxy provider.",
            labelnames=("provider",),
            registry=self.registry,
        )
        self.proxied_attempts_total = Counter(
            "proxied_attempts_total",
            "Proxied browser contexts or Direct requests opened, by purpose.",
            labelnames=("purpose",),
            registry=self.registry,
        )
        self.proxy_ip_observations_total = Counter(
            "proxy_ip_observations_total",
            "Post-check proxy-exit IP observations by sanitized result (never the IP).",
            labelnames=("result",),
            registry=self.registry,
        )
        self.proxy_failures_total = Counter(
            "proxy_failures_total",
            "Per-session proxy failures by purpose and sanitized reason.",
            labelnames=("purpose", "reason"),
            registry=self.registry,
        )
        # Direct Monitoring Strategy (Phase 8). Labels are closed enums only:
        # never a session, Queue ID, URL, or any recipe material.
        self.monitoring_strategy_info = Gauge(
            "monitoring_strategy_info",
            "Constant 1 labelled with the current run's persisted monitoring strategy.",
            labelnames=("strategy",),
            registry=self.registry,
        )
        self.direct_monitoring_attempts_total = Counter(
            "direct_monitoring_attempts_total",
            "Direct visitor-status requests sent.",
            registry=self.registry,
        )
        self.direct_monitoring_successes_total = Counter(
            "direct_monitoring_successes_total",
            "Direct observations validated and persisted without a browser.",
            registry=self.registry,
        )
        self.direct_monitoring_fallbacks_total = Counter(
            "direct_monitoring_fallbacks_total",
            "Direct Monitoring checks that used the browser fallback, by sanitized reason.",
            labelnames=("reason",),
            registry=self.registry,
        )
        self.direct_monitoring_request_duration_seconds = Histogram(
            "direct_monitoring_request_duration_seconds",
            "Duration of one direct visitor-status request and validation.",
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=self.registry,
        )
        self.direct_monitoring_fallback_duration_seconds = Histogram(
            "direct_monitoring_fallback_duration_seconds",
            "Duration of the browser fallback check after a direct failure.",
            buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120),
            registry=self.registry,
        )
        self.direct_monitoring_disagreements_total = Counter(
            "direct_monitoring_disagreements_total",
            "Refused direct observations whose lifecycle differed from the browser fallback.",
            registry=self.registry,
        )
        self.direct_monitoring_identity_mismatches_total = Counter(
            "direct_monitoring_identity_mismatches_total",
            "Direct responses with an ambiguous or contradicting Queue ID.",
            registry=self.registry,
        )
        self.direct_monitoring_recipes_refreshed_total = Counter(
            "direct_monitoring_recipes_refreshed_total",
            "Direct recipes adopted from a newly observed legitimate browser request.",
            registry=self.registry,
        )
        self.direct_monitoring_sessions = Gauge(
            "direct_monitoring_sessions",
            "Monitorable identified sessions by internal direct capability.",
            labelnames=("capability",),
            registry=self.registry,
        )
        for reason in DirectFallbackReason:
            self.direct_monitoring_fallbacks_total.labels(reason=reason.value)
        for capability in DirectCapability:
            self.direct_monitoring_sessions.labels(capability=capability.value).set(0)
        for operation in REPOSITORY_ERROR_OPERATIONS:
            self.repository_errors_total.labels(operation=operation)
        for bucket in PROGRESS_BUCKETS:
            self.queue_sessions_progress_bucket.labels(bucket=bucket).set(0)
        self._session_gauges = self._create_session_gauges()
        self._started_at = time.monotonic()
        self._check_count = 0
        self._check_duration = 0.0
        self._browser_context_peak = 0
        self._lock = threading.Lock()

    def _create_session_gauges(self) -> dict[QueueStatus, Gauge]:
        names = {
            QueueStatus.NEW: "queue_sessions_new",
            QueueStatus.CREATING: "queue_sessions_creating",
            QueueStatus.PRE_QUEUE: "queue_sessions_prequeue",
            QueueStatus.ACTIVE_QUEUE: "queue_sessions_active",
            QueueStatus.PARKED: "queue_sessions_parked",
            QueueStatus.CHECKING: "queue_sessions_checking",
            QueueStatus.PAUSED: "queue_sessions_paused",
            QueueStatus.CONNECTION_LOST: "queue_sessions_connection_lost",
            QueueStatus.SERVICED_SOON: "queue_sessions_serviced_soon",
            QueueStatus.TURN_STARTED: "queue_sessions_turn_started",
            QueueStatus.READY: "queue_sessions_ready",
            QueueStatus.ADMITTED: "queue_sessions_admitted",
            QueueStatus.EXPIRED: "queue_sessions_expired",
            QueueStatus.FAILED: "queue_sessions_failed",
        }
        return {
            status: Gauge(
                name, f"Persisted sessions currently in {status.value}.", registry=self.registry
            )
            for status, name in names.items()
        }

    def set_target(self, requested: int) -> None:
        self.queue_sessions_requested.set(requested)

    def record_creation_attempt(self) -> None:
        self.queue_creation_attempts_total.inc()

    def record_creation_success(self, duration_seconds: float) -> None:
        self.queue_sessions_created_total.inc()
        self.queue_ids_acquired_total.inc()
        self.session_creation_duration_seconds.observe(duration_seconds)
        self._session_gauges[QueueStatus.PARKED].inc()

    def record_creation_failure(self, duration_seconds: float) -> None:
        self.queue_creation_failures_total.inc()
        self.session_creation_duration_seconds.observe(duration_seconds)

    def record_creation_duplicate(self) -> None:
        self.queue_creation_duplicates_total.inc()

    def record_creation_transient_failure(self) -> None:
        self.queue_creation_transient_failures_total.inc()

    def record_creation_retry(self) -> None:
        self.queue_creation_retries_total.inc()

    def record_creation_permanent_failure(self) -> None:
        self.queue_creation_permanent_failures_total.inc()

    def record_creation_access_restricted(self) -> None:
        self.queue_creation_access_restricted_total.inc()

    def record_state_persistence_failure(self) -> None:
        self.state_persistence_failures_total.inc()

    def set_creation_activity(self, *, in_flight: int, queue_depth: int) -> None:
        self.queue_creation_in_flight.set(in_flight)
        self.queue_creation_queue_depth.set(queue_depth)

    def set_creation_rate(self, sessions_per_second: float) -> None:
        self.queue_sessions_created_per_second.set(sessions_per_second)

    def record_restore(
        self,
        duration_seconds: float,
        *,
        success: bool,
        used_storage_state: bool,
        identity_mismatch: bool,
        transfer_failures: int | None = None,
        state_failures: int | None = None,
        state_refresh_failures: int = 0,
    ) -> None:
        self.session_restore_duration_seconds.observe(duration_seconds)
        if state_refresh_failures:
            self.state_refresh_failures_total.inc(state_refresh_failures)
        if transfer_failures is not None or state_failures is not None:
            self.transfer_restore_failures_total.inc(transfer_failures or 0)
            self.state_restore_failures_total.inc(state_failures or 0)
        elif not success:
            if used_storage_state:
                self.state_restore_failures_total.inc()
            else:
                self.transfer_restore_failures_total.inc()
        if identity_mismatch:
            self.identity_mismatches_total.inc()

    def record_navigation_timeout(self) -> None:
        self.navigation_timeouts_total.inc()

    def record_navigation_failure(self, *, timed_out: bool = False) -> None:
        self.navigation_failures_total.inc()
        if timed_out:
            self.navigation_timeouts_total.inc()

    def record_navigation_duration(self, duration_seconds: float) -> None:
        self.navigation_duration_seconds.observe(duration_seconds)

    def record_check(self, duration_seconds: float, progress: QueueProgress | None) -> None:
        self.checks_total.inc()
        self.queue_check_duration_seconds.observe(duration_seconds)
        if progress is not None:
            if progress.progress_percentage is not None:
                self.queue_progress_percentage.observe(progress.progress_percentage)
            if progress.users_ahead is not None:
                self.queue_users_ahead.observe(progress.users_ahead)
        with self._lock:
            self._check_count += 1
            self._check_duration += duration_seconds

    def set_monitoring_activity(
        self,
        *,
        active_workers: int,
        queue_depth: int,
    ) -> None:
        self.monitoring_workers_active.set(active_workers)
        self.monitoring_queue_depth.set(queue_depth)

    def set_monitoring_backlog(
        self,
        due_sessions: int,
        *,
        oldest_overdue_seconds: float = 0.0,
    ) -> None:
        self.monitoring_due_backlog.set(due_sessions)
        self.monitoring_overdue_sessions.set(due_sessions)
        self.monitoring_oldest_overdue_seconds.set(max(0.0, oldest_overdue_seconds))

    def record_monitoring_claims(self, claimed: int) -> None:
        self.monitoring_sessions_claimed_total.inc(claimed)

    def record_monitoring_lease_conflicts(self, conflicts: int = 1) -> None:
        self.monitoring_lease_conflicts_total.inc(conflicts)

    def set_browser_capacity(self, *, active_contexts: int, processes: int) -> None:
        self.active_browser_contexts.set(active_contexts)
        self._browser_context_peak = max(self._browser_context_peak, active_contexts)
        self.active_browser_contexts_peak.set(self._browser_context_peak)
        self.browser_processes.set(processes)

    def record_browser_crash(self) -> None:
        self.browser_crashes_total.inc()

    def record_context_creation_failure(self) -> None:
        self.browser_context_creation_failures_total.inc()

    def record_browser_cleanup_failure(self) -> None:
        self.browser_cleanup_failures_total.inc()

    def record_context_creation_duration(self, duration_seconds: float) -> None:
        self.browser_context_creation_duration_seconds.observe(duration_seconds)

    def record_context_acquisition_duration(self, duration_seconds: float) -> None:
        self.browser_context_acquisition_duration_seconds.observe(duration_seconds)

    def record_context_acquisition_wait(self, duration_seconds: float) -> None:
        self.browser_context_acquisition_wait_seconds.observe(duration_seconds)

    def sync_session_counts(self, sessions: Iterable[QueueSession]) -> None:
        counts = dict.fromkeys(self._session_gauges, 0)
        for session in sessions:
            if session.status in counts:
                counts[session.status] += 1
        for status, gauge in self._session_gauges.items():
            gauge.set(counts[status])

    def sync_session_count_values(self, counts: dict[QueueStatus, int]) -> None:
        """Initialize lifecycle gauges from repository aggregate counts."""

        for status, gauge in self._session_gauges.items():
            gauge.set(counts.get(status, 0))

    def record_session_transition(
        self,
        previous: QueueStatus,
        current: QueueStatus,
    ) -> None:
        if previous is current:
            return
        if previous in self._session_gauges:
            self._session_gauges[previous].dec()
        if current in self._session_gauges:
            self._session_gauges[current].inc()

    def sync_recovery_summary(self, summary: RecoverySummary, *, target: int) -> None:
        """Set population gauges from one aggregate repository query."""

        self.set_target(target)
        self.queue_ids_valid.set(summary.valid_queue_ids)
        self.queue_ids_remaining.set(max(0, target - summary.valid_queue_ids))
        self.queue_ids_lost.set(summary.lost_queue_ids)
        self.queue_sessions_persisted.set(summary.total_persisted_sessions)
        self.queue_sessions_requiring_retry.set(summary.sessions_requiring_retry)
        self.monitoring_leases_active.set(summary.leased_sessions)
        self.monitoring_leases_expired.set(summary.expired_leases)
        self.sync_session_count_values(summary.status_counts)

    def set_progress_distribution(self, counts: dict[str, int]) -> None:
        for bucket in PROGRESS_BUCKETS:
            self.queue_sessions_progress_bucket.labels(bucket=bucket).set(counts.get(bucket, 0))

    def record_startup_recovery(
        self,
        duration_seconds: float,
        summary: RecoverySummary,
        *,
        target: int | None = None,
    ) -> None:
        self.startup_recovery_duration_seconds.set(duration_seconds)
        self.startup_expired_leases.set(summary.expired_leases)
        self.startup_due_backlog.set(summary.sessions_due)
        if target is not None:
            self.sync_recovery_summary(summary, target=target)
        else:
            self.sync_session_count_values(summary.status_counts)

    def record_lease_recoveries(self, recovered: int) -> None:
        if recovered > 0:
            self.monitoring_lease_recoveries_total.inc(recovered)

    def record_repository_error(self, operation: str) -> None:
        label = operation if operation in REPOSITORY_ERROR_OPERATIONS else "other"
        self.repository_errors_total.labels(operation=label).inc()

    def record_browser_restart(
        self,
        duration_seconds: float,
        *,
        success: bool,
        lost_contexts: int,
    ) -> None:
        self.browser_restart_duration_seconds.observe(duration_seconds)
        if not success:
            self.browser_restart_failures_total.inc()
        if lost_contexts:
            self.browser_contexts_lost_total.inc(lost_contexts)

    def record_browser_operation_timeout(self) -> None:
        self.browser_operation_timeouts_total.inc()

    def set_browser_backend(self, backend: str, *, browser_build: str) -> None:
        """Expose which backend/build this process runs (one low-cardinality series)."""

        self.browser_backend_info.clear()
        self.browser_backend_info.labels(backend=backend, browser_build=browser_build).set(1)

    def record_browser_unresponsive(self) -> None:
        self.browser_unresponsive_restarts_total.inc()

    def set_worker_configuration(self, *, monitoring_workers: int, creation_workers: int) -> None:
        self.monitoring_workers_configured.set(monitoring_workers)
        self.creation_workers_configured.set(creation_workers)

    def set_identity_replacement_blocked(self, blocked: bool) -> None:
        self.queue_identity_replacement_blocked.set(1 if blocked else 0)

    def refresh_rate_gauges(self) -> CheckStatistics:
        statistics = self.check_statistics()
        self.monitoring_checks_per_second.set(statistics.checks_per_second)
        self.monitoring_check_duration_average_seconds.set(
            statistics.average_check_duration_seconds
        )
        return statistics

    def check_statistics(self) -> CheckStatistics:
        with self._lock:
            count = self._check_count
            duration = self._check_duration
        elapsed = max(time.monotonic() - self._started_at, 1e-9)
        return CheckStatistics(
            checks_total=count,
            checks_per_second=count / elapsed,
            average_check_duration_seconds=duration / count if count else 0.0,
        )

    def set_proxy_provider(self, provider: str) -> None:
        self.proxy_provider_info.clear()
        self.proxy_provider_info.labels(provider=provider).set(1)

    def record_proxy_ip_observation(self, result: str) -> None:
        self.proxy_ip_observations_total.labels(result=result).inc()

    def record_proxy_attempt(self, purpose: ProxyPurpose) -> None:
        self.proxied_attempts_total.labels(purpose=ProxyPurpose(purpose).value).inc()

    def record_proxy_failure(self, purpose: ProxyPurpose, failure: ProxyFailure) -> None:
        self.proxy_failures_total.labels(
            purpose=ProxyPurpose(purpose).value, reason=ProxyFailure(failure).value
        ).inc()

    def set_monitoring_strategy(self, strategy: str) -> None:
        self.monitoring_strategy_info.clear()
        self.monitoring_strategy_info.labels(strategy=strategy).set(1)

    def record_direct_attempt(self, duration_seconds: float, *, success: bool) -> None:
        self.direct_monitoring_attempts_total.inc()
        self.direct_monitoring_request_duration_seconds.observe(max(0.0, duration_seconds))
        if success:
            self.direct_monitoring_successes_total.inc()

    def record_direct_fallback(self, reason: DirectFallbackReason, duration_seconds: float) -> None:
        self.direct_monitoring_fallbacks_total.labels(
            reason=DirectFallbackReason(reason).value
        ).inc()
        self.direct_monitoring_fallback_duration_seconds.observe(max(0.0, duration_seconds))

    def record_direct_disagreement(self) -> None:
        self.direct_monitoring_disagreements_total.inc()

    def record_direct_identity_mismatch(self) -> None:
        self.direct_monitoring_identity_mismatches_total.inc()

    def record_direct_recipe_refreshed(self) -> None:
        self.direct_monitoring_recipes_refreshed_total.inc()

    def set_direct_capability_counts(self, counts: dict[str, int]) -> None:
        for capability in DirectCapability:
            self.direct_monitoring_sessions.labels(capability=capability.value).set(
                counts.get(capability.value, 0)
            )

    def render(self) -> bytes:
        return generate_latest(self.registry)
