from typing import Any, cast

import pytest

from queue_load_test.browser import (
    BrowserCapacityError,
    BrowserManager,
)
from queue_load_test.browser.manager import PlaywrightStarter


class FakeContext:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        self.closed = False
        self.fail_close = False

    async def close(self) -> None:
        if self.fail_close:
            raise RuntimeError("context close failed")
        self.closed = True


class FakeBrowser:
    def __init__(self, process_id: int) -> None:
        self.process_id = process_id
        self.connected = True
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **options: object) -> FakeContext:
        if not self.connected:
            raise RuntimeError("browser process is disconnected")
        context = FakeContext(options)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.connected = False
        for context in self.contexts:
            context.closed = True


class FakeChromium:
    def __init__(self) -> None:
        self.launch_calls: list[dict[str, object]] = []
        self.browsers: list[FakeBrowser] = []

    async def launch(self, **options: object) -> FakeBrowser:
        self.launch_calls.append(options)
        browser = FakeBrowser(len(self.browsers))
        self.browsers.append(browser)
        return browser


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def manager_and_playwright(
    *,
    processes: int = 1,
    per_browser: int = 5,
    global_limit: int = 5,
) -> tuple[BrowserManager, FakePlaywright]:
    playwright = FakePlaywright()

    async def starter() -> Any:
        return playwright

    manager = BrowserManager(
        chrome_process_count=processes,
        max_contexts_per_browser=per_browser,
        max_active_contexts=global_limit,
        playwright_starter=cast(PlaywrightStarter, starter),
    )
    return manager, playwright


async def test_manager_starts_configured_google_chrome_processes() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=3, global_limit=6)

    await manager.start()

    assert manager.started
    assert playwright.chromium.launch_calls == [
        {"channel": "chrome", "headless": True},
        {"channel": "chrome", "headless": True},
    ]
    await manager.shutdown()


async def test_fresh_context_has_no_shared_storage_state() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()

    first = await manager.create_context()
    second = await manager.create_context()

    assert first.context is not second.context
    assert playwright.chromium.browsers[0].contexts[0].options == {}
    assert playwright.chromium.browsers[0].contexts[1].options == {}
    await first.close()
    await second.close()
    await manager.shutdown()


async def test_storage_state_is_used_only_when_explicitly_restoring() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()
    state = {"cookies": [], "origins": []}

    owned = await manager.create_context(storage_state=state)

    assert playwright.chromium.browsers[0].contexts[0].options == {"storage_state": state}
    await owned.close()
    await manager.shutdown()


async def test_least_loaded_browser_allocation() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=2, global_limit=4)
    await manager.start()

    contexts = [await manager.create_context() for _ in range(3)]

    assert len(playwright.chromium.browsers[0].contexts) == 2
    assert len(playwright.chromium.browsers[1].contexts) == 1
    for context in contexts:
        await context.close()
    await manager.shutdown()


async def test_global_context_limit_is_enforced() -> None:
    manager, _ = manager_and_playwright(processes=2, per_browser=5, global_limit=2)
    await manager.start()
    contexts = [await manager.create_context() for _ in range(2)]

    with pytest.raises(BrowserCapacityError, match="Global"):
        await manager.create_context()

    for context in contexts:
        await context.close()
    await manager.shutdown()


async def test_per_browser_capacity_is_enforced() -> None:
    manager, _ = manager_and_playwright(processes=1, per_browser=2, global_limit=2)
    await manager.start()
    contexts = [await manager.create_context() for _ in range(2)]

    with pytest.raises(BrowserCapacityError):
        await manager.create_context()

    for context in contexts:
        await context.close()
    await manager.shutdown()


async def test_release_updates_capacity_and_is_idempotent() -> None:
    manager, _ = manager_and_playwright()
    await manager.start()
    owned = await manager.create_context()
    assert (await manager.capacity()).active_contexts == 1

    await owned.close()
    await owned.close()

    capacity = await manager.capacity()
    assert owned.closed
    assert capacity.active_contexts == 0
    assert capacity.available_contexts == 5
    await manager.shutdown()


async def test_close_failure_does_not_leak_manager_capacity() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()
    owned = await manager.create_context()
    playwright.chromium.browsers[0].contexts[0].fail_close = True

    await owned.close()

    assert owned.closed
    assert (await manager.capacity()).active_contexts == 0
    await manager.shutdown()


async def test_context_scope_cleans_up_after_failure() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()

    with pytest.raises(RuntimeError, match="test failure"):
        async with manager.context() as context:
            assert context is playwright.chromium.browsers[0].contexts[0]
            raise RuntimeError("test failure")

    assert playwright.chromium.browsers[0].contexts[0].closed
    assert (await manager.capacity()).active_contexts == 0
    await manager.shutdown()


async def test_dead_browser_is_detected_and_restarted() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()
    lost_context = await manager.create_context()
    failed_browser = playwright.chromium.browsers[0]
    failed_browser.connected = False

    restarted = await manager.restart_failed_browsers()

    assert restarted == 1
    assert lost_context.closed
    assert len(playwright.chromium.browsers) == 2
    assert playwright.chromium.browsers[1].connected
    replacement_context = await manager.create_context()
    assert replacement_context.context is playwright.chromium.browsers[1].contexts[0]
    await replacement_context.close()
    await manager.shutdown()


async def test_capacity_reports_process_and_global_accounting() -> None:
    manager, _ = manager_and_playwright(processes=2, per_browser=3, global_limit=5)
    await manager.start()
    contexts = [await manager.create_context() for _ in range(3)]

    capacity = await manager.capacity()

    assert capacity.chrome_processes == 2
    assert capacity.connected_processes == 2
    assert capacity.active_contexts == 3
    assert capacity.available_contexts == 2
    assert [process.active_contexts for process in capacity.processes] == [2, 1]
    for context in contexts:
        await context.close()
    await manager.shutdown()


async def test_graceful_shutdown_closes_every_resource() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=2, global_limit=4)
    await manager.start()
    contexts = [await manager.create_context() for _ in range(3)]

    await manager.shutdown()

    assert not manager.started
    assert all(context.closed for context in contexts)
    assert all(not browser.connected for browser in playwright.chromium.browsers)
    assert playwright.stopped


async def test_manager_async_context_guarantees_shutdown() -> None:
    manager, playwright = manager_and_playwright()

    async with manager:
        await manager.create_context()

    assert playwright.stopped
    assert playwright.chromium.browsers[0].contexts[0].closed
