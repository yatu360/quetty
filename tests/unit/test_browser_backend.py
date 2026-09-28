import asyncio
from typing import Any, cast

import pytest

from queue_load_test.browser import (
    CAMOUFOX_BROWSER_VERSION,
    PATCHRIGHT_BROWSER_CHANNEL,
    BrowserBackendSetupError,
    BrowserCapacityError,
    BrowserManager,
    CamoufoxBackend,
    ChromeBackend,
    PatchrightBackend,
    create_browser_backend,
    manual_pool_topology,
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
        self.fail_next_context = False

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **kwargs: object) -> object:
        if self.fail_next_context:
            self.fail_next_context = False
            raise RuntimeError("context creation failed")
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


async def test_patchright_backend_uses_its_controller_and_installed_chrome_channel() -> None:
    browser = FakeBrowser()
    controller = cast(Any, type("FakePatchright", (), {"chromium": FakeChromium(browser)})())
    backend = PatchrightBackend()

    launched = await backend.launch(controller, headless=False)
    context = await backend.new_context(launched, storage_state="state.json")

    assert launched is browser
    assert controller.chromium.calls == [
        {"channel": PATCHRIGHT_BROWSER_CHANNEL, "headless": False}
    ]
    assert browser.context_calls == [{"storage_state": "state.json"}]
    assert context is not None


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
    assert isinstance(create_browser_backend(BrowserBackendName.PATCHRIGHT), PatchrightBackend)


async def test_missing_camoufox_build_has_actionable_error(monkeypatch: Any) -> None:
    async def missing(*args: object, **kwargs: object) -> object:
        raise ValueError(f"Browser version '{CAMOUFOX_BROWSER_VERSION}' not found")

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", missing)

    with pytest.raises(BrowserBackendSetupError, match="camoufox fetch"):
        await CamoufoxBackend().launch(cast(Any, object()), headless=True)


async def test_missing_patchright_chrome_has_actionable_error() -> None:
    class MissingChromium:
        async def launch(self, **_: object) -> object:
            raise RuntimeError("Chrome executable does not exist")

    controller = cast(Any, type("MissingPatchright", (), {"chromium": MissingChromium()})())

    with pytest.raises(BrowserBackendSetupError, match="patchright install chrome"):
        await PatchrightBackend().launch(controller, headless=True)


