from typing import Any, cast

import pytest

from queue_load_test.browser import (
    CAMOUFOX_BROWSER_VERSION,
    BrowserBackendSetupError,
    BrowserCapacityError,
    BrowserManager,
    CamoufoxBackend,
    ChromeBackend,
    create_browser_backend,
)
from queue_load_test.browser import backend as backend_module
from queue_load_test.browser.manager import PlaywrightStarter
from queue_load_test.models import BrowserBackendName


class FakeBrowser:
    version = CAMOUFOX_BROWSER_VERSION

    def __init__(self) -> None:
        self.connected = True
        self.context_calls: list[dict[str, object]] = []
        self.closed = False

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **kwargs: object) -> object:
        self.context_calls.append(kwargs)
        return object()

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.calls: list[dict[str, object]] = []

    async def launch(self, **kwargs: object) -> FakeBrowser:
        self.calls.append(kwargs)
        return self.browser


async def test_chrome_backend_uses_installed_chrome_channel() -> None:
    browser = FakeBrowser()
    playwright = cast(Any, type("FakePlaywright", (), {"chromium": FakeChromium(browser)})())
    backend = ChromeBackend()

    launched = await backend.launch(playwright, headless=False)

    assert launched is browser
    assert playwright.chromium.calls == [{"channel": "chrome", "headless": False}]


async def test_camoufox_backend_uses_supported_async_api_and_exact_installed_build(
    monkeypatch: Any,
) -> None:
    browser = FakeBrowser()
    launch_calls: list[tuple[object, dict[str, object]]] = []
    context_calls: list[tuple[object, dict[str, object]]] = []
    context = object()

    async def fake_new_browser(playwright: object, **kwargs: object) -> object:
        launch_calls.append((playwright, kwargs))
        return browser

    async def fake_new_context(selected: object, **kwargs: object) -> object:
        context_calls.append((selected, kwargs))
        return context

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", fake_new_browser)
    monkeypatch.setattr(backend_module, "AsyncNewContext", fake_new_context)
    backend = CamoufoxBackend()
    playwright = object()

    launched = await backend.launch(cast(Any, playwright), headless=True)
    created = await backend.new_context(cast(Any, launched), storage_state="state.json")

    assert launched is browser
    assert created is context
    assert launch_calls == [
        (
            playwright,
            {"headless": True, "browser": CAMOUFOX_BROWSER_VERSION},
        )
    ]
    assert context_calls == [(browser, {"storage_state": "state.json"})]


def test_backend_factory_selects_only_supported_backends() -> None:
    assert isinstance(create_browser_backend(BrowserBackendName.CHROME), ChromeBackend)
    assert isinstance(create_browser_backend(BrowserBackendName.CAMOUFOX), CamoufoxBackend)


async def test_missing_camoufox_build_has_actionable_error(monkeypatch: Any) -> None:
    async def missing(*args: object, **kwargs: object) -> object:
        raise ValueError(f"Browser version '{CAMOUFOX_BROWSER_VERSION}' not found")

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", missing)

    with pytest.raises(BrowserBackendSetupError, match="camoufox fetch"):
        await CamoufoxBackend().launch(cast(Any, object()), headless=True)


async def test_camoufox_manager_shares_one_bounded_process_across_contexts(
    monkeypatch: Any,
) -> None:
    browser = FakeBrowser()
    launch_count = 0

    async def fake_new_browser(*args: object, **kwargs: object) -> object:
        nonlocal launch_count
        launch_count += 1
        return browser

    async def fake_new_context(*args: object, **kwargs: object) -> object:
        return FakeContext()

    class FakePlaywright:
        stopped = False

        async def stop(self) -> None:
            self.stopped = True

    playwright = FakePlaywright()

    async def starter() -> Any:
        return playwright

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", fake_new_browser)
    monkeypatch.setattr(backend_module, "AsyncNewContext", fake_new_context)
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=2,
        max_active_contexts=2,
        playwright_starter=cast(PlaywrightStarter, starter),
        backend=CamoufoxBackend(),
    )

    await manager.start()
    first = await manager.create_context()
    second = await manager.create_context()
    with pytest.raises(BrowserCapacityError):
        await manager.create_context()

    assert launch_count == 1
    assert manager.managed_process_count == 1
    assert manager.active_context_count == 2
    await first.close()
    await second.close()
    await manager.shutdown()
    assert manager.managed_process_count == 0
    assert playwright.stopped
