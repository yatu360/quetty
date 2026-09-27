"""Managed browser process and isolated-context resource management."""

from queue_load_test.browser.backend import (
    CAMOUFOX_BROWSER_VERSION,
    BrowserBackend,
    BrowserBackendDiagnostics,
    BrowserBackendSetupError,
    CamoufoxBackend,
    ChromeBackend,
    create_browser_backend,
)
from queue_load_test.browser.manager import (
    BrowserCapacity,
    BrowserCapacityError,
    BrowserContextCapacity,
    BrowserManager,
    BrowserManagerError,
    BrowserManagerNotStartedError,
    BrowserProcessCapacity,
    OwnedBrowserContext,
)

__all__ = [
    "CAMOUFOX_BROWSER_VERSION",
    "BrowserBackend",
    "BrowserBackendDiagnostics",
    "BrowserBackendSetupError",
    "BrowserCapacity",
    "BrowserCapacityError",
    "BrowserContextCapacity",
    "BrowserManager",
    "BrowserManagerError",
    "BrowserManagerNotStartedError",
    "BrowserProcessCapacity",
    "CamoufoxBackend",
    "ChromeBackend",
    "OwnedBrowserContext",
    "create_browser_backend",
]
