from typing import Any

import pytest

from queue_load_test.browser import patchright_preflight as preflight_module
from queue_load_test.browser.patchright_preflight import run_patchright_preflight


async def test_unsupported_patchright_version_fails_without_launch(monkeypatch: Any) -> None:
    monkeypatch.setattr(preflight_module, "_installed", lambda _: "1.62.3")

    def must_not_start() -> object:
        raise AssertionError("an unsupported install must not launch a browser")

    monkeypatch.setattr(preflight_module, "async_playwright", must_not_start)

    result = await run_patchright_preflight()

    assert not result.passed
    assert not result.supported_version
    assert not result.async_launch
    assert "patchright==1.63.0" in result.remedy
    assert result.managed_processes_after_shutdown == 0


async def test_patchright_preflight_rejects_zero_context_cycles() -> None:
    with pytest.raises(ValueError, match="context_cycles"):
        await run_patchright_preflight(context_cycles=0)
