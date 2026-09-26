from pathlib import Path

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueStatus, evaluate_queue_status
from queue_load_test.queue_monitor import (
    QueueItLiveStateExtractor,
    QueueItSelectors,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "queue_it"


async def test_live_queue_it_html_fixtures() -> None:
    extractor = QueueItLiveStateExtractor()
    expected_statuses = {
        "pre_queue.html": QueueStatus.PRE_QUEUE,
        "active_queue.html": QueueStatus.ACTIVE_QUEUE,
        "paused.html": QueueStatus.PAUSED,
        "serviced_soon.html": QueueStatus.SERVICED_SOON,
        "turn_started.html": QueueStatus.TURN_STARTED,
        "missing_optional.html": QueueStatus.CHECKING,
        "malformed.html": QueueStatus.CHECKING,
    }

    async with BrowserManager() as manager:
        for fixture_name, expected_status in expected_statuses.items():
            async with manager.context() as context:
                page = await context.new_page()
                await page.set_content((FIXTURES / fixture_name).read_text(encoding="utf-8"))

                progress = await extractor.extract(page, session_id=fixture_name)

                assert evaluate_queue_status(progress) is expected_status


async def test_extractor_reads_javascript_rendered_live_dom() -> None:
    extractor = QueueItLiveStateExtractor()
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content((FIXTURES / "active_queue.html").read_text(encoding="utf-8"))

        progress = await extractor.extract(page, session_id="active")

    assert progress.progress_percentage == 42
    assert isinstance(progress.progress_percentage, int)
    assert progress.queue_number == "Q-123"
    assert progress.users_ahead == 1234
    assert progress.estimated_wait_text == "About 12 minutes"
    assert progress.expected_service_time is not None
    assert progress.last_updated_at is not None
    assert progress.active_queue is True
    assert progress.pre_queue is False
    assert progress.diagnostics is not None
    assert progress.diagnostics.matched_selectors["progress_percentage"] == (
        "#MainPart_divProgressbar"
    )


async def test_pre_queue_is_independent_of_an_existing_queue_id_attribute() -> None:
    extractor = QueueItLiveStateExtractor()
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content((FIXTURES / "pre_queue.html").read_text(encoding="utf-8"))

        assert await page.locator("body").get_attribute("data-queue-id") is not None
        progress = await extractor.extract(page, session_id="pre")

    assert progress.pre_queue is True
    assert progress.active_queue is False
    assert evaluate_queue_status(progress) is QueueStatus.PRE_QUEUE


async def test_staging_theme_test_id_selectors_are_supported() -> None:
    extractor = QueueItLiveStateExtractor()
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content((FIXTURES / "paused.html").read_text(encoding="utf-8"))

        progress = await extractor.extract(page, session_id="paused")

    assert progress.progress_percentage == 25
    assert progress.queue_number == "Q-PAUSED"
    assert progress.users_ahead == 500
    assert progress.queue_paused is True


async def test_missing_hidden_and_malformed_values_remain_optional() -> None:
    extractor = QueueItLiveStateExtractor()
    async with BrowserManager() as manager:
        for fixture_name in ("missing_optional.html", "malformed.html"):
            async with manager.context() as context:
                page = await context.new_page()
                await page.set_content((FIXTURES / fixture_name).read_text(encoding="utf-8"))

                progress = await extractor.extract(page, session_id=fixture_name)

                assert progress.progress_percentage is None
                assert progress.queue_number is None
                assert progress.users_ahead is None
                assert progress.expected_service_time is None
                assert progress.active_queue is False
                assert evaluate_queue_status(progress) is QueueStatus.CHECKING


async def test_one_selector_failure_does_not_fail_observation() -> None:
    selectors = QueueItSelectors(
        queue_number=("[invalid-selector", "#MainPart_lbQueueNumber"),
    )
    extractor = QueueItLiveStateExtractor(selectors)
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content((FIXTURES / "active_queue.html").read_text(encoding="utf-8"))

        progress = await extractor.extract(page, session_id="defensive")

    assert progress.queue_number == "Q-123"
    assert progress.users_ahead == 1234
    assert progress.diagnostics is not None
    assert progress.diagnostics.field_errors["queue_number"] == "Error"


async def test_manual_warning_and_first_in_line_signals() -> None:
    extractor = QueueItLiveStateExtractor()
    html = """
        <div id="MainPart_lbManualUpdateWarning">Connection lost. Trying again.</div>
        <div id="first-in-line" style="width: 1px; height: 1px"></div>
    """
    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content(html)

        progress = await extractor.extract(page, session_id="signals")

    assert progress.connection_lost is True
    assert progress.first_in_line is True
    assert evaluate_queue_status(progress) is QueueStatus.CONNECTION_LOST
