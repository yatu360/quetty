import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from queue_load_test.browser import BrowserCapacityError, BrowserManager
from queue_load_test.models import QueueSession, QueueStatus, SessionMode, evaluate_queue_status
from queue_load_test.queue_monitor import AdmissionDetector, QueueItLiveStateExtractor
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueItTransferExtractor


@dataclass(slots=True)
class ControlledQueueServer:
    server: asyncio.Server | None = None
    port: int = 0
    next_identity: int = 1
    stages: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)

    @property
    def queue_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/queue"

    @property
    def protected_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/protected"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self.server.sockets
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    def set_stage(self, queue_id: str, stage: str) -> None:
        self.stages[queue_id] = stage
        self.revisions[queue_id] = self.revisions.get(queue_id, 0) + 1

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            lines = request.decode("latin-1").splitlines()
            path = lines[0].split()[1]
            headers = {
                key.strip().casefold(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in (line.split(":", 1),)
            }
            parsed = urlsplit(path)
            if parsed.path == "/protected":
                await self._respond(writer, 200, "<h1>Protected staging destination</h1>")
                return
            query_id = parse_qs(parsed.query).get("q", [None])[0]
            cookie_id = self._cookie(headers.get("cookie", ""), "queue_id")
            queue_id = query_id or cookie_id
            if queue_id is None:
                queue_id = f"controlled-{self.next_identity:02d}"
                self.next_identity += 1
            stage = self.stages.setdefault(queue_id, "pre")
            if stage == "admitted":
                await self._redirect(writer, "/protected")
                return
            transfer_url = f"{self.queue_url}?q={queue_id}"
            body = self._html(queue_id, transfer_url, stage)
            await self._respond(
                writer,
                200,
                body,
                extra_headers=(f"Set-Cookie: queue_id={queue_id}; Path=/; SameSite=Lax\r\n"),
            )
        except asyncio.IncompleteReadError:
            writer.close()

    def _html(self, queue_id: str, transfer_url: str, stage: str) -> str:
        transfer = (
            f'<a data-testid="queue-transfer-link" href="{transfer_url}">'
            "Continue my journey on another browser or device</a>"
        )
        if stage == "pre":
            return (
                f'<body class="before" data-queue-id="{queue_id}">'
                f'<main data-testid="pre-queue">Waiting to start</main>{transfer}</body>'
            )
        revision = self.revisions.get(queue_id, 0)
        active = (
            '<div id="MainPart_divProgressbar" aria-valuenow="42" '
            'style="width: 42px; height: 10px"></div>'
            f'<span id="MainPart_lbQueueNumber">{queue_id}</span>'
            '<span id="MainPart_lbUsersInLineAheadOfYou">10 users ahead</span>'
            '<span id="MainPart_lbWhichIsIn">About 2 minutes</span>'
            f'<span id="MainPart_lbLastUpdateTimeText">2999-09-26 14:{18 + revision:02d}</span>'
        )
        indicator = {
            "active": "",
            "serviced": '<div id="serviced-soon">You will be serviced soon</div>',
            "turn": '<div data-testid="turn-started">Redirecting</div>',
        }[stage]
        return f"<body>{active}{indicator}{transfer}</body>"

    @staticmethod
    def _cookie(header: str, name: str) -> str | None:
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                return value
        return None

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
                f"HTTP/1.1 {status} OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(encoded)}\r\n{extra_headers}Connection: close\r\n\r\n"
            ).encode()
            + encoded
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    @staticmethod
    async def _redirect(writer: asyncio.StreamWriter, location: str) -> None:
        writer.write(
            (
                f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()


async def test_controlled_ten_session_hybrid_journey(tmp_path: Path) -> None:
    server = ControlledQueueServer()
    await server.start()
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=5,
        max_active_contexts=5,
    )
    state_store = FileSystemStateStore(tmp_path / "state")
    repository = SQLiteSessionRepository(tmp_path / "phase1.sqlite3")
    live_extractor = QueueItLiveStateExtractor()
    identities: list[str] = []
    transfer_urls: list[str] = []
    try:
        await manager.start()
        capacity_probe = [await manager.create_context() for _ in range(5)]
        assert (await manager.capacity()).active_contexts == 5
        with pytest.raises(BrowserCapacityError):
            await manager.create_context()
        await asyncio.gather(*(context.close() for context in capacity_probe))

        for index in range(10):
            async with manager.context() as context:
                page = await context.new_page()
                await page.goto(server.queue_url)
                progress = await live_extractor.extract(page, session_id=f"session-{index}")
                transfer = await QueueItTransferExtractor(server.queue_url).extract(page)
                assert progress.pre_queue is True
                assert transfer.queue_id is not None
                assert transfer.transfer_url is not None
                identities.append(transfer.queue_id)
                transfer_urls.append(transfer.transfer_url)
                state_path = await state_store.save(
                    f"session-{index}",
                    await context.storage_state(),
                )
                await repository.create(
                    QueueSession(
                        session_id=f"session-{index}",
                        queue_id=transfer.queue_id,
                        transfer_url=transfer.transfer_url,
                        mode=SessionMode.HYBRID,
                        status=QueueStatus.PARKED,
                        state_path=state_path,
                        created_at=datetime.now(UTC),
                    ),
                    progress,
                )

        assert len(identities) == len(set(identities)) == 10
        capacity = await manager.capacity()
        assert capacity.chrome_processes == 1
        assert capacity.active_contexts == 0
        assert capacity.maximum_active_contexts == 5

        queue_id = identities[0]
        server.set_stage(queue_id, "active")
        async with manager.context() as context:
            page = await context.new_page()
            await page.goto(transfer_urls[0])
            transfer = await QueueItTransferExtractor(server.queue_url).extract(
                page, expected_queue_id=queue_id
            )
            active = await live_extractor.extract(page, session_id="transfer-restore")
        assert transfer.identity_mismatch is False
        assert transfer.observed_queue_id == queue_id
        assert active.active_queue is True
        assert active.progress_percentage == 42
        first_update = active.last_updated_at

        state = await state_store.load("session-0")
        assert state is not None
        async with manager.context(storage_state=state) as context:
            page = await context.new_page()
            await page.goto(server.queue_url)
            restored = await QueueItTransferExtractor(server.queue_url).extract(
                page, expected_queue_id=queue_id
            )
        assert restored.observed_queue_id == queue_id
        assert restored.identity_mismatch is False

        server.set_stage(queue_id, "active")
        async with manager.context() as context:
            page = await context.new_page()
            await page.goto(transfer_urls[0])
            updated = await live_extractor.extract(page, session_id="updated")
        assert updated.last_updated_at != first_update

        server.set_stage(queue_id, "serviced")
        async with manager.context() as context:
            page = await context.new_page()
            await page.goto(transfer_urls[0])
            serviced = await live_extractor.extract(page, session_id="serviced")
        assert evaluate_queue_status(serviced) is QueueStatus.SERVICED_SOON

        server.set_stage(queue_id, "turn")
        async with manager.context() as context:
            page = await context.new_page()
            await page.goto(transfer_urls[0])
            turn = await live_extractor.extract(page, session_id="turn")
        assert evaluate_queue_status(turn) is QueueStatus.TURN_STARTED

        server.set_stage(queue_id, "admitted")
        async with manager.context() as context:
            page = await context.new_page()
            await page.goto(transfer_urls[0])
            admitted = await AdmissionDetector.from_urls(server.protected_url).detect(page)
        assert admitted is True

        await repository.close()
        restarted = SQLiteSessionRepository(tmp_path / "phase1.sqlite3")
        try:
            assert await restarted.count_successful_queue_ids() == 10
            recovered = await restarted.get("session-0")
            assert recovered is not None
            assert recovered.queue_id == queue_id
        finally:
            await restarted.close()
    finally:
        await manager.shutdown()
        await repository.close()
        await server.close()
