"""Local-only readiness probe for the pinned Patchright package and installed Chrome.

This is deliberately separate from the runtime backend boundary. Phase 7 Prompt 1 only
establishes dependency compatibility and whether disposable contexts are technically
viable; it does not make Patchright selectable by an application run.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version

from patchright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from queue_load_test.utils.asyncio_tools import await_bounded

PATCHRIGHT_PACKAGE_VERSION = "1.63.0"
PATCHRIGHT_BROWSER_CHANNEL = "chrome"
_LOCAL_PAGE = "data:text/html,<title>quetty-patchright-preflight</title><p>local</p>"


@dataclass(frozen=True, slots=True)
class PatchrightPreflightResult:
    """Serializable evidence from one Patchright/Chrome disposable-context probe."""

    passed: bool
    patchright_package_version: str | None
    expected_patchright_package_version: str
    browser_channel: str
    observed_browser_version: str | None
    package_imports: bool
    supported_version: bool
    browser_installed: bool
    async_launch: bool
    context_cycles_requested: int
    context_cycles_completed: int
    local_navigation: bool
    active_contexts_after_close: int
    browser_closed: bool
    patchright_stopped: bool
    managed_processes_after_shutdown: int
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def remedy(self) -> str:
        if not self.package_imports or not self.supported_version:
            return (
                "Reinstall the project dependencies (python -m pip install -e \".[test]\"); "
                f"Quetty requires patchright=={self.expected_patchright_package_version}."
            )
        if not self.browser_installed:
            return (
                "Install Google Chrome for the Patchright channel, for example with "
                "python -m patchright install chrome, then rerun the preflight."
            )
        return "Rerun queue-load-test-patchright-preflight for local failure details."

    def render_human(self) -> str:
        lines = [
            f"Patchright local preflight: {'PASS' if self.passed else 'FAIL'}",
            (
                f"Patchright package: {self.patchright_package_version or 'not installed'} "
                f"(required {self.expected_patchright_package_version})"
            ),
            f"Browser channel: {self.browser_channel}",
            f"Observed browser: {self.observed_browser_version or 'not available'}",
            (
                "Temporary context cycles: "
                f"{self.context_cycles_completed}/{self.context_cycles_requested}"
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


def _installed(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


async def run_patchright_preflight(
    *, timeout_seconds: float = 60.0, context_cycles: int = 5
) -> PatchrightPreflightResult:
    """Use Patchright async APIs with installed Chrome and disposable contexts only."""

    if context_cycles < 1:
        raise ValueError("context_cycles must be at least 1")

    package_version = _installed("patchright")
    package_imports = package_version is not None
    supported_version = package_version == PATCHRIGHT_PACKAGE_VERSION
    patchright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    observed_browser_version: str | None = None
    browser_installed = async_launch = local_navigation = False
    completed = 0
    active_after_close = 0
    patchright_stopped = False
    error: str | None = None

    async def exercise() -> None:
        nonlocal patchright, browser, context, page, observed_browser_version
        nonlocal browser_installed, async_launch, local_navigation, completed
        nonlocal active_after_close
        patchright = await async_playwright().start()
        browser = await patchright.chromium.launch(
            channel=PATCHRIGHT_BROWSER_CHANNEL,
            headless=True,
        )
        browser_installed = async_launch = True
        observed_browser_version = browser.version
        for _ in range(context_cycles):
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(_LOCAL_PAGE)
            local_navigation = await page.title() == "quetty-patchright-preflight"
            await page.close()
            page = None
            await context.close()
            context = None
            completed += 1
            active_after_close = len(browser.contexts)

    if package_imports and supported_version:
        try:
            await await_bounded(exercise(), timeout=timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - structured readiness evidence
            error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await await_bounded(page.close(), timeout=5.0)
            if context is not None:
                with contextlib.suppress(Exception):
                    await await_bounded(context.close(), timeout=5.0)
            if browser is not None:
                active_after_close = len(browser.contexts)
                with contextlib.suppress(Exception):
                    await await_bounded(browser.close(), timeout=10.0)
            if patchright is not None:
                with contextlib.suppress(Exception):
                    await await_bounded(patchright.stop(), timeout=10.0)
                    patchright_stopped = True
    else:
        error = "Unsupported or missing Patchright package version"

    browser_closed = browser is not None and not browser.is_connected()
    managed_after_shutdown = int(
        (browser is not None and not browser_closed)
        or (patchright is not None and not patchright_stopped)
    )
    passed = all(
        (
            package_imports,
            supported_version,
            browser_installed,
            async_launch,
            completed == context_cycles,
            local_navigation,
            active_after_close == 0,
            browser_closed,
            patchright_stopped,
            managed_after_shutdown == 0,
        )
    )
    return PatchrightPreflightResult(
        passed=passed,
        patchright_package_version=package_version,
        expected_patchright_package_version=PATCHRIGHT_PACKAGE_VERSION,
        browser_channel=PATCHRIGHT_BROWSER_CHANNEL,
        observed_browser_version=observed_browser_version,
        package_imports=package_imports,
        supported_version=supported_version,
        browser_installed=browser_installed,
        async_launch=async_launch,
        context_cycles_requested=context_cycles,
        context_cycles_completed=completed,
        local_navigation=local_navigation,
        active_contexts_after_close=active_after_close,
        browser_closed=browser_closed,
        patchright_stopped=patchright_stopped,
        managed_processes_after_shutdown=managed_after_shutdown,
        error=error,
    )
