"""Detect successful admission and terminal Queue-it pages from normal DOM behavior."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from playwright.async_api import Page

from queue_load_test.browser.errors import (
    BROWSER_ERROR_TYPES,
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


# DOM markers that only a Queue-it waiting-room page carries: the classic and demo
# live elements, the transfer dialog, the footer Queue ID, Queue-it's bot-challenge
# page, and the staging-theme test IDs. Their presence means "still queuing".
WAITING_ROOM_MARKERS: tuple[str, ...] = (
    "[id^='MainPart_']",
    "#queueIdLinkURL",
    "#queueIdLinkModal",
    "#hlLinkToQueueTicket2",
    "#divChallenge",
    "#challenge-container",
    "#expectedServiceTime",
    "[data-testid='pre-queue']",
    "[data-testid='active-queue']",
    "[data-testid='queue-progress']",
    "[data-testid='queue-transfer-link']",
    "[data-testid='queue-transfer-url']",
)

_POLL_SECONDS = 0.25


class AdmissionDetector:
    """Recognize admission only after the visitor has actually left the waiting room.

    A page counts as admitted when it carries no Queue-it waiting-room marker and
    either matches a configured destination or is no longer the session's own queue
    page (a different origin or path). The second rule supports runs whose operator
    only knows the queue page: a target URL equal to the queue page can no longer make
    the queue page itself look like admission.
    """

    def __init__(
        self,
        destinations: tuple[ExpectedDestination, ...],
        *,
        waiting_room_markers: tuple[str, ...] = WAITING_ROOM_MARKERS,
    ) -> None:
        if not destinations:
            raise ValueError("At least one expected destination is required")
        self.destinations = destinations
        self._marker_selector = ", ".join(waiting_room_markers)

    @classmethod
    def from_urls(cls, *values: str) -> "AdmissionDetector":
        return cls(tuple(ExpectedDestination.from_url(value) for value in values))

    def matches(self, value: str) -> bool:
        """URL-only destination match (no DOM evidence); see ``detect`` for admission."""

        return any(destination.matches(value) for destination in self.destinations)

    async def detect(
        self,
        page: Page,
        *,
        wait_timeout_ms: float = 0,
        queue_url: str | None = None,
    ) -> bool:
        """Return whether the page shows verified admission, optionally waiting for it."""

        if await self._admitted(page, queue_url):
            return True
        if wait_timeout_ms <= 0:
            return False
        deadline = asyncio.get_running_loop().time() + wait_timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_POLL_SECONDS)
            if await self._admitted(page, queue_url):
                return True
        return False

    async def is_waiting_room(self, page: Page) -> bool:
        """Whether the page still carries any Queue-it waiting-room marker.

        An unreadable page (for example mid-navigation) counts as a waiting room, so
        uncertainty never produces admission.
        """

        try:
            return bool(await page.locator(self._marker_selector).count())
        except BROWSER_ERROR_TYPES:
            return True

    async def _admitted(self, page: Page, queue_url: str | None) -> bool:
        url = page.url
        if not _is_http(url):
            return False
        if not (self.matches(url) or _left_queue_page(url, queue_url)):
            return False
        return not await self.is_waiting_room(page)


def _is_http(value: str) -> bool:
    try:
        return urlsplit(value).scheme.casefold() in {"http", "https"}
    except ValueError:
        return False


def _left_queue_page(url: str, queue_url: str | None) -> bool:
    """A different origin or path from the session's own queue page."""

    if not queue_url:
        return False
    try:
        current, queue = urlsplit(url), urlsplit(queue_url)
        current_port = _effective_port(current.scheme, current.port)
        queue_port = _effective_port(queue.scheme, queue.port)
    except ValueError:
        return False
    if current.hostname is None or queue.hostname is None:
        return False
    same_origin = (
        current.scheme.casefold() == queue.scheme.casefold()
        and current.hostname.casefold() == queue.hostname.casefold()
        and current_port == queue_port
    )
    same_path = (current.path.rstrip("/") or "/") == (queue.path.rstrip("/") or "/")
    return not (same_origin and same_path)


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
