import asyncio
import io
import json
import logging
from pathlib import Path
from typing import cast

from queue_load_test.browser import BrowserCapacity, BrowserManager
from queue_load_test.config import Settings
from queue_load_test.metrics import (
    JsonLogFormatter,
    PrometheusMetrics,
    log_event,
)
from queue_load_test.metrics.prometheus import REPOSITORY_ERROR_OPERATIONS
from queue_load_test.metrics.status import ObservabilityHttpServer, StatusSummaryProvider
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import PROGRESS_BUCKETS, SQLiteSessionRepository


def session(session_id: str, status: QueueStatus) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=f"queue-{session_id}",
        transfer_url=f"https://queue.test/journey?q=queue-{session_id}",
        mode=SessionMode.HYBRID,
        status=status,
        state_path=Path(f".browser-state/{session_id}.json"),
    )


class FakeBrowserManager:
    def report_navigation(self, context: object, *, responsive: bool) -> None:
        """Navigation health reports are irrelevant to this fake."""

    async def capacity(self) -> BrowserCapacity:
        return BrowserCapacity(
            chrome_processes=1,
            connected_processes=1,
            active_contexts=2,
            available_contexts=3,
            maximum_active_contexts=5,
            processes=(),
        )


def test_structured_logging_includes_context_without_transfer_url() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test-observability")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "queue_checked",
        session_id="session-1",
        queue_id="queue-1",
        status="ACTIVE_QUEUE",
        worker_id="worker-1",
        browser_id=0,
        attempt=2,
        restore_method="TRANSFER",
        duration=0.25,
        error_type="TimeoutError",
        total_sessions=1000,
        expired_leases=25,
        transfer_url="https://secret.test/journey?q=queue-1",
    )

    payload = json.loads(output.getvalue())
    assert payload["session_id"] == "session-1"
    assert payload["queue_id"] == "queue-1"
    assert payload["browser_id"] == 0
    assert payload["restore_method"] == "TRANSFER"
    assert payload["total_sessions"] == 1000
    assert payload["expired_leases"] == 25
    assert "transfer_url" not in payload
    assert "secret.test" not in output.getvalue()


