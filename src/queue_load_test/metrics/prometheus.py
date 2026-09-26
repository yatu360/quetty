"""Low-cardinality Prometheus instrumentation for the Phase 1 runtime."""

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus

_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


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
            "Managed Google Chrome processes.",
            registry=self.registry,
        )
        self.browser_crashes_total = Counter(
            "browser_crashes_total",
            "Detected Chrome process failures.",
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
        self._session_gauges = self._create_session_gauges()
        self._started_at = time.monotonic()
        self._check_count = 0
        self._check_duration = 0.0
        self._browser_context_peak = 0
        self._lock = threading.Lock()

    def _create_session_gauges(self) -> dict[QueueStatus, Gauge]:
        names = {
            QueueStatus.CREATING: "queue_sessions_creating",
            QueueStatus.PRE_QUEUE: "queue_sessions_prequeue",
            QueueStatus.ACTIVE_QUEUE: "queue_sessions_active",
            QueueStatus.PARKED: "queue_sessions_parked",
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
    ) -> None:
        self.session_restore_duration_seconds.observe(duration_seconds)
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

    def set_monitoring_backlog(self, due_sessions: int) -> None:
        self.monitoring_due_backlog.set(due_sessions)

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

    def sync_session_counts(self, sessions: Iterable[QueueSession]) -> None:
        counts = dict.fromkeys(self._session_gauges, 0)
        for session in sessions:
            if session.status in counts:
                counts[session.status] += 1
        for status, gauge in self._session_gauges.items():
            gauge.set(counts[status])

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

    def render(self) -> bytes:
        return generate_latest(self.registry)
