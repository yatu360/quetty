"""Copy URL works in a real browser across repeated HTMX table replacements.

The page, partials, static files, and transfer-URL endpoint come from the real app
(via TestClient); only the polling interval is shortened and the Clipboard API is
replaced with a recorder so the copied text can be asserted.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.models import QueueSession, QueueStatus, RunConfig, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web import create_app
from queue_load_test.web.actions import OperatorAction
from queue_load_test.web.service import RuntimeCapacity

ORIGIN = "http://dashboard.test"
# Synthetic only; never a real Queue-it identity.
TRANSFER_URL = "https://queue.synthetic.invalid/?c=fake&e=copy-url&q=fake-queue-0001&t=fake-token"

CLIPBOARD_RECORDER = """
window.__copied = [];
window.__clipboardFails = false;
Object.defineProperty(navigator, "clipboard", {
  configurable: true,
  value: {
    writeText: function (text) {
      if (window.__clipboardFails) {
        return Promise.reject(new Error("denied: " + text));
      }
      window.__copied.push(text);
      return Promise.resolve();
    },
  },
});
"""


class IdleRuntime:
    async def start_run(self, run: RunConfig) -> None:
        return None

    async def capacity(self) -> RuntimeCapacity:
        return RuntimeCapacity(active_contexts=0, maximum_active_contexts=1, chrome_processes=0)

    def error(self) -> str | None:
        return None

    def session_action(self, session_id: str) -> OperatorAction | None:
        return None

    def latest_add_action(self) -> OperatorAction | None:
        return None

    def stop_accepting(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _seed(database: Path) -> None:
    repository = SQLiteSessionRepository(database)

    async def seed() -> None:
        await repository.initialize()
        await repository.create_run(
            RunConfig(
                run_id="copy-url-browser",
                target_url="https://staging.synthetic.invalid/queue",
                requested_sessions=2,
                created_at=datetime.now(UTC),
            )
        )
        await repository.create(
            QueueSession(
                session_id="with-url",
                queue_id="fake-queue-0001",
                transfer_url=TRANSFER_URL,
                mode=SessionMode.HYBRID,
                state_path=Path("synthetic-state.json"),
                status=QueueStatus.ACTIVE_QUEUE,
            )
        )
        await repository.create(
            QueueSession(
                session_id="without-url",
                transfer_url="",
                mode=SessionMode.HYBRID,
                state_path=Path("synthetic-reservation.json"),
                status=QueueStatus.CREATING,
            )
        )
        await repository.close()

    asyncio.run(seed())


async def test_copy_url_copies_exact_url_after_repeated_htmx_refreshes(tmp_path: Path) -> None:
    database = tmp_path / "copy-browser.sqlite3"
    await asyncio.to_thread(_seed, database)
    app = create_app(
        settings=Settings(_env_file=None, DATABASE_URL=f"sqlite:///{database}"),
        repository=SQLiteSessionRepository(database),
        runtime=IdleRuntime(),  # type: ignore[arg-type]
    )
    counts = {"polls": 0, "copies": 0}
    served: list[str] = []

    with TestClient(app) as client:
        async with BrowserManager() as manager, manager.context() as context:
            page = await context.new_page()
            await page.add_init_script(CLIPBOARD_RECORDER)

            async def serve(route: object) -> None:
                request = route.request  # type: ignore[attr-defined]
                path = request.url.removeprefix(ORIGIN)
                if path.startswith("/partials/sessions"):
                    counts["polls"] += 1
                if path.endswith("/transfer-url"):
                    counts["copies"] += 1
                headers = {
                    key: value for key, value in request.headers.items() if key.startswith("hx-")
                }
                response = client.get(path, headers=headers, follow_redirects=False)
                body = response.text.replace("every 2s", "every 300ms")
                if not path.endswith("/transfer-url"):
                    served.append(body)
                await route.fulfill(  # type: ignore[attr-defined]
                    status=response.status_code,
                    body=body,
                    content_type=response.headers.get("content-type", "text/html"),
                )

            await page.route(f"{ORIGIN}/**", serve)
            await page.goto(f"{ORIGIN}/dashboard")
            await page.wait_for_function("window.htmx !== undefined")

            async def await_polls(extra: int) -> None:
                target = counts["polls"] + extra
                while counts["polls"] < target:
                    await page.wait_for_timeout(50)
                await page.wait_for_timeout(100)

            button = 'button[data-copy-session="with-url"]'
            await await_polls(2)
            await page.click(button)
            await page.wait_for_function("window.__copied.length === 1")
            assert await page.evaluate("window.__copied[0]") == TRANSFER_URL
            await page.wait_for_function(
                f"document.querySelector('{button}').textContent === 'Copied'"
            )
            # Feedback expires, then the label returns even on freshly swapped rows.
            await page.wait_for_function(
                f"document.querySelector('{button}').textContent === 'Copy URL'",
                timeout=5_000,
            )

            await await_polls(3)
            await page.click(button)
            await page.wait_for_function("window.__copied.length === 2")
            await page.wait_for_timeout(200)
            # One delegated listener: one fetch and one write per click, no rebinding.
            assert await page.evaluate("window.__copied") == [TRANSFER_URL, TRANSFER_URL]
            assert counts["copies"] == 2

            await page.evaluate("window.__clipboardFails = true")
            await page.click(button)
            await page.wait_for_function(
                f"document.querySelector('{button}').textContent === 'Copy failed'"
            )
            content = await page.content()

            assert await page.is_disabled('tr:has-text("without-url") button.copy-url')

    assert counts["copies"] == 3
    assert TRANSFER_URL not in content
    assert "fake-token" not in content
    assert served and all(TRANSFER_URL not in body for body in served)
