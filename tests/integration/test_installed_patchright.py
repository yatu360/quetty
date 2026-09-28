"""Installed-Chrome Patchright probes; use only in-memory data URLs."""

from queue_load_test.browser import BrowserManager, PatchrightBackend
from queue_load_test.browser.patchright_preflight import run_patchright_preflight


async def test_patchright_disposable_contexts_and_process_cleanup() -> None:
    result = await run_patchright_preflight(context_cycles=3)

    assert result.passed, result.render_human()
    assert result.context_cycles_completed == 3
    assert result.active_contexts_after_close == 0
    assert result.browser_closed
    assert result.patchright_stopped
    assert result.managed_processes_after_shutdown == 0


async def test_patchright_backend_runs_through_browser_manager_and_cleans_up() -> None:
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=2,
        max_active_contexts=2,
        backend=PatchrightBackend(),
    )

    await manager.start()
    first = await manager.create_context()
    second = await manager.create_context()
    try:
        first_page = await first.context.new_page()
        second_page = await second.context.new_page()
        await first_page.goto("data:text/html,<title>patchright-manager-one</title>")
        await second_page.goto("data:text/html,<title>patchright-manager-two</title>")
        assert await first_page.title() == "patchright-manager-one"
        assert await second_page.title() == "patchright-manager-two"
        assert manager.active_context_count == 2
        assert manager.managed_process_count == 1
        assert manager.backend_diagnostics().package_version == "1.63.0"
    finally:
        await first.close()
        await second.close()
        await manager.shutdown()

    assert manager.active_context_count == 0
    assert manager.managed_process_count == 0
