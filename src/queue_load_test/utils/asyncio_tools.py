"""Small asyncio helpers shared by persistence boundaries."""

import asyncio


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
