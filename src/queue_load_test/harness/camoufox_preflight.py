"""Local-only Camoufox launch, context, navigation, and cleanup preflight."""

from __future__ import annotations

import argparse
import asyncio
import json

from queue_load_test.browser.preflight import CamoufoxPreflightResult, run_camoufox_preflight

__all__ = ["CamoufoxPreflightResult", "main", "run_preflight"]


async def run_preflight() -> CamoufoxPreflightResult:
    """Exercise Camoufox against a data URL and always release manager resources."""

    return await run_camoufox_preflight()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format (default: human)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(run_preflight())
    if args.format == "json":
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(result.render_human(), end="")
    if not result.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
