from pathlib import Path

from queue_load_test.browser import BrowserManager
from queue_load_test.models import QueueSession, QueueStatus, SessionMode, evaluate_queue_status
from queue_load_test.queue_monitor import QueueItLiveStateExtractor
from queue_load_test.transfer import (
    QueueItTransferExtractor,
    QueueItTransferSelectors,
    TransferFailure,
    TransferSelector,
)

JOURNEY_URL = "https://queue.staging.example.test/journey"


async def test_supported_transfer_capture_and_validation_scenarios() -> None:
    extractor = QueueItTransferExtractor(JOURNEY_URL)
    scenarios = (
        (
            "transfer URL and Queue ID found",
            """
            <a id="MainPart_hlTransfer"
               href="https://queue.staging.example.test/journey?event=stage&amp;q=queue-123">
              Continue my journey on another browser or device
            </a>
            """,
            None,
            None,
        ),
        (
            "missing transfer UI",
            "<main>No transfer control in this theme</main>",
            "queue-123",
            TransferFailure.MISSING_UI,
        ),
        (
            "visible transfer UI with no value",
            '<a id="MainPart_hlTransfer">Continue my journey on another browser or device</a>',
            "queue-123",
            TransferFailure.MISSING_VALUE,
        ),
        (
            "malformed transfer URL",
            '<a id="MainPart_hlTransfer" href="https://[broken">Transfer</a>',
            "queue-123",
            TransferFailure.MALFORMED_URL,
        ),
        (
            "unexpected host",
            '<a id="MainPart_hlTransfer" href="https://unexpected.example/journey?q=queue-123">Transfer</a>',
            "queue-123",
            TransferFailure.UNEXPECTED_HOST,
        ),
        (
            "unexpected journey",
            '<a id="MainPart_hlTransfer" href="https://queue.staging.example.test/other?q=queue-123">Transfer</a>',
            "queue-123",
            TransferFailure.UNEXPECTED_JOURNEY,
        ),
        (
            "different expected and observed Queue IDs",
            '<a id="MainPart_hlTransfer" href="https://queue.staging.example.test/journey?q=observed-456">Transfer</a>',
            "expected-123",
            TransferFailure.IDENTITY_MISMATCH,
        ),
    )

    async with BrowserManager() as manager:
        for name, html, expected_queue_id, expected_failure in scenarios:
            async with manager.context() as context:
                page = await context.new_page()
                await page.set_content(html)

                result = await extractor.extract(page, expected_queue_id=expected_queue_id)

                assert result.failure is expected_failure, name
                if name == "transfer URL and Queue ID found":
                    assert result.successful
                    assert result.transfer_url == (
                        "https://queue.staging.example.test/journey?event=stage&q=queue-123"
                    )
                    assert result.observed_queue_id == "queue-123"
                    assert result.queue_id == "queue-123"
                    assert result.diagnostics.matched_selector == "#MainPart_hlTransfer"
                if name == "different expected and observed Queue IDs":
                    assert result.expected_queue_id == "expected-123"
                    assert result.observed_queue_id == "observed-456"
                    assert result.identity_mismatch
                    assert result.queue_id is None
                    assert "expected-123" not in repr(result)
                    assert "observed-456" not in repr(result)


async def test_live_value_capture_configurable_selector_and_pre_queue_identity() -> None:
    selectors = QueueItTransferSelectors(
        strategies=(TransferSelector("#custom-transfer", ("value",)),)
    )
    transfer_extractor = QueueItTransferExtractor(JOURNEY_URL, selectors)
    progress_extractor = QueueItLiveStateExtractor()
    session = QueueSession(
        session_id="pre-session",
        queue_id="known-in-pre-queue",
        transfer_url="pending",
        mode=SessionMode.HYBRID,
        status=QueueStatus.PRE_QUEUE,
        state_path=Path(".browser-state/pre-session.json"),
    )
    html = """
        <body class="before">
          <input id="custom-transfer" aria-label="Continue my journey" />
          <script>
            document.querySelector('#custom-transfer').value =
              'https://queue.staging.example.test/journey?q=known-in-pre-queue';
          </script>
        </body>
    """

    async with BrowserManager() as manager, manager.context() as context:
        page = await context.new_page()
        await page.set_content(html)

        transfer = await transfer_extractor.extract(
            page,
            expected_queue_id=session.queue_id,
        )
        progress = await progress_extractor.extract(page, session_id=session.session_id)

    assert transfer.successful
    assert transfer.expected_queue_id == "known-in-pre-queue"
    assert transfer.observed_queue_id == "known-in-pre-queue"
    assert not transfer.identity_mismatch
    assert transfer.queue_id == session.queue_id
    assert progress.pre_queue is True
    assert evaluate_queue_status(progress) is QueueStatus.PRE_QUEUE
