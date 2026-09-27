"""Local-only Camoufox launch, context, navigation, and cleanup preflight."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from importlib.metadata import version

from queue_load_test.browser import CAMOUFOX_BROWSER_VERSION, BrowserManager, CamoufoxBackend


@dataclass(frozen=True, slots=True)
class CamoufoxPreflightResult:
    """Serializable evidence from one local preflight run."""

    passed: bool
    camoufox_package_version: str
    playwright_version: str
    expected_browser_version: str
    observed_browser_version: str | None
    package_imports: bool
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

    def render_human(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"Camoufox local preflight: {status}",
            f"Camoufox package: {self.camoufox_package_version}",
            f"Playwright package: {self.playwright_version}",
            f"Browser build: {self.observed_browser_version or 'not available'}",
            f"Local navigation: {'PASS' if self.local_navigation else 'FAIL'}",
            f"Contexts after close: {self.active_contexts_after_close}",
            f"Managed processes after shutdown: {self.managed_processes_after_shutdown}",
        ]
        if self.error is not None:
            lines.append(f"Error: {self.error}")
        return "\n".join(lines) + "\n"


async def run_preflight() -> CamoufoxPreflightResult:
    """Exercise Camoufox against a data URL and always release manager resources."""

    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        headless=True,
        backend=CamoufoxBackend(),
    )
    observed_browser_version: str | None = None
    browser_installed = False
    async_launch = False
    context_opened = False
    local_navigation = False
    context_closed = False
    active_contexts_after_close = 0
    error: str | None = None
    owned = None
    try:
        await manager.start()
        async_launch = True
        browser_installed = True
        observed_browser_version = manager.backend_diagnostics().browser_version
        owned = await manager.create_context()
        context_opened = True
        page = await owned.context.new_page()
        await page.goto("data:text/html,<title>quetty-camoufox-preflight</title><p>local</p>")
        local_navigation = await page.title() == "quetty-camoufox-preflight"
        await owned.close()
        context_closed = owned.closed
        active_contexts_after_close = manager.active_context_count
    except Exception as exc:  # noqa: BLE001 - the CLI must return structured failure evidence
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if owned is not None and not owned.closed:
            await owned.close()
            context_closed = owned.closed
            active_contexts_after_close = manager.active_context_count
        await manager.shutdown()

    managed_processes_after_shutdown = manager.managed_process_count
    browser_closed = not manager.started and managed_processes_after_shutdown == 0
    passed = all(
        (
            browser_installed,
            async_launch,
            context_opened,
            local_navigation,
            context_closed,
            active_contexts_after_close == 0,
            browser_closed,
        )
    )
    return CamoufoxPreflightResult(
        passed=passed,
        camoufox_package_version=version("camoufox"),
        playwright_version=version("playwright"),
        expected_browser_version=CAMOUFOX_BROWSER_VERSION,
        observed_browser_version=observed_browser_version,
        package_imports=True,
        browser_installed=browser_installed,
        async_launch=async_launch,
        context_opened=context_opened,
        local_navigation=local_navigation,
        context_closed=context_closed,
        browser_closed=browser_closed,
        active_contexts_after_close=active_contexts_after_close,
        managed_processes_after_shutdown=managed_processes_after_shutdown,
        error=error,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format (default: human)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(run_preflight())
    if args.format == "json":
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(result.render_human(), end="")
    if not result.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
