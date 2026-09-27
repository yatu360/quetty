import asyncio
import contextlib
from typing import Any, cast

import pytest

from queue_load_test.browser import (
    BrowserCapacityError,
    BrowserContextCapacity,
    BrowserManager,
)
from queue_load_test.browser.manager import PlaywrightStarter
from queue_load_test.metrics import PrometheusMetrics


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
        self.fail_next_context = False

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **options: object) -> FakeContext:
        if not self.connected:
            raise RuntimeError("browser process is disconnected")
        if self.fail_next_context:
            self.fail_next_context = False
            raise RuntimeError("context creation failed")
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
        self.block_launch_call: int | None = None
        self.fail_launch_call: int | None = None
        self.launch_started = asyncio.Event()
        self.continue_launch = asyncio.Event()

    async def launch(self, **options: object) -> FakeBrowser:
        self.launch_calls.append(options)
        if len(self.launch_calls) == self.fail_launch_call:
            raise RuntimeError("browser launch failed")
        if len(self.launch_calls) == self.block_launch_call:
            self.launch_started.set()
            await self.continue_launch.wait()
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
    observability: PrometheusMetrics | None = None,
    headless: bool = True,
    shared_capacity: BrowserContextCapacity | None = None,
) -> tuple[BrowserManager, FakePlaywright]:
    playwright = FakePlaywright()

    async def starter() -> Any:
        return playwright

    manager = BrowserManager(
        chrome_process_count=processes,
        max_contexts_per_browser=per_browser,
        max_active_contexts=global_limit,
        playwright_starter=cast(PlaywrightStarter, starter),
        observability=observability,
        headless=headless,
        shared_capacity=shared_capacity,
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


async def test_launch_failure_does_not_leak_process_slot() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=2, global_limit=4)
    playwright.chromium.fail_launch_call = 2

    with pytest.raises(RuntimeError, match="browser launch failed"):
        await manager.start()

    assert not manager.started
    assert manager.managed_process_count == 0
    assert playwright.stopped
    assert not playwright.chromium.browsers[0].connected


async def test_shutdown_is_idempotent() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()

    await manager.shutdown()
    await manager.shutdown()

    assert not manager.started
    assert manager.managed_process_count == 0
    assert playwright.stopped


async def test_manager_starts_one_google_chrome_process() -> None:
    manager, playwright = manager_and_playwright(processes=1, per_browser=25, global_limit=25)

    await manager.start()

    assert len(playwright.chromium.browsers) == 1
    assert playwright.chromium.launch_calls == [{"channel": "chrome", "headless": True}]
    await manager.shutdown()


async def test_manual_manager_launches_visible_installed_google_chrome() -> None:
    manager, playwright = manager_and_playwright(headless=False)

    await manager.start()

    assert playwright.chromium.launch_calls == [{"channel": "chrome", "headless": False}]
    await manager.shutdown()


async def test_headed_and_headless_pools_share_one_global_context_budget() -> None:
    shared = BrowserContextCapacity(2)
    automatic, _ = manager_and_playwright(global_limit=2, shared_capacity=shared)
    manual, _ = manager_and_playwright(
        global_limit=2,
        headless=False,
        shared_capacity=shared,
    )
    await automatic.start()
    await manual.start()
    automatic_context = await automatic.create_context()
    manual_context = await manual.create_context()

    with pytest.raises(BrowserCapacityError, match="Global"):
        await automatic.create_context()
    assert shared.active == 2

    await manual_context.close()
    assert shared.active == 1
    replacement = await automatic.create_context()
    await replacement.close()
    await automatic_context.close()
    assert shared.active == 0
    await manual.shutdown()
    await automatic.shutdown()


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


async def test_context_reports_creation_and_allocation_wait_duration() -> None:
    metrics = PrometheusMetrics()
    manager, _ = manager_and_playwright(observability=metrics)
    await manager.start()

    owned = await manager.create_context()

    assert owned.creation_duration_seconds >= 0
    assert owned.acquisition_wait_seconds >= 0
    assert (
        metrics.registry.get_sample_value(
            "browser_context_creation_duration_seconds_count"
        )
        == 1
    )
    assert (
        metrics.registry.get_sample_value(
            "browser_context_acquisition_wait_seconds_count"
        )
        == 1
    )
    await owned.close()
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


