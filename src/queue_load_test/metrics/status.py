"""Human-readable Phase 1 status and a tiny asyncio HTTP endpoint."""

import asyncio
from dataclasses import dataclass

from prometheus_client import CONTENT_TYPE_LATEST

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.metrics.prometheus import PrometheusMetrics
from queue_load_test.models import QueueStatus, SessionMode
from queue_load_test.repository import SessionRepository


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
            ("Active contexts", self.active_contexts),
            ("Chrome processes", self.chrome_processes),
            ("Checks/sec", f"{self.checks_per_second:.2f}"),
            ("Average check duration", f"{self.average_check_duration_seconds:.3f}s"),
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
        sessions = await self._repository.list()
        self._metrics.set_target(self._settings.target_queue_ids)
        self._metrics.sync_session_counts(sessions)
        counts = {status: 0 for status in QueueStatus}
        for session in sessions:
            counts[session.status] += 1
        acquired = await self._repository.count_successful_queue_ids()
        capacity = await self._browser_manager.capacity()
        self._metrics.set_browser_capacity(
            active_contexts=capacity.active_contexts,
            processes=capacity.chrome_processes,
        )
        check_statistics = self._metrics.check_statistics()
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
            chrome_processes=capacity.chrome_processes,
            checks_per_second=check_statistics.checks_per_second,
            average_check_duration_seconds=(check_statistics.average_check_duration_seconds),
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
                await self._status_provider.snapshot()
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
