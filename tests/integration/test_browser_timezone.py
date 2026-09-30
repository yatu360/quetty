"""Pinned browser timezone and timezone-correct Queue-it time extraction (real Chrome)."""

from datetime import UTC, datetime

import pytest

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.queue_monitor import QueueItLiveStateExtractor

PAGE = """
<body>
  <div id="MainPart_divProgressbar" aria-valuenow="40" style="width:40px;height:10px"></div>
  <span id="MainPart_lbWhichIsIn">About 5 minutes</span>
  <span id="expectedServiceTime">{expected}</span>
  <span id="MainPart_lbLastUpdateTimeText">{updated}</span>
  {label}
</body>
"""


def _extractor(now: datetime) -> QueueItLiveStateExtractor:
    return QueueItLiveStateExtractor(clock=lambda: now)


@pytest.mark.parametrize("zone", [None, "America/New_York"])
async def test_contexts_are_pinned_to_the_configured_timezone(zone: str | None) -> None:
    manager = BrowserManager() if zone is None else BrowserManager(timezone_id=zone)
    async with manager, manager.context() as context:
        page = await context.new_page()
        resolved = await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert resolved == (zone or "Europe/London")


def test_settings_default_to_london_and_reject_unknown_zones() -> None:
    assert Settings(_env_file=None).browser_timezone == "Europe/London"  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="IANA timezone"):
        Settings(_env_file=None, BROWSER_TIMEZONE="Mars/Olympus")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("now", "expected_text", "updated_text", "label", "expected_utc", "updated_utc"),
    [
        # BST day, no label: the pinned Europe/London browser zone is used.
        (
            datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
            "2:45 PM", "14:01",
            "",
            datetime(2026, 7, 15, 13, 45, tzinfo=UTC),
            datetime(2026, 7, 15, 13, 1, tzinfo=UTC),
        ),
        # GMT day, no label.
        (
            datetime(2026, 1, 15, 14, 0, tzinfo=UTC),
            "2:45 PM", "14:01",
            "",
            datetime(2026, 1, 15, 14, 45, tzinfo=UTC),
            datetime(2026, 1, 15, 14, 1, tzinfo=UTC),
        ),
        # The page's own label wins over the browser zone.
        (
            datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
            "18:45", "18:01",
            '<span id="MainPart_lbExpectedServiceTimeTimeZonePostfix">(GMT+05:00)</span>',
            datetime(2026, 7, 15, 13, 45, tzinfo=UTC),
            datetime(2026, 7, 15, 13, 1, tzinfo=UTC),
        ),
    ],
)
async def test_extracted_times_are_converted_to_utc(
    now: datetime,
    expected_text: str,
    updated_text: str,
    label: str,
    expected_utc: datetime,
    updated_utc: datetime,
) -> None:
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content(
            PAGE.format(expected=expected_text, updated=updated_text, label=label)
        )
        progress = await _extractor(now).extract(page, session_id="tz")

    assert progress.expected_service_time == expected_utc
    assert progress.last_updated_at == updated_utc
    assert progress.active_queue is True
