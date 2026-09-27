"""Small asyncio helpers shared by persistence and browser boundaries."""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, cast


async def run_to_completion[T](work: asyncio.Future[T]) -> T:
    """Await thread-backed ``work``; if cancelled, let it finish before re-raising.

    A thread started with ``asyncio.to_thread`` cannot be interrupted. Returning
    early on cancellation would release the caller's locks while the thread still
    writes, and would hide whether the write happened.
    """

    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.wait((work,))
            except asyncio.CancelledError:
                continue
        if not work.cancelled():
            work.exception()  # retrieved; the caller is already being cancelled
        raise


class AbandonedOperationError(TimeoutError):
    """A timed-out operation did not stop even after repeated cancellation.

    The task is kept referenced and left to finish in the background; the caller
    must treat the underlying resource (for example a browser process) as wedged.
    """


# Strong references keep abandoned tasks alive until they finish; the event loop
# only holds weak references to tasks.
_ABANDONED: set[asyncio.Task[object]] = set()


async def await_bounded[T](
    work: Awaitable[T],
    *,
    timeout: float,
    cancel_grace: float = 1.0,
    cancel_attempts: int = 5,
    discard: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Return ``work``'s result, or raise ``TimeoutError`` after ``timeout`` seconds.

    ``asyncio.timeout``/``wait_for`` cancel exactly once and then wait for the work
    to finish cancelling. Playwright 1.62 reacts to that first cancellation by
    sending an abort and waiting, unbounded, for the browser to acknowledge it, so
    a wedged browser process keeps the caller waiting forever. Here the work runs
    as a task that is cancelled repeatedly (each re-cancellation interrupts that
    acknowledgement wait). If it still has not stopped it is abandoned and
    ``AbandonedOperationError`` is raised; a result that arrives after the
    deadline is passed to ``discard`` so resources such as contexts are released.
    """

    task: asyncio.Task[T] = asyncio.ensure_future(work)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await _stop(task, cancel_grace, cancel_attempts, discard)
        raise
    if task in done:
        return task.result()
    if await _stop(task, cancel_grace, cancel_attempts, discard):
        raise TimeoutError(f"operation exceeded {timeout:g}s")
    raise AbandonedOperationError(f"operation exceeded {timeout:g}s and did not stop")


async def _stop[T](
    task: asyncio.Task[T],
    grace: float,
    attempts: int,
    discard: Callable[[T], Awaitable[None]] | None,
) -> bool:
    """Cancel ``task`` until it finishes; return False if it had to be abandoned."""

    try:
        for _ in range(max(1, attempts)):
            task.cancel()
            done, _ = await asyncio.shield(asyncio.wait({task}, timeout=grace))
            if done:
                await _settle(task, discard)
                return True
    except asyncio.CancelledError:
        if not task.done():
            _abandon(task, discard)
        raise
    _abandon(task, discard)
    return False


def _abandon[T](task: asyncio.Task[T], discard: Callable[[T], Awaitable[None]] | None) -> None:
    abandoned = cast(asyncio.Task[object], task)
    _ABANDONED.add(abandoned)

    def forget(finished: asyncio.Task[object]) -> None:
        _ABANDONED.discard(finished)
        late = _late_result(finished)
        if late is not None and discard is not None:
            cleanup = asyncio.ensure_future(discard(cast(T, late)))
            _ABANDONED.add(cast(asyncio.Task[object], cleanup))
            cleanup.add_done_callback(_ABANDONED.discard)

    abandoned.add_done_callback(forget)


async def _settle[T](task: asyncio.Task[T], discard: Callable[[T], Awaitable[None]] | None) -> None:
    late = _late_result(task)
    if late is not None and discard is not None:
        with contextlib.suppress(Exception):
            await discard(cast(T, late))


def _late_result(task: asyncio.Task[Any]) -> Any | None:
    """Retrieve a finished task's outcome; return its result only if it succeeded."""

    if task.cancelled():
        return None
    if task.exception() is not None:
        return None
    return task.result()
