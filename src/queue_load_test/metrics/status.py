"""Human-readable Phase 1 status and a tiny asyncio HTTP endpoint."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.metrics.logging import log_event
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import QueueStatus, SessionMode
from queue_load_test.repository import PROGRESS_BUCKETS, SessionRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatusSummary:
    mode: SessionMode
    target_queue_ids: int
    queue_ids_acquired: int
    remaining: int
    creating: int
    pre_queue: int
    active_queue: int
    parked: int
    serviced_soon: int
    turn_started: int
    ready: int
    admitted: int
    expired: int
    failed: int
    active_contexts: int
    chrome_processes: int
    checks_per_second: float
    average_check_duration_seconds: float
    lost_queue_ids: int = 0
    connection_lost: int = 0
    persisted_sessions: int = 0
    monitoring_workers: int = 0
    creation_workers: int = 0
    creation_rate_per_second: float = 0.0
    queue_depth: int = 0
    due_backlog: int = 0
    oldest_overdue_seconds: float = 0.0
    active_leases: int = 0
    expired_leases: int = 0
    restore_failures: int = 0
    state_refresh_failures: int = 0
    identity_mismatches: int = 0
    browser_crashes: int = 0
    navigation_timeouts: int = 0
    lease_recoveries: int = 0
    repository_errors: int = 0
    progress_buckets: tuple[tuple[str, int], ...] = ()

    def render_text(self) -> str:
        rows = (
            ("Mode", self.mode.value),
            ("Target Queue IDs", self.target_queue_ids),
            ("Queue IDs acquired", self.queue_ids_acquired),
            ("Remaining", self.remaining),
            ("Creating", self.creating),
            ("Pre-queue", self.pre_queue),
            ("Active queue", self.active_queue),
            ("Parked", self.parked),
            ("Serviced soon", self.serviced_soon),
            ("Turn started", self.turn_started),
            ("Ready", self.ready),
            ("Admitted", self.admitted),
            ("Expired", self.expired),
            ("Failed", self.failed),
            ("Connection lost", self.connection_lost),
            ("Lost Queue IDs", self.lost_queue_ids),
            ("Persisted sessions", self.persisted_sessions),
            ("Active contexts", self.active_contexts),
            ("Browser processes", self.chrome_processes),
            ("Workers (monitor/create)", f"{self.monitoring_workers}/{self.creation_workers}"),
            ("Checks/sec", f"{self.checks_per_second:.2f}"),
            ("Creation rate", f"{self.creation_rate_per_second:.2f}/s"),
            ("Queue depth", self.queue_depth),
            ("Due backlog", self.due_backlog),
            ("Oldest overdue age", f"{self.oldest_overdue_seconds:.1f}s"),
            ("Average check duration", f"{self.average_check_duration_seconds:.3f}s"),
            ("Leases active/expired", f"{self.active_leases}/{self.expired_leases}"),
            ("Restore failures", self.restore_failures),
            ("State refresh failures", self.state_refresh_failures),
            ("Identity mismatches", self.identity_mismatches),
            ("Browser crashes", self.browser_crashes),
            ("Navigation timeouts", self.navigation_timeouts),
            ("Lease recoveries", self.lease_recoveries),
            ("Repository errors", self.repository_errors),
            (
                "Progress buckets",
                " ".join(f"{bucket}={count}" for bucket, count in self.progress_buckets) or "-",
            ),
        )
        width = max(len(label) for label, _ in rows)
        return "\n".join(f"{label:<{width}}  {value}" for label, value in rows) + "\n"


class StatusSummaryProvider:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: SessionRepository,
        browser_manager: BrowserManager,
        metrics: PrometheusMetrics,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._browser_manager = browser_manager
        self._metrics = metrics

    async def snapshot(self) -> StatusSummary:
        """Refresh aggregate gauges with bounded queries; never iterate sessions."""

        now = datetime.now(UTC)
        target = self._settings.target_queue_ids
        try:
            recovery = await self._repository.recovery_summary(now=now)
            due = await self._repository.due_session_summary(now=now)
            progress = await self._repository.progress_distribution()
        except Exception:
            # Keep /metrics serving previous values and the error counter while the
            # database is interrupted.
            self._metrics.record_repository_error("status")
            raise
        self._metrics.sync_recovery_summary(recovery, target=target)
        self._metrics.set_monitoring_backlog(
            due.count,
            oldest_overdue_seconds=due.oldest_overdue_seconds(now=now),
        )
        self._metrics.set_progress_distribution(progress)
        self._metrics.set_worker_configuration(
            monitoring_workers=self._settings.monitor_workers,
            creation_workers=self._settings.creation_workers,
        )
        counts = recovery.status_counts
        acquired = recovery.valid_queue_ids
        capacity = await self._browser_manager.capacity()
        self._metrics.set_browser_capacity(
            active_contexts=capacity.active_contexts,
            processes=capacity.browser_processes,
        )
        check_statistics = self._metrics.refresh_rate_gauges()
        metrics = self._metrics
        return StatusSummary(
            mode=self._settings.session_mode,
            target_queue_ids=self._settings.target_queue_ids,
            queue_ids_acquired=acquired,
            remaining=max(0, self._settings.target_queue_ids - acquired),
            creating=counts[QueueStatus.CREATING],
            pre_queue=counts[QueueStatus.PRE_QUEUE],
            active_queue=counts[QueueStatus.ACTIVE_QUEUE],
            parked=counts[QueueStatus.PARKED],
            serviced_soon=counts[QueueStatus.SERVICED_SOON],
            turn_started=counts[QueueStatus.TURN_STARTED],
            ready=counts[QueueStatus.READY],
            admitted=counts[QueueStatus.ADMITTED],
            expired=counts[QueueStatus.EXPIRED],
            failed=counts[QueueStatus.FAILED],
            active_contexts=capacity.active_contexts,
            chrome_processes=capacity.browser_processes,
            checks_per_second=check_statistics.checks_per_second,
            average_check_duration_seconds=(check_statistics.average_check_duration_seconds),
            lost_queue_ids=recovery.lost_queue_ids,
            connection_lost=counts[QueueStatus.CONNECTION_LOST],
            persisted_sessions=recovery.total_persisted_sessions,
            monitoring_workers=self._settings.monitor_workers,
            creation_workers=self._settings.creation_workers,
            creation_rate_per_second=_value(metrics.queue_sessions_created_per_second),
            queue_depth=int(_value(metrics.monitoring_queue_depth)),
            due_backlog=due.count,
            oldest_overdue_seconds=due.oldest_overdue_seconds(now=now),
            active_leases=recovery.leased_sessions,
            expired_leases=recovery.expired_leases,
            restore_failures=int(
                _value(metrics.transfer_restore_failures_total)
                + _value(metrics.state_restore_failures_total)
            ),
            state_refresh_failures=int(_value(metrics.state_refresh_failures_total)),
            identity_mismatches=int(_value(metrics.identity_mismatches_total)),
            browser_crashes=int(_value(metrics.browser_crashes_total)),
            navigation_timeouts=int(_value(metrics.navigation_timeouts_total)),
            lease_recoveries=int(_value(metrics.monitoring_lease_recoveries_total)),
            repository_errors=int(_labelled_total(metrics.repository_errors_total)),
            progress_buckets=tuple(
                (bucket, progress.get(bucket, 0)) for bucket in PROGRESS_BUCKETS
            ),
        )


def _value(metric: Counter | Gauge) -> float:
    """Read one unlabelled counter/gauge value from the in-process registry."""

    for family in metric.collect():
        for sample in family.samples:
            if not sample.name.endswith("_created"):
                return float(sample.value)
    return 0.0


def _labelled_total(metric: Counter) -> float:
    return sum(
        float(sample.value)
        for family in metric.collect()
        for sample in family.samples
        if sample.name.endswith("_total")
    )


class ObservabilityHttpServer:
    """Serve `/metrics` and `/status` without a web framework."""

    def __init__(
        self,
        *,
        metrics: PrometheusMetrics,
        status_provider: StatusSummaryProvider,
        host: str = "127.0.0.1",
        port: int = 9090,
    ) -> None:
        self._metrics = metrics
        self._status_provider = status_provider
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("ascii", errors="replace").split()
            path = parts[1].split("?", 1)[0] if len(parts) >= 2 else ""
            if path == "/metrics":
                try:
                    await self._status_provider.snapshot()
                except Exception as exc:  # noqa: BLE001 - serve last known values during outages
                    log_event(
                        logger,
                        logging.WARNING,
                        "status_snapshot_failed",
                        operation="metrics",
                        error_type=type(exc).__name__,
                    )
                await self._respond(writer, 200, CONTENT_TYPE_LATEST, self._metrics.render())
            elif path in {"/", "/status"}:
                summary = await self._status_provider.snapshot()
                await self._respond(
                    writer,
                    200,
                    "text/plain; charset=utf-8",
                    summary.render_text().encode(),
                )
            else:
                await self._respond(writer, 404, "text/plain; charset=utf-8", b"Not found\n")
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        reason = "OK" if status == 200 else "Not Found"
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()
