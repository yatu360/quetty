"""Capture Queue-it identity from the supported transfer UI."""

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import SplitResult, parse_qsl, urljoin, urlsplit

from playwright.async_api import Locator, Page

from queue_load_test.browser.errors import BROWSER_ERROR_TYPES


class TransferFailure(StrEnum):
    """Observable reasons that a supported transfer could not be accepted."""

    MISSING_UI = "MISSING_UI"
    MISSING_VALUE = "MISSING_VALUE"
    MALFORMED_URL = "MALFORMED_URL"
    UNEXPECTED_HOST = "UNEXPECTED_HOST"
    UNEXPECTED_JOURNEY = "UNEXPECTED_JOURNEY"
    AMBIGUOUS_QUEUE_ID = "AMBIGUOUS_QUEUE_ID"
    QUEUE_ID_MISSING = "QUEUE_ID_MISSING"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"


@dataclass(frozen=True, slots=True)
class TransferSelector:
    """One configurable transfer control and its supported value sources."""

    selector: str
    value_sources: tuple[str, ...] = ("href", "value", "data-transfer-url", "data-url")


@dataclass(frozen=True, slots=True)
class QueueItTransferSelectors:
    """Known Queue-it and staging-theme transfer controls."""

    strategies: tuple[TransferSelector, ...] = (
        TransferSelector("#MainPart_hlTransfer"),
        TransferSelector("#MainPart_hlContinue"),
        TransferSelector("#MainPart_txtTransferUrl", ("value",)),
        TransferSelector('[data-testid="queue-transfer-link"]'),
        TransferSelector('[data-testid="queue-transfer-url"]', ("value", "data-url")),
        TransferSelector(
            'a:has-text("Continue my journey on another browser or device")',
            ("href",),
        ),
    )


@dataclass(frozen=True, slots=True)
class TransferExtractionDiagnostics:
    """Non-sensitive evidence about how transfer extraction was attempted."""

    matched_selector: str | None = None
    value_source: str | None = None
    selector_errors: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransferExtractionResult:
    """A supported transfer URL plus explicit expected and observed identities."""

    transfer_url: str | None = field(default=None, repr=False)
    expected_queue_id: str | None = field(default=None, repr=False)
    observed_queue_id: str | None = field(default=None, repr=False)
    identity_mismatch: bool = False
    failure: TransferFailure | None = None
    diagnostics: TransferExtractionDiagnostics = field(
        default_factory=TransferExtractionDiagnostics
    )

    @property
    def queue_id(self) -> str | None:
        """Return a non-conflicting identity, never an observed mismatch."""

        if self.identity_mismatch:
            return None
        return self.expected_queue_id or self.observed_queue_id

    @property
    def successful(self) -> bool:
        return self.failure is None


@dataclass(frozen=True, slots=True)
class _TransferValue:
    value: str
    selector: str
    source: str


