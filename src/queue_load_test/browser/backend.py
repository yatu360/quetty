"""Minimal launch and context boundary for supported browser runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

from camoufox.async_api import AsyncNewBrowser, AsyncNewContext
from playwright.async_api import Browser, BrowserContext, Playwright, StorageState

from queue_load_test.models.browser import BrowserBackendName

type ContextStorageState = str | Path | StorageState

# The selector is deliberately exact. Passing it to AsyncNewBrowser uses an installed
# browser or raises; unlike Camoufox's implicit active-version path, it cannot fetch.
CAMOUFOX_BROWSER_VERSION = "152.0.4-beta.30"


class BrowserBackendSetupError(RuntimeError):
    """A selected backend is not installed or cannot be started as configured."""


@dataclass(frozen=True, slots=True)
class BrowserBackendDiagnostics:
    """Backend package and live browser version information."""

    name: BrowserBackendName
    package_version: str
    browser_version: str | None = None


class BrowserBackend(Protocol):
    """Only the browser operations whose implementations differ by backend."""

    @property
    def name(self) -> BrowserBackendName: ...

    async def launch(self, playwright: Playwright, *, headless: bool) -> Browser: ...

    async def new_context(
        self,
        browser: Browser,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> BrowserContext: ...

    def is_connected(self, browser: Browser) -> bool: ...

    async def close_context(self, context: BrowserContext) -> None: ...

    async def close_browser(self, browser: Browser) -> None: ...

    def diagnostics(self, browser: Browser | None = None) -> BrowserBackendDiagnostics: ...


class ChromeBackend:
    """Installed Google Chrome driven through Playwright Chromium."""

    @property
    def name(self) -> BrowserBackendName:
        return BrowserBackendName.CHROME

    async def launch(self, playwright: Playwright, *, headless: bool) -> Browser:
        return await playwright.chromium.launch(channel="chrome", headless=headless)

    async def new_context(
        self,
        browser: Browser,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> BrowserContext:
        if storage_state is None:
            return await browser.new_context()
        return await browser.new_context(storage_state=storage_state)

    def is_connected(self, browser: Browser) -> bool:
        return browser.is_connected()

    async def close_context(self, context: BrowserContext) -> None:
        await context.close()

    async def close_browser(self, browser: Browser) -> None:
        await browser.close()

    def diagnostics(self, browser: Browser | None = None) -> BrowserBackendDiagnostics:
        return BrowserBackendDiagnostics(
            name=self.name,
            package_version=version("playwright"),
            browser_version=browser.version if browser is not None else None,
        )


class CamoufoxBackend:
    """Camoufox async API with one process shared by bounded temporary contexts."""

    @property
    def name(self) -> BrowserBackendName:
        return BrowserBackendName.CAMOUFOX

    async def launch(self, playwright: Playwright, *, headless: bool) -> Browser:
        try:
            browser = await AsyncNewBrowser(
                playwright,
                headless=headless,
                browser=CAMOUFOX_BROWSER_VERSION,
            )
        except ValueError as exc:
            if "Browser version" not in str(exc) or "not found" not in str(exc):
                raise
            raise BrowserBackendSetupError(
                "Camoufox browser build is missing. Install the pinned build with: "
                f"camoufox fetch official/stable/{CAMOUFOX_BROWSER_VERSION}"
            ) from exc
        # persistent_context is left at its public default (False), so this is Browser.
        return browser

    async def new_context(
        self,
        browser: Browser,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> BrowserContext:
        kwargs: dict[str, Any] = (
            {} if storage_state is None else {"storage_state": storage_state}
        )
        return await AsyncNewContext(browser, **kwargs)

    def is_connected(self, browser: Browser) -> bool:
        return browser.is_connected()

    async def close_context(self, context: BrowserContext) -> None:
        await context.close()

    async def close_browser(self, browser: Browser) -> None:
        await browser.close()

    def diagnostics(self, browser: Browser | None = None) -> BrowserBackendDiagnostics:
        return BrowserBackendDiagnostics(
            name=self.name,
            package_version=version("camoufox"),
            browser_version=browser.version if browser is not None else None,
        )


def create_browser_backend(name: BrowserBackendName) -> BrowserBackend:
    """Construct the configured backend without starting any browser process."""

    if name is BrowserBackendName.CHROME:
        return ChromeBackend()
    if name is BrowserBackendName.CAMOUFOX:
        return CamoufoxBackend()
    raise ValueError(f"Unsupported browser backend: {name!r}")
