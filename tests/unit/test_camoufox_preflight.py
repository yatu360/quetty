from typing import Any

from queue_load_test.browser import CAMOUFOX_BROWSER_VERSION
from queue_load_test.browser import backend as backend_module
from queue_load_test.browser import preflight as preflight_module
from queue_load_test.browser.preflight import CAMOUFOX_FETCH_COMMAND, run_camoufox_preflight
from queue_load_test.metrics import PrometheusMetrics


async def test_unsupported_package_versions_fail_without_launching(monkeypatch: Any) -> None:
    versions = {"camoufox": "0.5.7", "playwright": "1.62.0"}
    monkeypatch.setattr(preflight_module, "_installed", versions.get)

    async def must_not_launch(*_: object, **__: object) -> object:
        raise AssertionError("an unsupported install must not launch a browser")

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", must_not_launch)

    result = await run_camoufox_preflight()

    assert not result.passed
    assert not result.supported_versions
    assert not result.async_launch
    assert "camoufox==0.5.6" in result.remedy
    assert result.managed_processes_after_shutdown == 0


async def test_missing_browser_build_names_the_exact_fetch_command(monkeypatch: Any) -> None:
    async def missing(*_: object, **__: object) -> object:
        raise ValueError(f"Browser version '{CAMOUFOX_BROWSER_VERSION}' not found")

    monkeypatch.setattr(backend_module, "AsyncNewBrowser", missing)

    result = await run_camoufox_preflight()

    assert not result.passed
    assert result.supported_versions
    assert not result.browser_installed
    assert CAMOUFOX_FETCH_COMMAND in result.remedy
    assert CAMOUFOX_FETCH_COMMAND in result.render_human()
    assert result.managed_processes_after_shutdown == 0


def test_browser_backend_info_is_one_low_cardinality_series() -> None:
    metrics = PrometheusMetrics()
    metrics.set_browser_backend("chrome", browser_build="153.0")
    metrics.set_browser_backend("camoufox", browser_build=CAMOUFOX_BROWSER_VERSION)

    samples = [
        sample
        for family in metrics.registry.collect()
        if family.name == "browser_backend_info"
        for sample in family.samples
    ]

    assert len(samples) == 1
    assert samples[0].labels == {"backend": "camoufox", "browser_build": CAMOUFOX_BROWSER_VERSION}
    assert set(samples[0].labels) == {"backend", "browser_build"}
