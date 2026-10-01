"""Detection of the pre-Queue access-restriction page on the rendered DOM."""

import re
import unicodedata
from typing import Protocol

from playwright.async_api import Page

from queue_load_test.browser.errors import BROWSER_ERROR_TYPES

# Known visible text of the restriction page, already normalised. Matching is a
# whole-phrase containment check, so unrelated error pages never match.
ACCESS_RESTRICTED_PHRASES: tuple[str, ...] = (
    "we are sorry your access has been restricted",
)

_NON_WORD = re.compile(r"[^\w]+")


def normalize_page_text(text: str) -> str:
    """Fold case, apostrophes, punctuation and whitespace for a stable phrase match."""

    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NON_WORD.sub(" ", folded).split())


def is_access_restricted_text(text: str | None) -> bool:
    """Return whether rendered page text contains the known restriction message."""

    if not text:
        return False
    normalized = normalize_page_text(text)
    return any(phrase in normalized for phrase in ACCESS_RESTRICTED_PHRASES)


class AccessRestrictionDetector(Protocol):
    async def detect(self, page: Page) -> bool: ...


class RenderedAccessRestrictionDetector:
    """Read the rendered body text; unreadable pages are never classified as restricted."""

    def __init__(self, *, text_timeout_ms: float = 2_000) -> None:
        self._text_timeout_ms = text_timeout_ms

    async def detect(self, page: Page) -> bool:
        try:
            text = await page.locator("body").first.inner_text(timeout=self._text_timeout_ms)
        except BROWSER_ERROR_TYPES:
            return False
        return is_access_restricted_text(text)