async def test_phase2_limit_is_balanced_across_two_browsers() -> None:
    manager, _ = manager_and_playwright(processes=2, per_browser=13, global_limit=25)
    await manager.start()

    contexts = [await manager.create_context() for _ in range(25)]

    assert [context.browser_id for context in contexts].count(0) == 13
    assert [context.browser_id for context in contexts].count(1) == 12
    capacity = await manager.capacity()
    assert [process.active_contexts for process in capacity.processes] == [13, 12]
    assert capacity.active_contexts == 25
    assert capacity.available_contexts == 0
    with pytest.raises(BrowserCapacityError, match="Global"):
        await manager.create_context()

    for context in contexts:
        await context.close()
    await manager.shutdown()


async def test_phase3_one_hundred_context_cap_is_bounded_across_four_browsers() -> None:
    manager, _ = manager_and_playwright(processes=4, per_browser=25, global_limit=100)
    await manager.start()

    contexts = [await manager.create_context() for _ in range(100)]
    capacity = await manager.capacity()

    assert capacity.chrome_processes == 4
    assert capacity.active_contexts == 100
    assert capacity.available_contexts == 0
    assert [process.active_contexts for process in capacity.processes] == [25, 25, 25, 25]
    with pytest.raises(BrowserCapacityError, match="Global"):
        await manager.create_context()

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
    metrics = PrometheusMetrics()
    manager, playwright = manager_and_playwright(observability=metrics)
    await manager.start()
    owned = await manager.create_context()
    playwright.chromium.browsers[0].contexts[0].fail_close = True

    await owned.close()

    assert owned.closed
    assert (await manager.capacity()).active_contexts == 0
    assert metrics.registry.get_sample_value("browser_cleanup_failures_total") == 1
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


async def test_context_creation_failure_does_not_leak_capacity() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()
    playwright.chromium.browsers[0].fail_next_context = True

    with pytest.raises(RuntimeError, match="context creation failed"):
        await manager.create_context()

    capacity = await manager.capacity()
    assert capacity.active_contexts == 0
    assert capacity.available_contexts == 5
    replacement_attempt = await manager.create_context()
    await replacement_attempt.close()
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
    assert replacement_context.browser_id == 0
    assert replacement_context.context is playwright.chromium.browsers[1].contexts[0]
    await replacement_context.close()
    await manager.shutdown()


async def test_browser_crash_releases_shared_context_capacity() -> None:
    shared = BrowserContextCapacity(1)
    manager, playwright = manager_and_playwright(
        global_limit=1,
        shared_capacity=shared,
    )
    await manager.start()
    lost_context = await manager.create_context()
    assert shared.active == 1
    playwright.chromium.browsers[0].connected = False

    assert await manager.restart_failed_browsers() == 1

    assert lost_context.closed
    assert shared.active == 0
    replacement = await manager.create_context()
    await replacement.close()
    await manager.shutdown()


async def test_read_only_capacity_snapshot_does_not_restart_dead_browser() -> None:
    manager, playwright = manager_and_playwright()
    await manager.start()
    playwright.chromium.browsers[0].connected = False

    capacity = await manager.capacity(repair=False)

    assert capacity.connected_processes == 0
    assert len(playwright.chromium.browsers) == 1
    assert manager.restart_count == 0
    await manager.shutdown()


async def test_healthy_browser_remains_usable_while_failed_browser_restarts() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=2, global_limit=4)
    await manager.start()
    failed_context = await manager.create_context()
    healthy_context = await manager.create_context()
    failed_browser = playwright.chromium.browsers[0]
    healthy_browser = playwright.chromium.browsers[1]
    failed_browser.connected = False
    playwright.chromium.block_launch_call = 3

    restart = asyncio.create_task(manager.restart_failed_browsers())
    await asyncio.wait_for(playwright.chromium.launch_started.wait(), timeout=1)
    context_during_restart = await asyncio.wait_for(manager.create_context(), timeout=1)

    assert context_during_restart.browser_id == 1
    assert healthy_browser.connected
    assert healthy_context.context in healthy_browser.contexts
    assert failed_context.closed

    playwright.chromium.continue_launch.set()
    assert await restart == 1
    capacity = await manager.capacity()
    assert capacity.connected_processes == 2
    assert capacity.active_contexts == 2
    assert capacity.available_contexts == 2

    await context_during_restart.close()
    await healthy_context.close()
    await manager.shutdown()


async def test_restart_replaces_only_the_failed_browser_and_restores_capacity() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=3, global_limit=5)
    await manager.start()
    failed_context = await manager.create_context()
    healthy_context = await manager.create_context()
    failed_browser = playwright.chromium.browsers[0]
    healthy_browser = playwright.chromium.browsers[1]
    failed_browser.connected = False

    assert await manager.restart_failed_browsers() == 1

    capacity = await manager.capacity()
    assert failed_context.closed
    assert playwright.chromium.browsers[1] is healthy_browser
    assert healthy_browser.connected
    assert len(playwright.chromium.browsers) == 3
    assert capacity.connected_processes == 2
    assert capacity.active_contexts == 1
    assert capacity.available_contexts == 4

    await healthy_context.close()
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


