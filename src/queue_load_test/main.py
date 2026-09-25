"""Async application entry point for Phase 1 validation."""

import asyncio

from queue_load_test.config import Settings, get_settings


async def run(settings: Settings) -> None:
    """Run the application.

    Phase 1 deliberately stops after validating configuration. Later phases can
    add async browser and worker lifecycles here without changing the CLI entry
    point.
    """

    del settings


def main() -> None:
    """Validate configuration and enter the asyncio runtime."""

    asyncio.run(run(get_settings()))


if __name__ == "__main__":
    main()
