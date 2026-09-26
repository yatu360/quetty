"""Live DOM extraction for Queue-it pages after JavaScript execution."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from queue_load_test.models import QueueExtractionDiagnostics, QueueProgress
from queue_load_test.queue_monitor.parsing import (
    parse_connection_lost,
    parse_estimated_wait_text,
    parse_expected_service_time,
    parse_first_in_line,
    parse_last_updated,
    parse_progress_percentage,
    parse_queue_number,
    parse_queue_paused,
    parse_serviced_soon,
    parse_turn_started,
    parse_users_ahead,
)

type BooleanParser = Callable[[str | None], bool | None]


@dataclass(frozen=True, slots=True)
class QueueItSelectors:
    """Known Queue-it selectors with optional staging-theme test IDs."""

    progress: tuple[str, ...] = (
        "#MainPart_divProgressbar",
        '[data-testid="queue-progress"]',
    )
    queue_number: tuple[str, ...] = (
        "#MainPart_lbQueueNumber",
        '[data-testid="queue-position"]',
    )
    users_ahead: tuple[str, ...] = (
        "#MainPart_lbUsersInLineAheadOfYou",
        '[data-testid="queue-users-ahead"]',
    )
    expected_service_time: tuple[str, ...] = ("#MainPart_lbExpectedServiceTime",)
    estimated_wait_text: tuple[str, ...] = ("#MainPart_lbWhichIsIn",)
    last_updated: tuple[str, ...] = ("#MainPart_lbLastUpdateTimeText",)
    queue_paused: tuple[str, ...] = ("#queue-paused", '[data-testid="queue-paused"]')
    first_in_line: tuple[str, ...] = ("#first-in-line", '[data-testid="first-in-line"]')
    serviced_soon: tuple[str, ...] = ("#serviced-soon", '[data-testid="serviced-soon"]')
    turn_started: tuple[str, ...] = ("#turn-started", '[data-testid="turn-started"]')
    manual_update_warning: tuple[str, ...] = ("#MainPart_lbManualUpdateWarning",)
    pre_queue: tuple[str, ...] = ('[data-testid="pre-queue"]',)
    active_queue: tuple[str, ...] = ('[data-testid="active-queue"]',)


@dataclass(frozen=True, slots=True)
class _ObservedElement:
    locator: Locator
    selector: str
    text: str


@dataclass(slots=True)
class _DiagnosticsBuilder:
    matched_selectors: dict[str, str] = field(default_factory=dict)
    field_errors: dict[str, str] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)

    def error(self, field_name: str, error: BaseException) -> None:
        self.field_errors.setdefault(field_name, type(error).__name__)

    def build(self) -> QueueExtractionDiagnostics:
        return QueueExtractionDiagnostics(
            matched_selectors=dict(self.matched_selectors),
            field_errors=dict(self.field_errors),
            signals=tuple(self.signals),
        )


class QueueItLiveStateExtractor:
    """Extract optional Queue-it values from the current rendered DOM."""

    def __init__(
        self,
        selectors: QueueItSelectors | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.selectors = selectors or QueueItSelectors()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def extract(self, page: Page, *, session_id: str) -> QueueProgress:
        """Observe a live page without requiring any individual element to exist."""

        diagnostics = _DiagnosticsBuilder()
        now = self._clock()

        progress_element = await self._find_visible(
            page,
            "progress_percentage",
            self.selectors.progress,
            diagnostics,
        )
        progress_percentage = await self._progress_value(progress_element, diagnostics)

        queue_number = parse_queue_number(
            await self._text(page, "queue_number", self.selectors.queue_number, diagnostics)
        )
        users_ahead = parse_users_ahead(
            await self._text(page, "users_ahead", self.selectors.users_ahead, diagnostics)
        )
        expected_service_time = parse_expected_service_time(
            await self._text(
                page,
                "expected_service_time",
                self.selectors.expected_service_time,
                diagnostics,
            ),
            reference_date=now.date(),
        )
        estimated_wait_text = parse_estimated_wait_text(
            await self._text(
                page,
                "estimated_wait_text",
                self.selectors.estimated_wait_text,
                diagnostics,
            )
        )
        last_updated_at = parse_last_updated(
            await self._text(page, "last_updated", self.selectors.last_updated, diagnostics),
            reference_date=now.date(),
        )
        queue_paused = await self._indicator(
            page,
            "queue_paused",
            self.selectors.queue_paused,
            parse_queue_paused,
            diagnostics,
        )
        first_in_line = await self._indicator(
            page,
            "first_in_line",
            self.selectors.first_in_line,
            parse_first_in_line,
            diagnostics,
        )
        serviced_soon = await self._indicator(
            page,
            "serviced_soon",
            self.selectors.serviced_soon,
            parse_serviced_soon,
            diagnostics,
        )
        turn_started = await self._indicator(
            page,
            "turn_started",
            self.selectors.turn_started,
            parse_turn_started,
            diagnostics,
        )
        manual_update_warning = parse_estimated_wait_text(
            await self._text(
                page,
                "manual_update_warning",
                self.selectors.manual_update_warning,
                diagnostics,
            )
        )
        connection_lost = parse_connection_lost(manual_update_warning)

        pre_queue = await self._detect_pre_queue(page, diagnostics, now)
        explicit_active = await self._has_visible(
            page,
            "active_queue",
            self.selectors.active_queue,
            diagnostics,
        )
        active_evidence = sum(
            value is not None
            for value in (
                queue_number,
                users_ahead,
                expected_service_time,
                estimated_wait_text,
            )
        )
        active_queue = not pre_queue and (explicit_active or active_evidence >= 2)
        if pre_queue:
            diagnostics.signals.append("pre_queue")
        if active_queue:
            diagnostics.signals.append("active_queue")
        if progress_percentage is not None:
            diagnostics.signals.append("progress_percentage")

        return QueueProgress(
            session_id=session_id,
            queue_number=queue_number,
            users_ahead=users_ahead,
            progress_percentage=progress_percentage,
            estimated_wait_text=estimated_wait_text,
            expected_service_time=expected_service_time,
            last_updated_at=last_updated_at,
            queue_paused=queue_paused,
            first_in_line=first_in_line,
            serviced_soon=serviced_soon,
            turn_started=turn_started,
            connection_lost=connection_lost,
            pre_queue=pre_queue,
            active_queue=active_queue,
            manual_update_warning=manual_update_warning,
            diagnostics=diagnostics.build(),
        )

    async def _find_visible(
        self,
        page: Page,
        field_name: str,
        selectors: tuple[str, ...],
        diagnostics: _DiagnosticsBuilder,
    ) -> _ObservedElement | None:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0 or not await locator.is_visible():
                    continue
                text = await locator.inner_text()
                diagnostics.matched_selectors[field_name] = selector
                return _ObservedElement(locator=locator, selector=selector, text=text)
            except PlaywrightError as exc:
                diagnostics.error(field_name, exc)
        return None

    async def _text(
        self,
        page: Page,
        field_name: str,
        selectors: tuple[str, ...],
        diagnostics: _DiagnosticsBuilder,
    ) -> str | None:
        observed = await self._find_visible(page, field_name, selectors, diagnostics)
        return observed.text if observed is not None else None

    async def _has_visible(
        self,
        page: Page,
        field_name: str,
        selectors: tuple[str, ...],
        diagnostics: _DiagnosticsBuilder,
    ) -> bool:
        return await self._find_visible(page, field_name, selectors, diagnostics) is not None

    async def _indicator(
        self,
        page: Page,
        field_name: str,
        selectors: tuple[str, ...],
        parser: BooleanParser,
        diagnostics: _DiagnosticsBuilder,
    ) -> bool | None:
        observed = await self._find_visible(page, field_name, selectors, diagnostics)
        if observed is None:
            return None
        parsed = parser(observed.text)
        return parsed if parsed is not None else True

    async def _progress_value(
        self,
        observed: _ObservedElement | None,
        diagnostics: _DiagnosticsBuilder,
    ) -> int | None:
        if observed is None:
            return None
        try:
            raw_value = await observed.locator.get_attribute("aria-valuenow")
        except PlaywrightError as exc:
            diagnostics.error("progress_percentage", exc)
            return None
        parsed = parse_progress_percentage(raw_value)
        if parsed is None or not parsed.is_integer():
            return None
        return int(parsed)

    async def _detect_pre_queue(
        self,
        page: Page,
        diagnostics: _DiagnosticsBuilder,
        now: datetime,
    ) -> bool:
        if await self._has_visible(
            page,
            "pre_queue",
            self.selectors.pre_queue,
            diagnostics,
        ):
            return True

        body_values: dict[str, str | None] = {}
        try:
            body = page.locator("body").first
            body_values = {
                "class": await body.get_attribute("class"),
                "isBeforeOrIdle": await body.get_attribute("data-is-before-or-idle"),
                "secondsToStart": await body.get_attribute("data-seconds-to-start"),
                "eventStartTime": await body.get_attribute("data-event-start-time"),
            }
        except PlaywrightError as exc:
            diagnostics.error("pre_queue_body", exc)

        body_class = body_values.get("class") or ""
        if "before" in body_class.casefold():
            return True

        page_state = await self._safe_page_state(page, diagnostics)
        is_before = body_values.get("isBeforeOrIdle") or page_state.get("isBeforeOrIdle")
        if self._truthy(is_before):
            return True
        seconds_to_start = body_values.get("secondsToStart") or page_state.get("secondsToStart")
        try:
            if isinstance(seconds_to_start, str | int | float) and float(seconds_to_start) > 0:
                return True
        except (TypeError, ValueError):
            pass

        event_start = body_values.get("eventStartTime") or page_state.get("eventStartTime")
        event_time = (
            parse_expected_service_time(event_start)
            if isinstance(event_start, str | int | float | bool | datetime)
            else None
        )
        return event_time is not None and event_time > now

    @staticmethod
    async def _safe_page_state(
        page: Page,
        diagnostics: _DiagnosticsBuilder,
    ) -> dict[str, object]:
        try:
            value: Any = await page.evaluate(
                """
                () => {
                    const result = {};
                    for (const key of ['isBeforeOrIdle', 'secondsToStart', 'eventStartTime']) {
                        const value = globalThis[key];
                        if (['boolean', 'number', 'string'].includes(typeof value)) {
                            result[key] = value;
                        }
                    }
                    return result;
                }
                """
            )
        except PlaywrightError as exc:
            diagnostics.error("pre_queue_page_state", exc)
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _truthy(value: object) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().casefold() in {"true", "1", "yes"}
        return False