def test_prometheus_counters_gauges_and_histograms_have_bounded_labels() -> None:
    metrics = PrometheusMetrics()
    sessions = [
        session("pre", QueueStatus.PRE_QUEUE),
        session("active", QueueStatus.ACTIVE_QUEUE),
        session("parked", QueueStatus.PARKED),
        session("soon", QueueStatus.SERVICED_SOON),
        session("ready", QueueStatus.READY),
        session("admitted", QueueStatus.ADMITTED),
        session("expired", QueueStatus.EXPIRED),
        session("failed", QueueStatus.FAILED),
    ]
    metrics.set_target(10)
    metrics.record_creation_attempt()
    metrics.record_creation_success(0.5)
    metrics.record_creation_failure(1.0)
    metrics.record_creation_duplicate()
    metrics.record_creation_transient_failure()
    metrics.record_creation_permanent_failure()
    metrics.set_creation_activity(in_flight=2, queue_depth=3)
    metrics.set_creation_rate(4.5)
    metrics.set_monitoring_activity(active_workers=2, queue_depth=4)
    metrics.set_monitoring_backlog(12, oldest_overdue_seconds=34.5)
    metrics.record_monitoring_claims(5)
    metrics.record_monitoring_lease_conflicts(1)
    metrics.record_restore(
        0.2,
        success=False,
        used_storage_state=True,
        identity_mismatch=True,
    )
    metrics.record_restore(
        0.1,
        success=False,
        used_storage_state=False,
        identity_mismatch=False,
    )
    metrics.record_navigation_timeout()
    metrics.record_navigation_failure()
    metrics.record_navigation_duration(0.5)
    metrics.record_context_creation_failure()
    metrics.record_context_acquisition_duration(0.25)
    metrics.record_browser_crash()
    metrics.set_browser_capacity(active_contexts=2, processes=1)
    metrics.record_check(
        0.25,
        QueueProgress(
            session_id="secret-session",
            progress_percentage=50,
            users_ahead=42,
        ),
    )
    metrics.sync_session_counts(sessions)

    assert metrics.registry.get_sample_value("queue_sessions_requested") == 10
    assert metrics.registry.get_sample_value("queue_sessions_created_total") == 1
    assert metrics.registry.get_sample_value("queue_ids_acquired_total") == 1
    assert metrics.registry.get_sample_value("queue_creation_attempts_total") == 1
    assert metrics.registry.get_sample_value("queue_creation_failures_total") == 1
    assert metrics.registry.get_sample_value("queue_creation_duplicates_total") == 1
    assert metrics.registry.get_sample_value("queue_creation_transient_failures_total") == 1
    assert metrics.registry.get_sample_value("queue_creation_permanent_failures_total") == 1
    assert metrics.registry.get_sample_value("queue_creation_in_flight") == 2
    assert metrics.registry.get_sample_value("queue_creation_queue_depth") == 3
    assert metrics.registry.get_sample_value("queue_sessions_created_per_second") == 4.5
    assert metrics.registry.get_sample_value("monitoring_workers_active") == 2
    assert metrics.registry.get_sample_value("monitoring_queue_depth") == 4
    assert metrics.registry.get_sample_value("monitoring_due_backlog") == 12
    assert metrics.registry.get_sample_value("monitoring_overdue_sessions") == 12
    assert metrics.registry.get_sample_value("monitoring_oldest_overdue_seconds") == 34.5
    assert metrics.registry.get_sample_value("monitoring_sessions_claimed_total") == 5
    assert metrics.registry.get_sample_value("monitoring_lease_conflicts_total") == 1
    assert metrics.registry.get_sample_value("queue_sessions_active") == 1
    assert metrics.registry.get_sample_value("queue_sessions_admitted") == 1
    assert metrics.registry.get_sample_value("browser_crashes_total") == 1
    assert metrics.registry.get_sample_value("active_browser_contexts_peak") == 2
    assert metrics.registry.get_sample_value("browser_context_creation_failures_total") == 1
    assert metrics.registry.get_sample_value("navigation_failures_total") == 1
    assert metrics.registry.get_sample_value("navigation_duration_seconds_count") == 1
    assert (
        metrics.registry.get_sample_value("browser_context_acquisition_duration_seconds_count")
        == 1
    )
    assert metrics.registry.get_sample_value("checks_total") == 1

    exposition = metrics.render().decode()
    assert "session_restore_duration_seconds" in exposition
    assert "queue_check_duration_seconds" in exposition
    assert "queue-progress" not in exposition
    assert "secret-session" not in exposition
    assert "queue_id=" not in exposition
    allowed_values = {
        "bucket": set(PROGRESS_BUCKETS),
        "operation": set(REPOSITORY_ERROR_OPERATIONS),
    }
    for family in metrics.registry.collect():
        for sample in family.samples:
            assert set(sample.labels) <= {"le", *allowed_values}
            for name, value in sample.labels.items():
                if name in allowed_values:
                    assert value in allowed_values[name]


async def test_status_summary_and_http_endpoints_are_readable(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    await repository.create(session("active", QueueStatus.ACTIVE_QUEUE))
    await repository.create(session("turn", QueueStatus.TURN_STARTED))
    await repository.create(session("failed", QueueStatus.FAILED))
    settings = Settings(STAGING_URL="https://staging.example.test", TARGET_QUEUE_IDS=10)
    metrics = PrometheusMetrics()
    provider = StatusSummaryProvider(
        settings=settings,
        repository=repository,
        browser_manager=cast(BrowserManager, FakeBrowserManager()),
        metrics=metrics,
    )

    summary = await provider.snapshot()
    text = summary.render_text()
    assert "Mode" in text and "HYBRID" in text
    assert "Target Queue IDs" in text and "10" in text
    assert "Active queue" in text and "1" in text
    assert "Turn started" in text
    assert "Active contexts" in text and "2" in text

    server = ObservabilityHttpServer(
        metrics=metrics,
        status_provider=provider,
        port=0,
    )
    await server.start()
    try:
        status_response = await _http_get(server.port, "/status")
        metrics_response = await _http_get(server.port, "/metrics")
    finally:
        await server.close()

    assert "HTTP/1.1 200 OK" in status_response
    assert "Queue IDs acquired" in status_response
    assert "queue_sessions_active 1.0" in metrics_response
    assert "queue_sessions_requested 10.0" in metrics_response
    await repository.close()


async def _http_get(port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response.decode(errors="replace")