async def test_graceful_shutdown_waits_for_in_progress_restart_without_leaks() -> None:
    manager, playwright = manager_and_playwright(processes=2, per_browser=2, global_limit=4)
    await manager.start()
    contexts = [await manager.create_context() for _ in range(2)]
    playwright.chromium.browsers[0].connected = False
    playwright.chromium.block_launch_call = 3
    restart = asyncio.create_task(manager.restart_failed_browsers())
    await asyncio.wait_for(playwright.chromium.launch_started.wait(), timeout=1)

    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    playwright.chromium.continue_launch.set()

    assert await restart == 0
    await shutdown
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


async def test_repeated_crashes_record_restart_duration_and_lost_contexts() -> None:
    metrics = PrometheusMetrics()
    manager, playwright = manager_and_playwright(
        processes=2,
        per_browser=3,
        global_limit=6,
        observability=metrics,
    )
    await manager.start()
    healthy = await manager.create_context()
    for crash in range(3):
        lost = [await manager.create_context() for _ in range(2)]
        victim = next(
            browser
            for browser in playwright.chromium.browsers
            if browser.connected and healthy.context not in browser.contexts
        )
        victim.connected = False

        assert await manager.restart_failed_browsers() == 1
        assert all(context.closed for context in lost if context.browser_id != healthy.browser_id)
        for context in lost:
            await context.close()
        assert not healthy.closed
        assert manager.restart_count == crash + 1

    capacity = await manager.capacity()
    assert capacity.connected_processes == 2
    assert capacity.active_contexts == 1
    assert len(manager.restart_durations) == 3
    assert metrics.registry.get_sample_value("browser_crashes_total") == 3
    assert metrics.registry.get_sample_value("browser_restart_duration_seconds_count") == 3
    assert metrics.registry.get_sample_value("browser_restart_failures_total") == 0
    assert (metrics.registry.get_sample_value("browser_contexts_lost_total") or 0) >= 3
    await healthy.close()
    await manager.shutdown()
    assert (await_capacity_after_shutdown(manager)) == 0


def await_capacity_after_shutdown(manager: BrowserManager) -> int:
    return sum(len(slot.contexts) for slot in manager._slots)


async def test_cancelled_close_under_lock_contention_releases_capacity() -> None:
    manager, _ = manager_and_playwright()
    await manager.start()
    owned = await manager.create_context()
    release_lock = asyncio.Event()

    async def hold_allocation_lock() -> None:
        async with manager._lock:
            await release_lock.wait()

    holder = asyncio.create_task(hold_allocation_lock())
    await asyncio.sleep(0)
    closing = asyncio.create_task(owned.close())
    await asyncio.sleep(0)
    closing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await closing
    release_lock.set()
    await holder

    assert owned.closed
    assert (await manager.capacity()).active_contexts == 0
    await manager.shutdown()


class HangingBrowser(FakeBrowser):
    def __init__(self, process_id: int) -> None:
        super().__init__(process_id)
        self.hang_new_context = False
        self.hang_close = False

    async def new_context(self, **options: object) -> FakeContext:
        if self.hang_new_context:
            await asyncio.Event().wait()
        return await super().new_context(**options)


class HangingContext(FakeContext):
    async def close(self) -> None:
        await asyncio.Event().wait()


async def test_hung_context_creation_is_bounded_and_releases_lock() -> None:
    manager, _ = manager_and_playwright()
    manager._operation_timeout_seconds = 0.05
    await manager.start()
    hanging = HangingBrowser(0)
    hanging.hang_new_context = True
    manager._slots[0].browser = cast(Any, hanging)

    with pytest.raises(TimeoutError):
        await manager.create_context()

    hanging.hang_new_context = False
    recovered = await asyncio.wait_for(manager.create_context(), timeout=1)
    await recovered.close()
    await manager.shutdown()


async def test_hung_context_close_is_bounded() -> None:
    metrics = PrometheusMetrics()
    manager, _ = manager_and_playwright(observability=metrics)
    manager._close_timeout_seconds = 0.05
    await manager.start()
    owned = await manager.create_context()
    owned._context = cast(Any, HangingContext({}))

    await asyncio.wait_for(owned.close(), timeout=1)

    assert (await manager.capacity()).active_contexts == 0
    assert metrics.registry.get_sample_value("browser_cleanup_failures_total") == 1
    await manager.shutdown()
