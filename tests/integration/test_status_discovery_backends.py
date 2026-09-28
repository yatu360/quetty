from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from queue_load_test.browser import BrowserManager, create_browser_backend
from queue_load_test.models import BrowserBackendName
from queue_load_test.status_discovery import DiscoveryDomSnapshot, StatusDiscoveryFactory


@dataclass(slots=True)
class DiscoveryFixtureServer:
    server: asyncio.Server | None = None
    port: int = 0
    pulses: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/visitor"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self.server.sockets
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            lines = raw_headers.decode("latin-1").splitlines()
            request_target = lines[0].split()[1]
            headers = {
                key.casefold(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in (line.split(":", 1),)
            }
            length = int(headers.get("content-length", "0"))
            if length:
                await reader.readexactly(length)
            if request_target == "/visitor":
                body = b"""<!doctype html><body>
                  <div id="progress">0</div>
                  <script>
                    async function pulse(n) {
                      const response = await fetch('/runtime-pulse?nonce=' + n, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({queueId: 'local-queue', nonce: n})
                      });
                      const data = await response.json();
                      document.getElementById('progress').textContent = data.progressPercentage;
                    }
                    (async () => {
                      await pulse('one');
                      await new Promise(resolve => setTimeout(resolve, 40));
                      await pulse('two');
                      window.discoveryDone = true;
                    })();
                  </script>
                </body>"""
                await self._respond(writer, body, content_type="text/html")
            elif request_target.startswith("/runtime-pulse?"):
                self.pulses += 1
                body = json.dumps(
                    {
                        "status": "active",
                        "progressPercentage": 42,
                        "usersAhead": 7,
                    }
                ).encode()
                await self._respond(
                    writer,
                    body,
                    content_type="application/json",
                    extra=f"Set-Cookie: local_version={self.pulses}; Path=/; SameSite=Lax\r\n",
                )
            else:
                await self._respond(writer, b"not found", status=404, content_type="text/plain")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        body: bytes,
        *,
        content_type: str,
        status: int = 200,
        extra: str = "",
    ) -> None:
        writer.write(
            (
                f"HTTP/1.1 {status} OK\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n{extra}Connection: close\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()


@pytest.mark.parametrize(
    "backend_name",
    [BrowserBackendName.CHROME, BrowserBackendName.PATCHRIGHT],
)
async def test_browser_observation_seam_captures_local_fetches_on_supported_backends(
    tmp_path: Path,
    backend_name: BrowserBackendName,
) -> None:
    fixture = DiscoveryFixtureServer()
    await fixture.start()
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        headless=True,
        backend=create_browser_backend(backend_name),
    )
    await manager.start()
    try:
        async with manager.context() as context:
            page = await context.new_page()

            async def snapshot() -> DiscoveryDomSnapshot:
                return DiscoveryDomSnapshot(
                    observed_at="2026-09-29T12:00:00+00:00",
                    page_url=page.url,
                    lifecycle_status="ACTIVE_QUEUE",
                    progress_percentage=float(await page.locator("#progress").text_content()),
                    users_ahead=7,
                )

            observation = StatusDiscoveryFactory(
                evidence_directory=tmp_path / backend_name.value,
                scope="local_simulator",
            ).create(
                page=page,
                context=context,
                session_id=f"local-{backend_name.value}",
                expected_queue_id="local-queue",
                dom_snapshot_provider=snapshot,
            )
            async with observation:
                await page.goto(fixture.url, wait_until="domcontentloaded")
                await page.wait_for_function("window.discoveryDone === true")
        assert manager.active_context_count == 0
    finally:
        await manager.shutdown()
        await fixture.close()

    artifact_path = next((tmp_path / backend_name.value).glob("*.json"))
    artifact = json.loads(artifact_path.read_text())
    pulses = [
        item for item in artifact["exchanges"] if item["path"] == "/runtime-pulse"
    ]
    assert len(pulses) == 2
    assert all(item["method"] == "POST" for item in pulses)
    assert [item["query"] for item in pulses] == [
        [["nonce", "one"]],
        [["nonce", "two"]],
    ]
    assert [item["request_json"]["nonce"] for item in pulses] == ["one", "two"]
    assert all(item["response_status"] == 200 for item in pulses)
    assert all(item["response_json"]["progressPercentage"] == 42 for item in pulses)
    assert all(item["set_cookie"] for item in pulses)
    assert all(item["classification"] == "dom_correlated_status_candidate" for item in pulses)
    assert fixture.pulses == 2
    assert manager.managed_process_count == 0
