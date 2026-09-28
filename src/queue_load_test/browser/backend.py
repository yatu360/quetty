"""Minimal launch and context boundary for supported browser runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

from camoufox.async_api import AsyncNewBrowser, AsyncNewContext
from patchright.async_api import async_playwright as async_patchright
from playwright.async_api import Browser, BrowserContext, Playwright, StorageState

from queue_load_test.models.browser import BrowserBackendName

type ContextStorageState = str | Path | StorageState
type BrowserController = Any
type ManagedBrowser = Any
type ManagedBrowserContext = Any
type BrowserControllerStarter = Callable[[], Awaitable[BrowserController]]

# The selector is deliberately exact. Passing it to AsyncNewBrowser uses an installed
# browser or raises; unlike Camoufox's implicit active-version path, it cannot fetch.
CAMOUFOX_BROWSER_VERSION = "152.0.4-beta.30"
# Exact supported Python package pins (mirrored in pyproject.toml). Changing any of the
# three requires the documented upgrade and validation procedure.
CAMOUFOX_PACKAGE_VERSION = "0.5.6"
PLAYWRIGHT_VERSION = "1.62.0"
PATCHRIGHT_PACKAGE_VERSION = "1.63.0"
PATCHRIGHT_BROWSER_CHANNEL = "chrome"


async def _start_patchright_controller() -> BrowserController:
    return await async_patchright().start()


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

    controller_starter: BrowserControllerStarter | None

    async def launch(
        self, playwright: BrowserController, *, headless: bool
    ) -> ManagedBrowser: ...

    async def new_context(
        self,
        browser: ManagedBrowser,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> ManagedBrowserContext: ...

    def is_connected(self, browser: ManagedBrowser) -> bool: ...

    async def close_context(self, context: ManagedBrowserContext) -> None: ...

    async def close_browser(self, browser: ManagedBrowser) -> None: ...

    def diagnostics(
        self, browser: ManagedBrowser | None = None
    ) -> BrowserBackendDiagnostics: ...


class ChromeBackend:
    """Installed Google Chrome driven through Playwright Chromium."""

    # One Chrome process hosts up to 25 contexts; restarting it because a slow target
    # timed out would discard healthy in-flight work, so Chrome relies on disconnects.
    unresponsive_restart_threshold: int | None = None
    controller_starter: BrowserControllerStarter | None = None

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
    """Camoufox async API with one process shared by temporary contexts.

    Camoufox 0.5.6 can construct multiple contexts concurrently, but repeated
    concurrent navigation waves on one process can leave Firefox navigation
    calls pending.  Keep that version-specific constraint below the backend
    boundary: when ``serialize_contexts`` is set, one context is live per
    managed Camoufox process, while the application's fixed workers and global
    capacity coordinator stay bounded.  The lease is released when a close
    finishes; a close that hangs marks the process for replacement instead of
    letting a new context overlap it.
    ``serialize_contexts=False`` exists only for controlled capacity measurement;
    Phase 6 evidence shows overlapping create/navigate/close churn on one
    unserialized process can wedge it while it still reports connected.
    """

    # Consecutive navigation timeouts on one connected process before the manager
    # restarts it. Phase 6 observed a Camoufox process that stopped completing
    # navigations while still reporting connected; with one live context per process,
    # a restart can only discard the context that reported the timeout.
    unresponsive_restart_threshold: int | None = 3
    controller_starter: BrowserControllerStarter | None = None

    def __init__(self, *, serialize_contexts: bool = True) -> None:
        self._serialize_contexts = serialize_contexts
        self._process_leases: dict[int, asyncio.Semaphore] = {}
        self._context_leases: dict[int, asyncio.Semaphore] = {}

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
        if not self._serialize_contexts:
            return await AsyncNewContext(browser, **kwargs)
        lease = self._process_leases.setdefault(id(browser), asyncio.Semaphore(1))
        await lease.acquire()
        try:
            context = await AsyncNewContext(browser, **kwargs)
        except BaseException:
            lease.release()
            raise
        self._context_leases[id(context)] = lease
        return context

    def is_connected(self, browser: Browser) -> bool:
        return browser.is_connected()

    async def close_context(self, context: BrowserContext) -> None:
        # Release only once the close has finished: creating the next context while
        # the previous one is still closing is the overlapping churn that wedges a
        # Camoufox 0.5.6 process. A close that never finishes keeps the lease, and
        # the manager replaces that process (a replacement gets a fresh lease).
        lease = self._context_leases.pop(id(context), None)
        try:
            await context.close()
        finally:
            if lease is not None:
                lease.release()

    async def close_browser(self, browser: Browser) -> None:
        self._process_leases.pop(id(browser), None)
        await browser.close()

    def diagnostics(self, browser: Browser | None = None) -> BrowserBackendDiagnostics:
        return BrowserBackendDiagnostics(
            name=self.name,
            package_version=version("camoufox"),
            browser_version=browser.version if browser is not None else None,
        )


class PatchrightBackend:
    """Installed Google Chrome driven by Patchright's independent async controller."""

    # Prompt 1 found no evidence requiring Camoufox-style context serialization.
    # Use BrowserManager's ordinary concurrency and disconnect-based replacement.
    unresponsive_restart_threshold: int | None = None

    def __init__(
        self,
        *,
        controller_starter: BrowserControllerStarter = _start_patchright_controller,
    ) -> None:
        self.controller_starter: BrowserControllerStarter | None = controller_starter

    @property
    def name(self) -> BrowserBackendName:
        return BrowserBackendName.PATCHRIGHT

    async def launch(
        self,
        controller: BrowserController,
        *,
        headless: bool,
    ) -> ManagedBrowser:
        try:
            return await controller.chromium.launch(
                channel=PATCHRIGHT_BROWSER_CHANNEL,
                headless=headless,
            )
        except Exception as exc:
            message = str(exc).casefold()
            if "executable" not in message and "chrome" not in message:
                raise
            raise BrowserBackendSetupError(
                "Google Chrome is unavailable to Patchright. Install Chrome explicitly "
                "(for example: python -m patchright install chrome); runtime never downloads it."
            ) from exc

    async def new_context(
        self,
        browser: ManagedBrowser,
        *,
        storage_state: ContextStorageState | None = None,
    ) -> ManagedBrowserContext:
        if storage_state is None:
            return await browser.new_context()
        return await browser.new_context(storage_state=storage_state)

    def is_connected(self, browser: ManagedBrowser) -> bool:
        return bool(browser.is_connected())

    async def close_context(self, context: ManagedBrowserContext) -> None:
        await context.close()

    async def close_browser(self, browser: ManagedBrowser) -> None:
        await browser.close()

    def diagnostics(
        self,
        browser: ManagedBrowser | None = None,
    ) -> BrowserBackendDiagnostics:
        return BrowserBackendDiagnostics(
            name=self.name,
            package_version=version("patchright"),
            browser_version=browser.version if browser is not None else None,
        )


def create_browser_backend(name: BrowserBackendName) -> BrowserBackend:
    """Construct the configured backend without starting any browser process."""

    if name is BrowserBackendName.CHROME:
        return ChromeBackend()
    if name is BrowserBackendName.CAMOUFOX:
        return CamoufoxBackend()
    if name is BrowserBackendName.PATCHRIGHT:
        return PatchrightBackend()
    raise ValueError(f"Unsupported browser backend: {name!r}")


def manual_pool_topology(name: BrowserBackendName, capacity: int) -> tuple[int, int]:
    """Return ``(processes, contexts_per_process)`` for the headed manual Open pool.

    Chrome shares one headed process between operator windows. Camoufox 0.5.6
    allows one live context per process, and a retained operator window must not
    make a second Open wait on that lease, so each manual window gets its own
    bounded process slot (at most ``MAX_MANUAL_OPEN_SESSIONS``). The pool is still
    fixed-size: never one process per persisted Queue session.
    """

    if capacity < 1:
        raise ValueError("manual pool capacity must be at least 1")
    if BrowserBackendName.parse(name) is BrowserBackendName.CAMOUFOX:
        return capacity, 1
    return 1, capacity
