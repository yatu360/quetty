"""Async application entry point for Phase 1 validation."""

import asyncio

from queue_load_test.config import Settings, get_settings
from queue_load_test.runtime import ApplicationRuntime


async def run(settings: Settings, runtime: ApplicationRuntime | None = None) -> None:
    """Validate settings and run an assembled application runtime when supplied."""

    if runtime is None:
        del settings
        return
    await runtime.run()


def main() -> None:
    """Validate configuration and enter the asyncio runtime."""

    asyncio.run(run(get_settings()))


if __name__ == "__main__":
    main()
