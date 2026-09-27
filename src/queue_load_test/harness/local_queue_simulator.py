"""A small local Queue-it-like page server for controlled failure scenarios.

It is **not** Queue-it. It exposes just enough page structure for the project's
own extractors (progress, pre-queue, serviced-soon, and a page-provided transfer
link) so that installed Google Chrome can exercise real contexts, navigation,
storage-state restoration, and identity verification without contacting any
staging environment. Fault sets let a scenario force deterministic failures for
specific synthetic identities.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

STAGE_PRE = "pre"
STAGE_ACTIVE = "active"
STAGE_SERVICED = "serviced"


@dataclass(slots=True)
class LocalQueueSimulator:
    """Serve synthetic queue pages; every identity is a local test value."""

    stages: dict[str, str] = field(default_factory=dict)
    progress: dict[str, int] = field(default_factory=dict)
    empty_response_ids: set[str] = field(default_factory=set)
    slow_ids: set[str] = field(default_factory=set)
    transfer_down_ids: set[str] = field(default_factory=set)
    mismatch_ids: set[str] = field(default_factory=set)
    slow_seconds: float = 3.0
    new_identity_prefix: str = "sim-new"
    requests: int = 0
    new_identities: int = 0
    _server: asyncio.Server | None = None
    port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def queue_url(self) -> str:
        return f"{self.base_url}/queue"

    @property
    def protected_url(self) -> str:
        return f"{self.base_url}/protected"

    def transfer_url(self, queue_id: str) -> str:
        return f"{self.queue_url}?q={queue_id}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        if not self._server.sockets:
            raise RuntimeError("local simulator did not bind a socket")
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            self.requests += 1
            lines = request.decode("latin-1").splitlines()
            target = lines[0].split()[1] if lines and len(lines[0].split()) > 1 else "/"
            headers = {
                key.strip().casefold(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in (line.split(":", 1),)
            }
            parsed = urlsplit(target)
            if parsed.path == "/protected":
                await self._respond(writer, 200, "<h1>Protected local destination</h1>")
                return
            if parsed.path != "/queue":
                await self._respond(writer, 404, "<h1>Not found</h1>")
                return
            query_id = parse_qs(parsed.query).get("q", [None])[0]
            cookie_id = _cookie(headers.get("cookie", ""), "queue_id")
            queue_id = query_id or cookie_id
            if queue_id is None:
                self.new_identities += 1
                queue_id = f"{self.new_identity_prefix}-{self.new_identities:05d}"
                self.stages.setdefault(queue_id, STAGE_ACTIVE)
            if queue_id in self.empty_response_ids:
                return  # close without a response: a genuine navigation failure
            if queue_id in self.slow_ids:
                await asyncio.sleep(self.slow_seconds)
            if query_id is not None and queue_id in self.transfer_down_ids:
                await self._respond(writer, 503, "<h1>Transfer temporarily unavailable</h1>")
                return
            shown_id = f"{queue_id}-other" if queue_id in self.mismatch_ids else queue_id
            body = self._html(queue_id, self.transfer_url(shown_id))
            await self._respond(
                writer,
                200,
                body,
                extra_headers=f"Set-Cookie: queue_id={queue_id}; Path=/; SameSite=Lax\r\n",
            )
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _html(self, queue_id: str, transfer_url: str) -> str:
        transfer = (
            f'<a data-testid="queue-transfer-link" href="{transfer_url}">'
            "Continue my journey on another browser or device</a>"
        )
        stage = self.stages.get(queue_id, STAGE_ACTIVE)
        if stage == STAGE_PRE:
            return (
                '<body class="before">'
                f'<main data-testid="pre-queue">Waiting to start</main>{transfer}</body>'
            )
        percentage = self.progress.get(queue_id, 40)
        active = (
            f'<div id="MainPart_divProgressbar" aria-valuenow="{percentage}" '
            f'style="width: {percentage}px; height: 10px"></div>'
            '<span id="MainPart_lbQueueNumber">local</span>'
            '<span id="MainPart_lbUsersInLineAheadOfYou">10 users ahead</span>'
            '<span id="MainPart_lbWhichIsIn">About 2 minutes</span>'
        )
        indicator = (
            '<div id="serviced-soon">You will be serviced soon</div>'
            if stage == STAGE_SERVICED
            else ""
        )
        return f"<body>{active}{indicator}{transfer}</body>"

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        body: str,
        *,
        extra_headers: str = "",
    ) -> None:
        encoded = body.encode()
        writer.write(
            (
                f"HTTP/1.1 {status} X\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(encoded)}\r\n{extra_headers}Connection: close\r\n\r\n"
            ).encode()
            + encoded
        )
        await writer.drain()


def _cookie(header: str, name: str) -> str | None:
    for item in header.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            return value
    return None
