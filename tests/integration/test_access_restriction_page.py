"""Rendered-DOM evidence for the pre-Queue access-restriction detector.

Uses only ``set_content`` documents and an HTTP server bound to 127.0.0.1; it never
contacts Queue-it or any staging environment.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueStatus, SessionMode
from queue_load_test.queue_monitor import RenderedAccessRestrictionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    AcquisitionFailure,
    CreationOutcomeKind,
    CreationRetryPolicy,
    CreationWorkItem,
    QueueSessionCreator,
)
from queue_load_test.state import FileSystemStateStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "queue_it"
RESTRICTED_FIXTURE = (FIXTURES / "access_restricted.html").read_text(encoding="utf-8")


async def test_javascript_rendered_restriction_page_is_detected() -> None:
    detector = RenderedAccessRestrictionDetector()
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content(RESTRICTED_FIXTURE)

        assert await detector.detect(page) is True


async def test_queue_it_fixtures_and_hidden_text_are_not_restricted() -> None:
    detector = RenderedAccessRestrictionDetector()
    hidden = (
        "<!doctype html><body><p>Queue page</p>"
        '<div style="display:none">We are sorry, your access has been restricted</div></body>'
    )
    async with BrowserManager() as manager:
        for fixture in sorted(FIXTURES.glob("*.html")):
            if fixture.name == "access_restricted.html":
                continue
            async with manager.context() as context:
                page = await context.new_page()
                await page.set_content(fixture.read_text(encoding="utf-8"))
                assert await detector.detect(page) is False, fixture.name
        async with manager.context() as context:
            page = await context.new_page()
            await page.set_content(hidden)
            assert await detector.detect(page) is False


class _RestrictedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = RESTRICTED_FIXTURE.encode()
        self.send_response(403)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


@contextmanager
def _restricted_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RestrictedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_creator_discards_restricted_attempt_in_a_real_browser(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "restricted.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    with _restricted_server() as url:
        async with BrowserManager() as manager:
            creator = QueueSessionCreator(
                browser_manager=manager,
                repository=repository,
                state_store=state_store,
                staging_url=url,
                state_directory=state_store.directory,
                mode=SessionMode.HYBRID,
                retry_policy=CreationRetryPolicy(max_attempts=3),
                navigation_timeout_ms=10_000,
                live_page_timeout_seconds=5,
            )

            outcome = await creator.create(CreationWorkItem(sequence=1, session_id="r"))

            assert manager.active_context_count == 0

    assert outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE
    assert outcome.failure_code == AcquisitionFailure.ACCESS_RESTRICTED_BEFORE_QUEUE.code
    assert outcome.attempts == 1
    persisted = await repository.get("r")
    assert persisted is not None
    assert persisted.status is QueueStatus.FAILED
    assert persisted.queue_id is None
    assert await repository.count_successful_queue_ids() == 0
    assert await state_store.load("r") is None
    await repository.close()
