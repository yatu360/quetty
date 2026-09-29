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
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

STAGE_PRE = "pre"
STAGE_ACTIVE = "active"
STAGE_SERVICED = "serviced"

# Deterministic faults for the optional visitor-status endpoint (``status_enabled``).
STATUS_FAULTS = frozenset(
    {
        "http_500",
        "slow",
        "redirect",
        "html",
        "malformed",
        "mismatch",
        "missing_id",
        "rejected",
        "contradictory",
        "unknown_lifecycle",
        "admitted",
        "unknown_field",
        "wrong_type",
    }
)


@dataclass(slots=True)
class LocalQueueSimulator:
    """Serve synthetic queue pages; every identity is a local test value."""

    stages: dict[str, str] = field(default_factory=dict)
    progress: dict[str, int] = field(default_factory=dict)
    empty_response_ids: set[str] = field(default_factory=set)
    slow_ids: set[str] = field(default_factory=set)
    transfer_down_ids: set[str] = field(default_factory=set)
    mismatch_ids: set[str] = field(default_factory=set)
    forced_new_ids: list[str] = field(default_factory=list)
    slow_seconds: float = 3.0
    new_identity_prefix: str = "sim-new"
    requests: int = 0
    new_identities: int = 0
    # Optional page-driven JSON visitor-status polling for Direct Monitoring tests.
    status_enabled: bool = False
    status_poll_ms: int = 400
    status_faults: dict[str, str] = field(default_factory=dict)
    # Recognizable fake secret seeded into the visitor cookie, the page-built status
    # URL, the status response body, and a status Set-Cookie (secret-leak tests).
    secret_token: str | None = None
    # Stage presented to newly created identities (lifecycle only moves forward).
    initial_stage: str = STAGE_ACTIVE
    # "classic": a visible transfer link. "modal": the current Queue-it layout seen on
    # Glastonbury 2025 and the Queue-it demo (closed transfer dialog whose link is
    # element text, a hidden footer Queue ID, and ``#expectedServiceTime``).
    layout: str = "classic"
    # Identities whose footer Queue ID contradicts the dialog link (fail-closed tests).
    crosscheck_conflict_ids: set[str] = field(default_factory=set)
    # Identities whose turn has come: their queue page redirects to the protected site.
    admitted_ids: set[str] = field(default_factory=set)
    # Optional response-provided polling guidance (``pollAfterSeconds``).
    poll_after_seconds: float | None = None
    direct_request_times: dict[str, list[float]] = field(default_factory=dict)
    page_requests: Counter[str] = field(default_factory=Counter)
    browser_status_requests: Counter[str] = field(default_factory=Counter)
    direct_status_requests: Counter[str] = field(default_factory=Counter)
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

    @property
    def entry_url(self) -> str:
        """A protected-site entry that redirects un-admitted visitors into the queue.

        This mirrors how an operator's target URL behaves: the target is the protected
        destination, and reaching it means admission. Use it as a run target; using the
        queue page itself would make every observation look admitted.
        """

        return f"{self.base_url}/entry"

    def transfer_url(self, queue_id: str) -> str:
        return f"{self.queue_url}?q={queue_id}"

    @property
    def status_url(self) -> str:
        return f"{self.base_url}/status"

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
            if parsed.path == "/entry":
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: /queue\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if parsed.path == "/protected":
                await self._respond(writer, 200, "<h1>Protected local destination</h1>")
                return
            if parsed.path == "/status" and self.status_enabled:
                await self._status(writer, parsed.query, headers)
                return
            if parsed.path != "/queue":
                await self._respond(writer, 404, "<h1>Not found</h1>")
                return
            query_id = parse_qs(parsed.query).get("q", [None])[0]
            cookie_id = _cookie(headers.get("cookie", ""), "queue_id")
            queue_id = query_id or cookie_id
            if queue_id is None:
                self.new_identities += 1
                queue_id = (
                    self.forced_new_ids.pop(0)
                    if self.forced_new_ids
                    else f"{self.new_identity_prefix}-{self.new_identities:05d}"
                )
                self.stages.setdefault(queue_id, self.initial_stage)
            if queue_id in self.admitted_ids:
                writer.write(
                    b"HTTP/1.1 302 Found\r\nLocation: /protected\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if queue_id in self.empty_response_ids:
                return  # close without a response: a genuine navigation failure
            if queue_id in self.slow_ids:
                await asyncio.sleep(self.slow_seconds)
            if query_id is not None and queue_id in self.transfer_down_ids:
                await self._respond(writer, 503, "<h1>Transfer temporarily unavailable</h1>")
                return
            self.page_requests[queue_id] += 1
            shown_id = f"{queue_id}-other" if queue_id in self.mismatch_ids else queue_id
            body = self._html(queue_id, self.transfer_url(shown_id))
            cookies = f"Set-Cookie: queue_id={queue_id}; Path=/; SameSite=Lax\r\n"
            if self.secret_token is not None:
                cookies += (
                    f"Set-Cookie: visitor_secret={self.secret_token}; Path=/; SameSite=Lax\r\n"
                )
            await self._respond(writer, 200, body, extra_headers=cookies)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _status(
        self,
        writer: asyncio.StreamWriter,
        query: str,
        headers: dict[str, str],
    ) -> None:
        """JSON status polled by the page itself; replays arrive without Sec-Fetch."""

        queue_id = parse_qs(query).get("q", [""])[0]
        browser = "sec-fetch-mode" in headers
        (self.browser_status_requests if browser else self.direct_status_requests)[
            queue_id
        ] += 1
        if not browser:
            self.direct_request_times.setdefault(queue_id, []).append(time.monotonic())
        if _cookie(headers.get("cookie", ""), "queue_id") != queue_id:
            await self._respond(writer, 403, "rejected", content_type="text/plain")
            return
        fault = self.status_faults.get(queue_id) if not browser else None
        if fault == "slow":
            await asyncio.sleep(self.slow_seconds)
        if fault == "http_500":
            await self._respond(writer, 500, "unavailable", content_type="text/plain")
            return
        if fault == "rejected":
            await self._respond(writer, 403, "rejected", content_type="text/plain")
            return
        if fault == "redirect":
            writer.write(
                b"HTTP/1.1 302 Found\r\nLocation: /queue\r\nContent-Length: 0\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            return
        if fault == "html":
            await self._respond(writer, 200, "<h1>not json</h1>")
            return
        if fault == "malformed":
            await self._respond(writer, 200, "{not json", content_type="application/json")
            return
        document = self.status_document(queue_id, fault)
        extra = ""
        if self.secret_token is not None:
            document["sessionToken"] = self.secret_token
            extra = f"Set-Cookie: rotation={self.secret_token}-rot; Path=/; SameSite=Lax\r\n"
        await self._respond(
            writer,
            200,
            json.dumps(document),
            content_type="application/json",
            extra_headers=extra,
        )

    def status_document(self, queue_id: str, fault: str | None = None) -> dict[str, object]:
        stage = self.stages.get(queue_id, STAGE_ACTIVE)
        document: dict[str, object] = {
            "queueId": f"{queue_id}-other" if fault == "mismatch" else queue_id,
            "preQueue": stage == STAGE_PRE,
            "activeQueue": stage != STAGE_PRE,
            "servicedSoon": stage == STAGE_SERVICED,
            "progress": self.progress.get(queue_id, 40) if stage != STAGE_PRE else None,
            "usersAhead": 10 if stage != STAGE_PRE else None,
        }
        if self.poll_after_seconds is not None:
            document["pollAfterSeconds"] = self.poll_after_seconds
        if fault == "missing_id":
            document.pop("queueId")
        elif fault == "contradictory":
            document.update(preQueue=True, activeQueue=True)
        elif fault == "unknown_lifecycle":
            document.update(
                preQueue=False, activeQueue=False, servicedSoon=False, usersAhead=None
            )
        elif fault == "admitted":
            document["redirectUrl"] = self.protected_url
        elif fault == "unknown_field":
            document["addedLater"] = {"value": 1}
        elif fault == "wrong_type":
            document["usersAhead"] = "ten"
        return document

    @staticmethod
    def status_schema(scope: str = "local_simulator") -> dict[str, object]:
        """The reviewed-schema document describing this simulator's status JSON."""

        return {
            "schema_version": 1,
            "source_scope": scope,
            "fields": {
                "queue_id": ["queueId"],
                "pre_queue": ["preQueue"],
                "active_queue": ["activeQueue"],
                "serviced_soon": ["servicedSoon"],
                "progress_percentage": ["progress"],
                "users_ahead": ["usersAhead"],
                "redirect_url": ["redirectUrl"],
                "poll_after_seconds": ["pollAfterSeconds"],
            },
        }

    def _status_script(self, queue_id: str) -> str:
        if not self.status_enabled:
            return ""
        token = (
            "&token=" + self.secret_token if self.secret_token is not None else ""
        )
        return (
            "<script>(function(){const q="
            + json.dumps(queue_id)
            + ";function poll(){fetch('/status?q='+encodeURIComponent(q)+"
            + json.dumps(token)
            + ","
            "{headers:{'Accept':'application/json'}}).catch(function(){});}"
            f"poll();setInterval(poll,{self.status_poll_ms});}})();</script>"
        )

    def _modal_transfer(self, queue_id: str, transfer_url: str) -> str:
        shown = parse_qs(urlsplit(transfer_url).query).get("q", [queue_id])[0]
        footer = f"{shown}-conflict" if queue_id in self.crosscheck_conflict_ids else shown
        # The real page wraps the long link across lines inside the element text.
        wrapped = transfer_url.replace("?", "?\n  ").replace("&", "&amp;\n  ")
        return (
            '<div id="footer-direct-link" style="display: none">'
            f'<span>Queue ID: </span><span id="hlLinkToQueueTicket2">{footer}</span></div>'
            '<div id="queueIdLinkModal" role="dialog" class="modal" style="display: none">'
            '<h2 id="queueIdLinkModalLabel">'
            "Continue my journey on another browser or device</h2>"
            '<p id="queueIdLinkModalDescription">To transfer your spot in line to another '
            "browser or device, copy your unique link below.</p>"
            f'<p><span id="queueIdLinkURL">{wrapped}</span>'
            '<button id="copyToClipboardButton" type="button">Copy my link</button></p>'
            "</div>"
        )

    def _html(self, queue_id: str, transfer_url: str) -> str:
        if self.layout == "modal":
            transfer = self._modal_transfer(queue_id, transfer_url)
        else:
            transfer = (
                f'<a data-testid="queue-transfer-link" href="{transfer_url}">'
                "Continue my journey on another browser or device</a>"
            )
        transfer += self._status_script(queue_id)
        stage = self.stages.get(queue_id, STAGE_ACTIVE)
        if stage == STAGE_PRE:
            return (
                '<body class="before">'
                f'<main data-testid="pre-queue">Waiting to start</main>{transfer}</body>'
            )
        percentage = self.progress.get(queue_id, 40)
        if self.layout == "modal":
            # Demo-style: queue number and users ahead hidden, expected arrival shown
            # in ``#expectedServiceTime`` while the classic element stays hidden.
            active = (
                f'<div id="MainPart_divProgressbar" aria-valuenow="{percentage}" '
                f'style="width: {percentage}px; height: 10px"></div>'
                '<span id="MainPart_lbQueueNumber" style="display: none"></span>'
                '<span id="MainPart_lbUsersInLineAheadOfYou" style="display: none">NaN</span>'
                '<span id="MainPart_lbExpectedServiceTime" style="display: none">'
                "2:45 PM</span>"
                '<span id="expectedServiceTime">2:45 PM</span>'
                '<span id="MainPart_lbWhichIsIn">less than a minute</span>'
            )
        else:
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
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        encoded = body.encode()
        writer.write(
            (
                f"HTTP/1.1 {status} X\r\nContent-Type: {content_type}\r\n"
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
