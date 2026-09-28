"""Installed-Chrome Patchright probe; uses only an in-memory data URL."""

from queue_load_test.browser.patchright_preflight import run_patchright_preflight


async def test_patchright_disposable_contexts_and_process_cleanup() -> None:
    result = await run_patchright_preflight(context_cycles=3)

    assert result.passed, result.render_human()
    assert result.context_cycles_completed == 3
    assert result.active_contexts_after_close == 0
    assert result.browser_closed
    assert result.patchright_stopped
    assert result.managed_processes_after_shutdown == 0
