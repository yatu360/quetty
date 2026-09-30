"""Dashboard times render in the viewer's own timezone (real Chrome, real HTMX)."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.web.app import local_time_markup

STATIC = Path(__file__).parents[2] / "src" / "queue_load_test" / "web" / "static"
ORIGIN = "http://dashboard.test"
INSTANT = datetime(2026, 9, 29, 22, 13, 5, tzinfo=UTC)


def test_markup_carries_the_exact_utc_instant_and_a_utc_fallback() -> None:
    same_instant = INSTANT.astimezone(timezone(timedelta(hours=1)))
    for value in (INSTANT, same_instant):
        rendered = str(local_time_markup(value))
        assert 'datetime="2026-09-29T22:13:05Z"' in rendered
        assert "29 Sep 22:13:05 UTC" in rendered
    assert str(local_time_markup(None)) == "—"


def _partial(poll: int) -> str:
    return (
        '<div id="session-results" hx-get="/partials/sessions" '
        'hx-trigger="every 300ms" hx-swap="outerHTML">'
        f'<span id="poll">{poll}</span>{local_time_markup(INSTANT)}</div>'
    )


@pytest.mark.parametrize(
    ("zone", "clock", "abbreviations"),
    [
        ("Europe/London", "23:13:05", {"BST", "GMT+1"}),
        ("America/New_York", "18:13:05", {"EDT", "GMT-4"}),
        ("UTC", "22:13:05", {"UTC"}),
    ],
)
async def test_times_are_shown_in_the_viewer_timezone_and_after_refresh(
    zone: str, clock: str, abbreviations: set[str]
) -> None:
    polls = {"count": 0}
    manager = BrowserManager(timezone_id=zone)
    async with manager, manager.context() as context:
        page = await context.new_page()

        async def serve(route: object) -> None:
            path = route.request.url.removeprefix(ORIGIN)  # type: ignore[attr-defined]
            if path.startswith("/static/"):
                body = (STATIC / path.removeprefix("/static/")).read_text(encoding="utf-8")
                await route.fulfill(body=body, content_type="text/javascript")  # type: ignore[attr-defined]
            elif path.startswith("/partials/sessions"):
                polls["count"] += 1
                await route.fulfill(body=_partial(polls["count"]), content_type="text/html")  # type: ignore[attr-defined]
            else:
                await route.fulfill(  # type: ignore[attr-defined]
                    body=(
                        '<!doctype html><html><head><script src="/static/htmx.min.js">'
                        '</script><script src="/static/dashboard.js"></script></head>'
                        f"<body>{_partial(0)}</body></html>"
                    ),
                    content_type="text/html",
                )

        await page.route(f"{ORIGIN}/**", serve)
        await page.goto(f"{ORIGIN}/dashboard")
        first = await page.text_content("time.local-time")
        while polls["count"] < 2:
            await page.wait_for_timeout(100)
        await page.wait_for_timeout(150)
        after_refresh = await page.text_content("time.local-time")

    for text in (first, after_refresh):
        assert text is not None
        assert clock in text, (zone, text)
        assert any(label in text for label in abbreviations), (zone, text)
        assert "22:13:05 UTC" not in text or zone == "UTC"
