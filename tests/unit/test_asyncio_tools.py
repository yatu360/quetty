import asyncio

import pytest

from queue_load_test.utils.asyncio_tools import AbandonedOperationError, await_bounded


async def _playwright_like_call(finished: list[str]) -> str:
    """Mimic Playwright 1.62: on cancel, wait unbounded for an abort acknowledgement."""

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        try:
            await asyncio.Event().wait()  # the browser never acknowledges the abort
        finally:
            finished.append("stopped")
        raise
    return "unreachable"


async def test_single_cancellation_timeout_hangs_on_playwright_like_abort() -> None:
    finished: list[str] = []
    task = asyncio.ensure_future(_playwright_like_call(finished))
    await asyncio.sleep(0)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=0.1)
    # This is the observed shutdown hang: one cancellation cannot stop the call.
    assert not done
    task.cancel()
    await asyncio.wait({task}, timeout=1)
    assert finished == ["stopped"]


async def test_await_bounded_stops_a_playwright_like_call_by_recancelling() -> None:
    finished: list[str] = []
    started = asyncio.get_running_loop().time()

    with pytest.raises(TimeoutError) as raised:
        await await_bounded(_playwright_like_call(finished), timeout=0.05, cancel_grace=0.05)

    assert not isinstance(raised.value, AbandonedOperationError)
    assert finished == ["stopped"]
    assert asyncio.get_running_loop().time() - started < 1


async def test_await_bounded_abandons_uncancellable_work_and_discards_late_results() -> None:
    release = asyncio.Event()
    discarded: list[str] = []

    async def stubborn() -> str:
        while True:
            try:
                await release.wait()
                return "late-context"
            except asyncio.CancelledError:
                continue  # ignores every cancellation

    async def discard(value: str) -> None:
        discarded.append(value)

    with pytest.raises(AbandonedOperationError):
        await await_bounded(
            stubborn(), timeout=0.02, cancel_grace=0.01, cancel_attempts=2, discard=discard
        )
    release.set()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if discarded:
            break
    assert discarded == ["late-context"]


async def test_await_bounded_returns_results_and_propagates_errors() -> None:
    async def value() -> int:
        return 7

    async def failing() -> int:
        raise ValueError("boom")

    assert await await_bounded(value(), timeout=1) == 7
    with pytest.raises(ValueError, match="boom"):
        await await_bounded(failing(), timeout=1)
