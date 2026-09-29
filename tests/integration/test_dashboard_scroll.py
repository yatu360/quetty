"""The polled sessions table keeps its horizontal scroll across HTMX refreshes."""

from pathlib import Path

import pytest

from queue_load_test.browser import BrowserManager

STATIC = Path(__file__).parents[2] / "src" / "queue_load_test" / "web" / "static"
ORIGIN = "http://dashboard.test"


def _partial(poll: int) -> str:
    cells = "".join(f"<td>column-{index}-{poll}</td>" for index in range(40))
    return (
        '<div id="session-results" hx-get="/partials/sessions" '
        'hx-trigger="every 300ms" hx-swap="outerHTML">'
        f'<div class="table-wrap" style="overflow-x: auto; width: 400px">'
        f'<table style="white-space: nowrap"><tr>{cells}</tr></table></div></div>'
    )


def _page(with_fix: bool) -> str:
    script = '<script src="/static/dashboard.js"></script>' if with_fix else ""
    return (
        '<!doctype html><html><head><script src="/static/htmx.min.js"></script>'
        f"{script}</head><body>{_partial(0)}</body></html>"
    )


@pytest.mark.parametrize("with_fix", [True, False])
async def test_table_scroll_survives_polled_refresh(with_fix: bool) -> None:
    polls = {"count": 0}

    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()

        async def serve(route: object) -> None:
            request = route.request  # type: ignore[attr-defined]
            path = request.url.removeprefix(ORIGIN)
            if path.startswith("/static/"):
                body = (STATIC / path.removeprefix("/static/")).read_text(encoding="utf-8")
                await route.fulfill(body=body, content_type="text/javascript")  # type: ignore[attr-defined]
            elif path.startswith("/partials/sessions"):
                polls["count"] += 1
                await route.fulfill(body=_partial(polls["count"]), content_type="text/html")  # type: ignore[attr-defined]
            else:
                await route.fulfill(body=_page(with_fix), content_type="text/html")  # type: ignore[attr-defined]

        await page.route(f"{ORIGIN}/**", serve)
        await page.goto(f"{ORIGIN}/dashboard")
        await page.wait_for_function("window.htmx !== undefined")
        await page.evaluate("document.querySelector('.table-wrap').scrollLeft = 900")
        assert await page.evaluate("document.querySelector('.table-wrap').scrollLeft") == 900
        start = polls["count"]
        while polls["count"] < start + 3:
            await page.wait_for_timeout(100)
        await page.wait_for_timeout(150)
        after = await page.evaluate("document.querySelector('.table-wrap').scrollLeft")
        refreshed = await page.evaluate("document.querySelector('td').textContent")

    assert refreshed != "column-0-0"  # the table really was replaced by polls
    if with_fix:
        assert after == 900
    else:
        assert after == 0  # the original bug: every refresh jumps back to the left