class QueueItTransferExtractor:
    """Read an official transfer value exposed by the current Queue-it page."""

    def __init__(
        self,
        expected_journey_url: str,
        selectors: QueueItTransferSelectors | None = None,
    ) -> None:
        self.selectors = selectors or QueueItTransferSelectors()
        self._expected_url = expected_journey_url
        self._expected = self._parse_expected_url(expected_journey_url)

    async def extract(
        self,
        page: Page,
        *,
        expected_queue_id: str | None = None,
    ) -> TransferExtractionResult:
        """Capture and validate a transfer without constructing a Queue-it URL."""

        normalized_expected = self._optional_identity(expected_queue_id)
        selector_errors: dict[str, str] = {}
        transfer_value, ui_found = await self._find_transfer_value(page, selector_errors)
        if transfer_value is None:
            return TransferExtractionResult(
                expected_queue_id=normalized_expected,
                failure=(TransferFailure.MISSING_VALUE if ui_found else TransferFailure.MISSING_UI),
                diagnostics=TransferExtractionDiagnostics(selector_errors=selector_errors),
            )

        diagnostics = TransferExtractionDiagnostics(
            matched_selector=transfer_value.selector,
            value_source=transfer_value.source,
            selector_errors=selector_errors,
        )
        try:
            absolute_url = urljoin(self._expected_url, transfer_value.value.strip())
        except ValueError:
            return TransferExtractionResult(
                expected_queue_id=normalized_expected,
                failure=TransferFailure.MALFORMED_URL,
                diagnostics=diagnostics,
            )
        parsed = self._parse_transfer_url(absolute_url)
        if parsed is None:
            return TransferExtractionResult(
                expected_queue_id=normalized_expected,
                failure=TransferFailure.MALFORMED_URL,
                diagnostics=diagnostics,
            )

        if not self._same_origin(parsed):
            return TransferExtractionResult(
                expected_queue_id=normalized_expected,
                failure=TransferFailure.UNEXPECTED_HOST,
                diagnostics=diagnostics,
            )
        if self._normalized_path(parsed.path) != self._normalized_path(self._expected.path):
            return TransferExtractionResult(
                expected_queue_id=normalized_expected,
                failure=TransferFailure.UNEXPECTED_JOURNEY,
                diagnostics=diagnostics,
            )

        observed_ids = {
            value.strip()
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() == "q" and value.strip()
        }
        if len(observed_ids) > 1:
            return TransferExtractionResult(
                transfer_url=absolute_url,
                expected_queue_id=normalized_expected,
                failure=TransferFailure.AMBIGUOUS_QUEUE_ID,
                diagnostics=diagnostics,
            )
        observed_queue_id = next(iter(observed_ids), None)
        identity_mismatch = (
            normalized_expected is not None
            and observed_queue_id is not None
            and normalized_expected != observed_queue_id
        )
        if identity_mismatch:
            return TransferExtractionResult(
                transfer_url=absolute_url,
                expected_queue_id=normalized_expected,
                observed_queue_id=observed_queue_id,
                identity_mismatch=True,
                failure=TransferFailure.IDENTITY_MISMATCH,
                diagnostics=diagnostics,
            )
        if normalized_expected is None and observed_queue_id is None:
            return TransferExtractionResult(
                transfer_url=absolute_url,
                failure=TransferFailure.QUEUE_ID_MISSING,
                diagnostics=diagnostics,
            )
        return TransferExtractionResult(
            transfer_url=absolute_url,
            expected_queue_id=normalized_expected,
            observed_queue_id=observed_queue_id,
            diagnostics=diagnostics,
        )

    async def _find_transfer_value(
        self,
        page: Page,
        selector_errors: dict[str, str],
    ) -> tuple[_TransferValue | None, bool]:
        ui_found = False
        for index, strategy in enumerate(self.selectors.strategies):
            error_key = f"strategy_{index}"
            try:
                locator = page.locator(strategy.selector).first
                if await locator.count() == 0 or not await locator.is_visible():
                    continue
                ui_found = True
                for source in strategy.value_sources:
                    value = await self._read_value(locator, source)
                    if value is not None and value.strip():
                        return (
                            _TransferValue(
                                value=value,
                                selector=strategy.selector,
                                source=source,
                            ),
                            True,
                        )
            except BROWSER_ERROR_TYPES as exc:
                selector_errors[error_key] = type(exc).__name__
        return None, ui_found

    @staticmethod
    async def _read_value(locator: Locator, source: str) -> str | None:
        if source == "value":
            return await locator.input_value()
        if source == "text":
            return await locator.inner_text()
        return await locator.get_attribute(source)

    @staticmethod
    def _optional_identity(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _parse_expected_url(value: str) -> SplitResult:
        parsed = QueueItTransferExtractor._parse_transfer_url(value)
        if parsed is None:
            raise ValueError("expected_journey_url must be an absolute HTTP(S) URL")
        return parsed

    @staticmethod
    def _parse_transfer_url(value: str) -> SplitResult | None:
        if not value or any(character.isspace() for character in value):
            return None
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        return parsed

    def _same_origin(self, parsed: SplitResult) -> bool:
        parsed_hostname = parsed.hostname
        expected_hostname = self._expected.hostname
        if parsed_hostname is None or expected_hostname is None:
            return False
        return (
            parsed.scheme.casefold() == self._expected.scheme.casefold()
            and parsed_hostname.casefold() == expected_hostname.casefold()
            and self._effective_port(parsed) == self._effective_port(self._expected)
        )

    @staticmethod
    def _effective_port(parsed: SplitResult) -> int | None:
        if parsed.port is not None:
            return parsed.port
        return 443 if parsed.scheme.casefold() == "https" else 80

    @staticmethod
    def _normalized_path(path: str) -> str:
        normalized = path.rstrip("/")
        return normalized or "/"