async def test_camoufox_manager_serializes_contexts_on_one_bounded_process(
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
    second_task = asyncio.create_task(manager.create_context())
    await asyncio.sleep(0)

    assert launch_count == 1
    assert manager.managed_process_count == 1
    assert manager.active_context_count == 1
    assert not second_task.done()
    await first.close()
    second = await second_task
    assert manager.active_context_count == 1
    await second.close()
    await manager.shutdown()
    assert manager.managed_process_count == 0
    assert playwright.stopped


class HangingCloseContext(FakeContext):
    async def close(self) -> None:
        await asyncio.Event().wait()


async def test_camoufox_lease_is_per_process_and_held_until_close_finishes(
    monkeypatch: Any,
) -> None:
    contexts: list[FakeContext] = [HangingCloseContext(), FakeContext(), FakeContext()]

    async def fake_new_context(*args: object, **kwargs: object) -> object:
        return contexts.pop(0)

    monkeypatch.setattr(backend_module, "AsyncNewContext", fake_new_context)
    backend = CamoufoxBackend()
    first_browser, second_browser = FakeBrowser(), FakeBrowser()

    hanging = await backend.new_context(cast(Any, first_browser))
    # A different managed process is not serialized behind the first one.
    other = await asyncio.wait_for(backend.new_context(cast(Any, second_browser)), 1)

    closing = asyncio.create_task(backend.close_context(hanging))
    await asyncio.sleep(0)
    # No new context may overlap a close still in progress on the same process.
    waiting = asyncio.create_task(backend.new_context(cast(Any, first_browser)))
    done, _ = await asyncio.wait({waiting}, timeout=0.1)
    assert not done
    # Replacing the wedged process gives the replacement a fresh lease.
    waiting.cancel()
    closing.cancel()
    await backend.close_browser(cast(Any, first_browser))
    replacement_browser = FakeBrowser()
    replacement = await asyncio.wait_for(backend.new_context(cast(Any, replacement_browser)), 1)

    await backend.close_context(other)
    await backend.close_context(replacement)


def test_manual_pool_gives_each_camoufox_window_its_own_bounded_process() -> None:
    assert manual_pool_topology(BrowserBackendName.CHROME, 5) == (1, 5)
    assert manual_pool_topology(BrowserBackendName.CAMOUFOX, 5) == (5, 1)
    assert manual_pool_topology(BrowserBackendName.PATCHRIGHT, 5) == (1, 5)
    with pytest.raises(ValueError):
        manual_pool_topology(BrowserBackendName.CAMOUFOX, 0)


async def test_patchright_manager_bounds_capacity_releases_failures_and_cleans_up() -> None:
    class ManagedChromium:
        def __init__(self) -> None:
            self.browsers: list[FakeBrowser] = []
            self.calls: list[dict[str, object]] = []

        async def launch(self, **options: object) -> FakeBrowser:
            self.calls.append(options)
            browser = FakeBrowser()
            self.browsers.append(browser)
            return browser

    class FakePatchright:
        def __init__(self) -> None:
            self.chromium = ManagedChromium()
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    controller = FakePatchright()

    async def starter() -> Any:
        return controller

    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        headless=False,
        backend=PatchrightBackend(controller_starter=starter),
    )
    await manager.start()
    browser = controller.chromium.browsers[0]
    browser.fail_next_context = True

    with pytest.raises(RuntimeError, match="context creation failed"):
        await manager.create_context()
    assert manager.active_context_count == 0

    owned = await manager.create_context()
    with pytest.raises(BrowserCapacityError):
        await manager.create_context()
    await owned.close()
    await manager.shutdown()

    assert controller.stopped
    assert controller.chromium.calls == [{"channel": "chrome", "headless": False}]
    assert manager.managed_process_count == 0
    assert manager.active_context_count == 0
    assert not browser.connected


async def test_patchright_disconnected_process_is_replaced_without_multiplication() -> None:
    class ManagedChromium:
        def __init__(self) -> None:
            self.browsers: list[FakeBrowser] = []

        async def launch(self, **_: object) -> FakeBrowser:
            browser = FakeBrowser()
            self.browsers.append(browser)
            return browser

    class FakePatchright:
        def __init__(self) -> None:
            self.chromium = ManagedChromium()

        async def stop(self) -> None:
            return None

    controller = FakePatchright()

    async def starter() -> Any:
        return controller

    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        backend=PatchrightBackend(controller_starter=starter),
    )
    await manager.start()
    original = controller.chromium.browsers[0]
    original.connected = False

    capacity = await manager.capacity()

    assert manager.restart_count == 1
    assert manager.managed_process_count == 1
    assert capacity.connected_processes == 1
    assert len(controller.chromium.browsers) == 2
    await manager.shutdown()


async def test_unserialized_camoufox_measurement_backend_allows_concurrent_contexts(
    monkeypatch: Any,
) -> None:
    async def fake_new_context(*args: object, **kwargs: object) -> object:
        return FakeContext()

    monkeypatch.setattr(backend_module, "AsyncNewContext", fake_new_context)
    backend = CamoufoxBackend(serialize_contexts=False)
    browser = FakeBrowser()

    first = await backend.new_context(cast(Any, browser))
    second = await asyncio.wait_for(backend.new_context(cast(Any, browser)), 1)

    await backend.close_context(first)
    await backend.close_context(second)


