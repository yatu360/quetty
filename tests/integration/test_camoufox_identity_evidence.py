"""Local evidence for the Camoufox 0.5.6 reusable-identity gate.

These tests use only ``data:`` documents and an HTTP server bound to 127.0.0.1.
They never contact Queue-it or any staging environment.  A missing pinned Camoufox
browser is an explicit local prerequisite and causes these evidence tests to skip.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from camoufox import launch_options
from camoufox.async_api import AsyncNewBrowser, AsyncNewContext
from camoufox.fingerprints import get_random_preset
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from queue_load_test.browser import CAMOUFOX_BROWSER_VERSION

_IDENTITY_SCRIPT = """() => {
    const canvas = document.createElement("canvas");
    canvas.width = 240;
    canvas.height = 80;
    const context = canvas.getContext("2d");
    context.textBaseline = "top";
    context.font = "16px Arial";
    context.fillStyle = "#f60";
    context.fillRect(20, 10, 100, 35);
    context.fillStyle = "#069";
    context.fillText("Quetty identity 123", 2, 2);
    context.strokeStyle = "rgba(102, 204, 0, .7)";
    context.arc(80, 30, 20, 0, Math.PI * 2);
    context.stroke();
    const webgl = document.createElement("canvas").getContext("webgl");
    const debug = webgl && webgl.getExtension("WEBGL_debug_renderer_info");
    return {
        userAgent: navigator.userAgent,
        platform: navigator.platform,
        oscpu: navigator.oscpu,
        hardwareConcurrency: navigator.hardwareConcurrency,
        screen: [
            screen.width,
            screen.height,
            screen.availWidth,
            screen.availHeight,
            screen.colorDepth,
        ],
        webglVendor: debug && webgl.getParameter(debug.UNMASKED_VENDOR_WEBGL),
        webglRenderer: debug && webgl.getParameter(debug.UNMASKED_RENDERER_WEBGL),
        canvas: canvas.toDataURL(),
    };
}"""


class _LocalStateHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<!doctype html><title>local browser state</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


@contextmanager
def _local_page() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalStateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _camoufox(playwright: Playwright) -> Browser:
    try:
        browser = await AsyncNewBrowser(
            playwright,
            headless=True,
            browser=CAMOUFOX_BROWSER_VERSION,
        )
    except ValueError as exc:
        if "not found" in str(exc):
            pytest.skip(f"pinned Camoufox browser is not installed: {CAMOUFOX_BROWSER_VERSION}")
        raise
    return cast(Browser, browser)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _observe(context: BrowserContext) -> tuple[str, str]:
    page = await context.new_page()
    await page.goto("data:text/html,<title>local identity</title>")
    observed = cast(dict[str, object], await page.evaluate(_IDENTITY_SCRIPT))
    await page.close()
    canvas = observed.pop("canvas")
    return _digest(observed), _digest(canvas)


async def _preset_observations(
    browser: Browser,
    preset: dict[str, Any],
    *,
    cycles: int,
) -> AsyncIterator[tuple[str, str]]:
    for _ in range(cycles):
        context = await AsyncNewContext(browser, preset=preset)
        try:
            yield await _observe(context)
        finally:
            await context.close()


async def test_reused_public_context_preset_is_not_a_complete_stable_identity() -> None:
    """The same preset keeps core fields but redraws an observable canvas seed."""

    async with async_playwright() as playwright:
        browser = await _camoufox(playwright)
        try:
            preset = get_random_preset(
                os="macos",
                ff_version=browser.version.split(".", 1)[0],
            )
            assert preset is not None
            observations = [
                item
                async for item in _preset_observations(browser, preset, cycles=20)
            ]
        finally:
            await browser.close()

    assert len({core for core, _ in observations}) == 1
    assert len({canvas for _, canvas in observations}) > 1


async def test_preset_disk_round_trip_and_full_runtime_restart_change_identity(
    tmp_path: Path,
) -> None:
    """Disk persistence plus a new Playwright runtime does not preserve identity."""

    async with async_playwright() as playwright:
        first_browser = await _camoufox(playwright)
        try:
            preset = get_random_preset(
                os="macos",
                ff_version=first_browser.version.split(".", 1)[0],
            )
            assert preset is not None
            first_context = await AsyncNewContext(first_browser, preset=preset)
            try:
                first = await _observe(first_context)
            finally:
                await first_context.close()
        finally:
            await first_browser.close()

    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(preset), encoding="utf-8")
    persisted = cast(dict[str, Any], json.loads(identity_path.read_text(encoding="utf-8")))

    async with async_playwright() as playwright:
        second_browser = await _camoufox(playwright)
        try:
            second_context = await AsyncNewContext(second_browser, preset=persisted)
            try:
                second = await _observe(second_context)
            finally:
                await second_context.close()
        finally:
            await second_browser.close()

    assert first != second


async def test_public_launch_options_replay_is_stable_but_process_scoped() -> None:
    """The alternative public replay input is stable only at browser-process scope."""

    options = launch_options(
        headless=True,
        browser=CAMOUFOX_BROWSER_VERSION,
        env={},
    )
    # The clean environment prevents launch_options from copying unrelated process
    # credentials into the reusable input.  Its identity is still encoded in an
    # implementation-owned CAMOU_CONFIG environment value.
    assert set(options["env"]) and all(
        key.startswith("CAMOU_CONFIG_") for key in options["env"]
    )
    json.dumps(options)

    observations: list[tuple[str, str]] = []
    async with async_playwright() as playwright:
        for _ in range(2):
            launched = await AsyncNewBrowser(playwright, from_options=options)
            browser = cast(Browser, launched)
            try:
                context = await browser.new_context()
                try:
                    observations.append(await _observe(context))
                finally:
                    await context.close()
            finally:
                await browser.close()

    assert observations[0] == observations[1]


async def _new_context(
    backend: str,
    browser: Browser,
    state: dict[str, Any] | None = None,
) -> BrowserContext:
    if backend == "chrome":
        return await browser.new_context(storage_state=state) if state else await browser.new_context()
    return (
        await AsyncNewContext(browser, storage_state=state)
        if state
        else await AsyncNewContext(browser)
    )


async def _capture_state(backend: str, browser: Browser, url: str) -> dict[str, Any]:
    context = await _new_context(backend, browser)
    try:
        page = await context.new_page()
        await page.goto(url)
        await page.evaluate(
            """() => {
                document.cookie = "quetty_cookie=cookie-value; path=/";
                localStorage.setItem("quetty_local", "local-value");
                sessionStorage.setItem("quetty_session", "session-value");
            }"""
        )
        return cast(dict[str, Any], await context.storage_state())
    finally:
        await context.close()


async def _restored_values(
    backend: str,
    browser: Browser,
    url: str,
    state: dict[str, Any],
) -> dict[str, str | None]:
    context = await _new_context(backend, browser, state)
    try:
        page = await context.new_page()
        await page.goto(url)
        return cast(
            dict[str, str | None],
            await page.evaluate(
                """() => ({
                    cookie: document.cookie,
                    local: localStorage.getItem("quetty_local"),
                    session: sessionStorage.getItem("quetty_session"),
                })"""
            ),
        )
    finally:
        await context.close()


async def test_local_cross_engine_storage_state_matrix() -> None:
    """Verify meaningful cookie/local-storage restoration, not mere JSON acceptance."""

    with _local_page() as url:
        async with async_playwright() as playwright:
            chrome = await playwright.chromium.launch(channel="chrome", headless=True)
            camoufox = await _camoufox(playwright)
            browsers = {"chrome": chrome, "camoufox": camoufox}
            try:
                states = {
                    backend: await _capture_state(backend, browser, url)
                    for backend, browser in browsers.items()
                }
                matrix = {
                    (source, destination): await _restored_values(
                        destination,
                        browsers[destination],
                        url,
                        states[source],
                    )
                    for source, destination in (
                        ("camoufox", "camoufox"),
                        ("chrome", "camoufox"),
                        ("camoufox", "chrome"),
                        ("chrome", "chrome"),
                    )
                }
            finally:
                await camoufox.close()
                await chrome.close()

    for direction in (
        ("camoufox", "camoufox"),
        ("chrome", "camoufox"),
        ("chrome", "chrome"),
    ):
        assert matrix[direction] == {
            "cookie": "quetty_cookie=cookie-value",
            "local": "local-value",
            "session": None,
        }
    assert matrix[("camoufox", "chrome")] == {
        "cookie": "",
        "local": "local-value",
        "session": None,
    }
