"""Real-browser acceptance for acquisition-time access-restriction handling.

Uses real Chrome contexts, the production ``QueueSessionCreator``, the bounded
``SessionCreationController``, SQLite, and the filesystem state store, against an HTTP
server bound to 127.0.0.1 that serves a scripted sequence of restriction and synthetic
queue pages. It never contacts Queue-it. The synthetic queue page exposes its ID in
``#qid`` and is read by small local extractors; Queue-it parsing is covered elsewhere.
"""

import asyncio
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    AcquisitionFailure,
    CreationRetryPolicy,
    QueueSessionCreator,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import TransferExtractionResult

FIXTURES = Path(__file__).parents[1] / "fixtures" / "queue_it"
RESTRICTED_HTML = (FIXTURES / "access_restricted.html").read_text(encoding="utf-8")
CODE = AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.code


class _ScriptedServer:
    """Serves the scripted responses in order, then restriction pages forever."""

    def __init__(self, script: list[str]) -> None:
        self.script = deque(script)
        self.requests = 0
        self.restricted_served = 0
        self._lock = threading.Lock()

    def next_response(self) -> tuple[int, bytes]:
        with self._lock:
            self.requests += 1
            kind = self.script.popleft() if self.script else "R"
            if kind == "R":
                self.restricted_served += 1
                return 403, RESTRICTED_HTML.encode()
            body = (
                "<!doctype html><title>Queue</title>"
                f'<body><p>You are now in line.</p><div id="qid">queue-{self.requests}</div>'
                "</body>"
            )
            return 200, body.encode()


@contextmanager
def _serve(script: list[str]) -> Iterator[tuple[str, _ScriptedServer]]:
    scripted = _ScriptedServer(script)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/":
                # Browser side requests (favicon) must not consume the script.
                status, body = 404, b""
            else:
                status, body = scripted.next_response()
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", scripted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _SyntheticLiveExtractor:
    async def extract(self, page: Page, *, session_id: str) -> QueueProgress:
        active = await page.locator("#qid").count() > 0
        return QueueProgress(session_id=session_id, active_queue=active)


class _SyntheticTransferExtractor:
    async def extract(
        self, page: Page, *, expected_queue_id: str | None = None
    ) -> TransferExtractionResult:
        del expected_queue_id
        queue_id = await page.locator("#qid").inner_text()
        return TransferExtractionResult(
            transfer_url=f"https://queue.example.test/journey?q={queue_id}",
            observed_queue_id=queue_id,
        )


def _creator(
    manager: BrowserManager,
    repository: SQLiteSessionRepository,
    state_store: FileSystemStateStore,
    url: str,
) -> QueueSessionCreator:
    return QueueSessionCreator(
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
        staging_url=url,
        state_directory=state_store.directory,
        mode=SessionMode.HYBRID,
        live_extractor=_SyntheticLiveExtractor(),  # type: ignore[arg-type]
        transfer_extractor_factory=lambda _: _SyntheticTransferExtractor(),
        retry_policy=CreationRetryPolicy(max_attempts=1),
        navigation_timeout_ms=10_000,
        live_page_timeout_seconds=5,
        observation_interval_seconds=0.05,
    )


def _state_names(state_store: FileSystemStateStore) -> set[str]:
    if not state_store.directory.exists():
        return set()
    return {path.name for path in state_store.directory.iterdir()}


async def test_real_browser_sequence_reaches_exact_target(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "sequence.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    with _serve(["R", "R", "S", "R", "S"]) as (url, server):
        async with BrowserManager() as manager:
            controller = SessionCreationController(
                repository=repository,
                handler=_creator(manager, repository, state_store, url),
                target_queue_ids=2,
                worker_count=2,
                queue_capacity=2,
            )

            metrics = await asyncio.wait_for(controller.run(), timeout=60)

            assert manager.active_context_count == 0

    assert metrics.successful_unique_ids == 2
    assert metrics.access_restricted == 3
    assert server.requests == 5
    assert await repository.count_successful_queue_ids() == 2
    failed = await repository.list(QueueStatus.FAILED)
    assert len(failed) == 3
    assert all(row.queue_id is None and row.last_error == CODE for row in failed)
    parked = await repository.list(QueueStatus.PARKED)
    assert _state_names(state_store) == {f"{row.session_id}.json" for row in parked}
    await repository.close()


async def test_shutdown_after_repeated_restrictions_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "shutdown.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    seeded: list[QueueSession] = []
    for index in range(2):
        session = await repository.create(
            QueueSession(
                session_id=f"existing-{index}",
                queue_id=f"existing-queue-{index}",
                transfer_url=f"https://queue.example.test/journey?q=existing-queue-{index}",
                mode=SessionMode.HYBRID,
                status=QueueStatus.PARKED,
                state_path=state_store.path_for(f"existing-{index}"),
            )
        )
        await state_store.save(session.session_id, {"cookies": [], "origins": []})
        seeded.append(session)
    rows_before = {s.session_id: await repository.get(s.session_id) for s in seeded}
    state_before = {
        path.name: path.read_bytes() for path in state_store.directory.iterdir()
    }
    stop_event = asyncio.Event()

    with _serve([]) as (url, server):
        manager = BrowserManager()
        await manager.start()
        try:
            controller = SessionCreationController(
                repository=repository,
                handler=_creator(manager, repository, state_store, url),
                target_queue_ids=10,
                worker_count=3,
                queue_capacity=3,
            )
            run_task = asyncio.create_task(controller.run(stop_event))
            while server.restricted_served < 6:
                assert not run_task.done()
                await asyncio.sleep(0.05)
            stop_event.set()
            metrics = await asyncio.wait_for(run_task, timeout=30)
            assert manager.active_context_count == 0
            assert metrics.currently_creating == 0
            assert metrics.maximum_concurrent_creating <= 3
        finally:
            await manager.shutdown()

    assert metrics.access_restricted >= 6
    assert metrics.successful_unique_ids == 2
    rows = await repository.list()
    now = datetime.now(UTC)
    assert all(row.worker_id is None and row.lease_until is None for row in rows)
    claimed = await repository.claim_due_sessions(
        worker_id="acceptance-probe",
        now=now + timedelta(days=1),
        lease_until=now + timedelta(days=2),
        limit=100,
    )
    assert {row.session_id for row in claimed} <= {s.session_id for s in seeded}
    for session_id in {row.session_id for row in claimed}:
        await repository.release_lease(session_id, worker_id="acceptance-probe")
    for session in seeded:
        assert await repository.get(session.session_id) == rows_before[session.session_id]
    # Only the seeded state remains: no restricted-attempt or temporary files.
    assert _state_names(state_store) == set(state_before)
    for name, content in state_before.items():
        assert (state_store.directory / name).read_bytes() == content
    failed: list[Any] = await repository.list(QueueStatus.FAILED)
    assert len(failed) == metrics.access_restricted
    assert all(row.queue_id is None and row.last_error == CODE for row in failed)
    await repository.close()
