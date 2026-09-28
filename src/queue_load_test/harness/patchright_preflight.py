"""Local-only Patchright launch, temporary-context, navigation, and cleanup probe."""

from __future__ import annotations

import argparse
import asyncio
import json

from queue_load_test.browser.patchright_preflight import (
    PatchrightPreflightResult,
    run_patchright_preflight,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument("--context-cycles", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result: PatchrightPreflightResult = asyncio.run(
        run_patchright_preflight(context_cycles=args.context_cycles)
    )
    if args.format == "json":
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        print(result.render_human(), end="")
    if not result.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
