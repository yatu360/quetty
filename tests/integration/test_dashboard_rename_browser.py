"""Rename works in a real browser through dashboard.js across HTMX refreshes.

The page, partials, static files, and rename route come from the real app (via
TestClient); only the polling interval is shortened and ``window.prompt`` returns a
scripted answer so the operator's input can be simulated.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from test_dashboard_copy_url_browser import IdleRuntime

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.models import QueueSession, QueueStatus, RunConfig, SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.web import create_app

ORIGIN = "http://dashboard.test"

PROMPT_SCRIPT = """
window.__answers = [];
window.__prompts = [];
window.prompt = function (message, current) {
  window.__prompts.push(current);
  return window.__answers.length ? window.__answers.shift() : null;
};
"""


def _seed(database: Path) -> None:
    repository = SQLiteSessionRepository(database)

    async def seed() -> None:
        await repository.initialize()
        await repository.create_run(
            RunConfig(
                run_id="rename-browser",
                target_url="https://staging.synthetic.invalid/queue",
                requested_sessions=1,
                created_at=datetime.now(UTC),
            )
        )
        await repository.create(
            QueueSession(
                session_id="named",
                queue_id="fake-queue-0001",
                transfer_url="https://queue.synthetic.invalid/?q=fake-queue-0001",
                mode=SessionMode.HYBRID,
                state_path=Path("synthetic-state.json"),
                status=QueueStatus.PRE_QUEUE,
            )
        )
        await repository.close()

    asyncio.run(seed())


async def test_rename_prompt_saves_unicode_name_and_survives_refreshes(tmp_path: Path) -> None:
    database = tmp_path / "rename-browser.sqlite3"
    await asyncio.to_thread(_seed, database)
    app = create_app(
        settings=Settings(_env_file=None, DATABASE_URL=f"sqlite:///{database}"),
        repository=SQLiteSessionRepository(database),
        runtime=IdleRuntime(),  # type: ignore[arg-type]
    )
    counts = {"polls": 0, "renames": 0}

    with TestClient(app) as client:
        async with BrowserManager() as manager, manager.context() as context:
            page = await context.new_page()
            await page.add_init_script(PROMPT_SCRIPT)

            async def serve(route: object) -> None:
                request = route.request  # type: ignore[attr-defined]
                path = request.url.removeprefix(ORIGIN)
                headers = {
                    key: value for key, value in request.headers.items() if key.startswith("hx-")
                }
                if request.method == "POST":
                    counts["renames"] += 1
                    headers["content-type"] = request.headers.get("content-type", "")
                    response = client.post(
                        path, content=request.post_data_buffer or b"", headers=headers
                    )
                else:
                    if path.startswith("/partials/sessions"):
                        counts["polls"] += 1
                    response = client.get(path, headers=headers, follow_redirects=False)
                await route.fulfill(  # type: ignore[attr-defined]
                    status=response.status_code,
                    body=response.text.replace("every 2s", "every 300ms"),
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

            name = "Café door — 東京 🎟"
            await await_polls(2)
            await page.evaluate("answer => window.__answers.push(answer)", name)
            await page.click("button.rename-session")
            await page.wait_for_selector(".session-name")
            await await_polls(3)  # refreshed rows keep showing the persisted name
            assert await page.inner_text(".session-name") == name
            # The prompt is pre-filled with the current name next time.
            await page.click("button.rename-session")  # cancelled: no request
            await page.wait_for_timeout(200)
            assert await page.evaluate("window.__prompts") == ["", name]
            assert counts["renames"] == 1

    stored = SQLiteSessionRepository(database)
    summaries = await stored.list_session_summaries(page=1, page_size=10)
    await stored.close()
    assert summaries.items[0].display_name == name
