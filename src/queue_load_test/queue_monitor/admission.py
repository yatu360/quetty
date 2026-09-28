"""Detect successful admission and terminal Queue-it pages from normal DOM behavior."""

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from playwright.async_api import Page

from queue_load_test.browser.errors import (
    BROWSER_ERROR_TYPES,
    BROWSER_TIMEOUT_ERROR_TYPES,
)


@dataclass(frozen=True, slots=True)
class ExpectedDestination:
    """One allowed protected destination origin and path prefix."""

    scheme: str
    host: str
    port: int | None
    path_prefix: str

    @classmethod
    def from_url(cls, value: str) -> "ExpectedDestination":
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("Expected destination must be an absolute HTTP(S) URL")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Expected destination has an invalid port") from exc
        path = parsed.path.rstrip("/") or "/"
        return cls(parsed.scheme.casefold(), parsed.hostname.casefold(), port, path)

    def matches(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return False
        if parsed.hostname is None:
            return False
        if (
            parsed.scheme.casefold() != self.scheme
            or parsed.hostname.casefold() != self.host
            or _effective_port(parsed.scheme, port) != _effective_port(self.scheme, self.port)
        ):
            return False
        candidate_path = parsed.path.rstrip("/") or "/"
        if self.path_prefix == "/":
            return True
        return candidate_path == self.path_prefix or candidate_path.startswith(
            f"{self.path_prefix}/"
        )


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return 443 if scheme.casefold() == "https" else 80


class AdmissionDetector:
    """Recognize normal navigation to one of the protected staging destinations."""

    def __init__(self, destinations: tuple[ExpectedDestination, ...]) -> None:
        if not destinations:
            raise ValueError("At least one expected destination is required")
        self.destinations = destinations

    @classmethod
    def from_urls(cls, *values: str) -> "AdmissionDetector":
        return cls(tuple(ExpectedDestination.from_url(value) for value in values))

    def matches(self, value: str) -> bool:
        return any(destination.matches(value) for destination in self.destinations)

    async def detect(self, page: Page, *, wait_timeout_ms: float = 0) -> bool:
        if self.matches(page.url):
            return True
        if wait_timeout_ms <= 0:
            return False
        try:
            await page.wait_for_url(
                lambda url: self.matches(str(url)),
                timeout=wait_timeout_ms,
                wait_until="domcontentloaded",
            )
        except BROWSER_TIMEOUT_ERROR_TYPES:
            return False
        return self.matches(page.url)


class TerminalQueueState(StrEnum):
    EXPIRED = "EXPIRED"
    EVENT_CLOSED = "EVENT_CLOSED"


@dataclass(frozen=True, slots=True)
class TerminalStateSelectors:
    expired: tuple[str, ...] = (
        '[data-testid="queue-expired"]',
        "#queue-expired",
        "#MainPart_divQueueExpired",
    )
    event_closed: tuple[str, ...] = (
        '[data-testid="event-closed"]',
        "#event-closed",
        "#MainPart_divEventClosed",
    )


class QueueItTerminalStateDetector:
    """Read explicit, visible terminal markers without retaining page HTML."""

    def __init__(self, selectors: TerminalStateSelectors | None = None) -> None:
        self.selectors = selectors or TerminalStateSelectors()

    async def detect(self, page: Page) -> TerminalQueueState | None:
        if await self._has_visible(page, self.selectors.expired):
            return TerminalQueueState.EXPIRED
        if await self._has_visible(page, self.selectors.event_closed):
            return TerminalQueueState.EVENT_CLOSED
        return None

    @staticmethod
    async def _has_visible(page: Page, selectors: tuple[str, ...]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return True
            except BROWSER_ERROR_TYPES:
                continue
        return False
