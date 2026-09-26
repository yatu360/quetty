"""Google Chrome process and isolated-context resource management."""

from queue_load_test.browser.manager import (
    BrowserCapacity,
    BrowserCapacityError,
    BrowserManager,
    BrowserManagerError,
    BrowserManagerNotStartedError,
    BrowserProcessCapacity,
    OwnedBrowserContext,
)

__all__ = [
    "BrowserCapacity",
    "BrowserCapacityError",
    "BrowserManager",
    "BrowserManagerError",
    "BrowserManagerNotStartedError",
    "BrowserProcessCapacity",
    "OwnedBrowserContext",
]
