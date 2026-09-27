"""Local-only readiness check for the pinned Camoufox runtime.

Used by the operator CLI (``queue-load-test-camoufox-preflight``) and before a new
Camoufox run is persisted. It never downloads or updates a browser: a missing build
fails with the exact supported install command.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version

from queue_load_test.browser.backend import (
    CAMOUFOX_BROWSER_VERSION,
    CAMOUFOX_PACKAGE_VERSION,
    PLAYWRIGHT_VERSION,
    CamoufoxBackend,
)
from queue_load_test.browser.manager import BrowserManager, OwnedBrowserContext
from queue_load_test.utils.asyncio_tools import await_bounded

CAMOUFOX_FETCH_COMMAND = f"camoufox fetch official/stable/{CAMOUFOX_BROWSER_VERSION}"
_LOCAL_PAGE = "data:text/html,<title>quetty-camoufox-preflight</title><p>local</p>"


@dataclass(frozen=True, slots=True)
class CamoufoxPreflightResult:
    """Serializable evidence from one local preflight run."""

    passed: bool
    camoufox_package_version: str | None
    playwright_version: str | None
    expected_camoufox_package_version: str
    expected_playwright_version: str
    expected_browser_version: str
    observed_browser_version: str | None
    package_imports: bool
    supported_versions: bool
    browser_installed: bool
    async_launch: bool
    context_opened: bool
    local_navigation: bool
    context_closed: bool
    browser_closed: bool
    active_contexts_after_close: int
    managed_processes_after_shutdown: int
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def remedy(self) -> str:
        """One actionable operator instruction for a failed preflight."""

        if not self.package_imports or not self.supported_versions:
            return (
                "Reinstall the project dependencies (python -m pip install -e \".[test]\"); "
                f"Quetty requires camoufox=={self.expected_camoufox_package_version} and "
                f"playwright=={self.expected_playwright_version}."
            )
        if not self.browser_installed:
            return f"Install the pinned Camoufox browser with: {CAMOUFOX_FETCH_COMMAND}"
        return (
            "Camoufox did not complete a local launch; run queue-load-test-camoufox-preflight "
            "for details."
        )

    def render_human(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"Camoufox local preflight: {status}",
            (
                f"Camoufox package: {self.camoufox_package_version or 'not installed'} "
                f"(required {self.expected_camoufox_package_version})"
            ),
            (
                f"Playwright package: {self.playwright_version or 'not installed'} "
                f"(required {self.expected_playwright_version})"
            ),
            (
                f"Browser build: {self.observed_browser_version or 'not available'} "
                f"(required {self.expected_browser_version})"
            ),
            f"Local navigation: {'PASS' if self.local_navigation else 'FAIL'}",
            f"Contexts after close: {self.active_contexts_after_close}",
            f"Managed processes after shutdown: {self.managed_processes_after_shutdown}",
        ]
        if self.error is not None:
            lines.append(f"Error: {self.error}")
        if not self.passed:
            lines.append(f"Next step: {self.remedy}")
        return "\n".join(lines) + "\n"


type CamoufoxPreflight = Callable[[], Awaitable[CamoufoxPreflightResult]]


def _installed(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


async def run_camoufox_preflight(*, timeout_seconds: float = 60.0) -> CamoufoxPreflightResult:
    """Launch the pinned build, open one context, load a data URL, and clean up."""

    package_version = _installed("camoufox")
    playwright_version = _installed("playwright")
    package_imports = package_version is not None and playwright_version is not None
    supported_versions = (
        package_version == CAMOUFOX_PACKAGE_VERSION and playwright_version == PLAYWRIGHT_VERSION
    )
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        headless=True,
        backend=CamoufoxBackend(),
    )
    observed: str | None = None
    browser_installed = async_launch = context_opened = False
    local_navigation = context_closed = False
    active_after_close = 0
    error: str | None = None
    owned: OwnedBrowserContext | None = None

    async def exercise() -> None:
        nonlocal async_launch, browser_installed, observed, owned, context_opened
        nonlocal local_navigation, context_closed, active_after_close
        await manager.start()
        async_launch = browser_installed = True
        observed = manager.backend_diagnostics().browser_version
        owned = await manager.create_context()
        context_opened = True
        page = await owned.context.new_page()
        await page.goto(_LOCAL_PAGE)
        local_navigation = await page.title() == "quetty-camoufox-preflight"
        await owned.close()
        context_closed = owned.closed
        active_after_close = manager.active_context_count

    if package_imports and supported_versions:
        try:
            await await_bounded(exercise(), timeout=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - structured failure evidence
            error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            if owned is not None and not owned.closed:
                await owned.close()
                context_closed = owned.closed
                active_after_close = manager.active_context_count
            await manager.shutdown()
    else:
        error = "Unsupported or missing camoufox/playwright package versions"
    processes_after = manager.managed_process_count
    browser_closed = not manager.started and processes_after == 0
    passed = all(
        (
            package_imports,
            supported_versions,
            browser_installed,
            async_launch,
            context_opened,
            local_navigation,
            context_closed,
            active_after_close == 0,
            browser_closed,
            observed is None or observed == CAMOUFOX_BROWSER_VERSION,
        )
    )
    return CamoufoxPreflightResult(
        passed=passed,
        camoufox_package_version=package_version,
        playwright_version=playwright_version,
        expected_camoufox_package_version=CAMOUFOX_PACKAGE_VERSION,
        expected_playwright_version=PLAYWRIGHT_VERSION,
        expected_browser_version=CAMOUFOX_BROWSER_VERSION,
        observed_browser_version=observed,
        package_imports=package_imports,
        supported_versions=supported_versions,
        browser_installed=browser_installed,
        async_launch=async_launch,
        context_opened=context_opened,
        local_navigation=local_navigation,
        context_closed=context_closed,
        browser_closed=browser_closed,
        active_contexts_after_close=active_after_close,
        managed_processes_after_shutdown=processes_after,
        error=error,
    )