async def _fake_camoufox_manager(monkeypatch: Any, backend: Any) -> tuple[BrowserManager, list[Any]]:
    launched: list[Any] = []

    async def fake_new_browser(*args: object, **kwargs: object) -> object:
        browser = FakeBrowser()
        launched.append(browser)
        return browser

    async def fake_new_context(*args: object, **kwargs: object) -> object:
        return FakeContext()

    class FakePlaywright:
        async def stop(self) -> None:
            return None

        chromium = type("Chromium", (), {"launch": staticmethod(fake_new_browser)})()

    async def starter() -> Any:
        return FakePlaywright()

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", fake_new_browser)
    monkeypatch.setattr(backend_module, "AsyncNewContext", fake_new_context)
    monkeypatch.setattr(ChromeBackend, "new_context", lambda self, browser, **_: fake_new_context())
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        playwright_starter=cast(PlaywrightStarter, starter),
        backend=backend,
    )
    await manager.start()
    return manager, launched


async def test_unresponsive_camoufox_process_is_restarted_after_threshold(
    monkeypatch: Any,
) -> None:
    manager, launched = await _fake_camoufox_manager(monkeypatch, CamoufoxBackend())
    owned = await manager.create_context()
    # A successful navigation resets the streak.
    manager.report_navigation(owned.context, responsive=False)
    manager.report_navigation(owned.context, responsive=False)
    manager.report_navigation(owned.context, responsive=True)
    manager.report_navigation(owned.context, responsive=False)
    assert manager.unresponsive_restart_count == 0
    manager.report_navigation(owned.context, responsive=False)
    manager.report_navigation(owned.context, responsive=False)
    assert manager.unresponsive_restart_count == 1

    capacity = await manager.capacity()

    assert manager.restart_count == 1
    assert len(launched) == 2 and launched[0].closed
    assert owned.closed and capacity.active_contexts == 0
    assert capacity.connected_processes == 1
    replacement = await manager.create_context()
    assert replacement.context is not owned.context
    await replacement.close()
    await manager.shutdown()


async def test_chrome_ignores_navigation_timeouts_for_restart(monkeypatch: Any) -> None:
    manager, launched = await _fake_camoufox_manager(monkeypatch, ChromeBackend())
    owned = await manager.create_context()
    for _ in range(10):
        manager.report_navigation(owned.context, responsive=False)

    await manager.capacity()

    assert manager.unresponsive_restart_count == 0
    assert manager.restart_count == 0
    assert len(launched) == 1 and not owned.closed
    await owned.close()
    await manager.shutdown()


async def test_shutdown_is_bounded_when_browser_close_ignores_first_cancellation(
    monkeypatch: Any,
) -> None:
    manager, launched = await _fake_camoufox_manager(monkeypatch, CamoufoxBackend())
    stopped: list[bool] = []

    async def wedged_close() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)
            raise

    launched[0].close = wedged_close
    manager._close_timeout_seconds = 0.05

    task = asyncio.ensure_future(manager.shutdown())
    done, _ = await asyncio.wait({task}, timeout=5)
    assert task in done, "shutdown did not return"

    assert stopped == [True]
    assert manager.managed_process_count == 0


async def test_hung_playwright_stop_kills_the_driver_so_pending_calls_end(
    monkeypatch: Any,
) -> None:
    killed: list[bool] = []

    class FakeDriver:
        returncode = None

        def kill(self) -> None:
            killed.append(True)
            self.returncode = -9

    class HangingPlaywright:
        def __init__(self) -> None:
            transport = type("Transport", (), {"_proc": FakeDriver()})()
            connection = type("Connection", (), {"_transport": transport})()
            self._impl_obj = type("Impl", (), {"_connection": connection})()

        async def stop(self) -> None:
            while True:  # a driver that never exits while a browser is wedged
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if killed:
                        raise
                    continue

    async def fake_new_browser(*args: object, **kwargs: object) -> object:
        return FakeBrowser()

    async def starter() -> Any:
        return HangingPlaywright()

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", fake_new_browser)
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        playwright_starter=cast(PlaywrightStarter, starter),
        backend=CamoufoxBackend(),
        close_timeout_seconds=0.05,
    )
    await manager.start()

    task = asyncio.ensure_future(manager.shutdown())
    done, _ = await asyncio.wait({task}, timeout=10)

    assert task in done, "shutdown did not return"
    assert killed == [True]
    assert not manager.started
