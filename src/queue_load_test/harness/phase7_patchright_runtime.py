"""Phase 7 full Patchright runtime/dashboard workflow with a Chrome control.

Both workflows use only :class:`LocalQueueSimulator`. The report contains aggregate
checks and timings, never Queue IDs, transfer URLs, or browser-state contents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from queue_load_test.harness.phase5_workflow import run_workflow
from queue_load_test.models import BrowserBackendName


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    checks = result["checks"]
    return {
        "passed": result["passed"],
        "failed": result["failed"],
        "scenario_checks": len(checks),
        "failed_checks": [check["name"] for check in checks if not check["passed"]],
        "evidence": result["evidence"],
        "checks": checks,
    }


async def run_phase7_runtime_evidence(
    directory: Path,
    *,
    patchright_headed: bool = True,
    chrome_headed: bool = False,
) -> dict[str, object]:
    """Run the candidate and control through the same extended application workflow."""

    directory.mkdir(parents=True, exist_ok=True)
    patchright = await run_workflow(
        directory / "patchright",
        headed=patchright_headed,
        backend=BrowserBackendName.PATCHRIGHT,
        phase7_recovery=True,
    )
    chrome = await run_workflow(
        directory / "chrome",
        headed=chrome_headed,
        backend=BrowserBackendName.CHROME,
        phase7_recovery=True,
    )
    patchright_summary = _summary(patchright)
    chrome_summary = _summary(chrome)
    passed = int(patchright_summary["passed"]) + int(chrome_summary["passed"])
    failed = int(patchright_summary["failed"]) + int(chrome_summary["failed"])
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "local_queue_simulator_only",
        "staging": "NOT_RUN_UNKNOWN",
        "camoufox_phase7_workflow": "EXCLUDED",
        "decision": "PASS" if failed == 0 else "FAIL",
        "scenario_checks": passed + failed,
        "passed": passed,
        "failed": failed,
        "patchright": patchright_summary,
        "chrome_control": chrome_summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patchright-headless",
        action="store_true",
        help="hide the Patchright manual window (headed is the evidence default)",
    )
    parser.add_argument(
        "--chrome-headed",
        action="store_true",
        help="show Chrome control creation/manual windows",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with tempfile.TemporaryDirectory(prefix="phase7-runtime-") as temporary:
        result = asyncio.run(
            run_phase7_runtime_evidence(
                Path(temporary),
                patchright_headed=not args.patchright_headless,
                chrome_headed=args.chrome_headed,
            )
        )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(f"{result['passed']} passed, {result['failed']} failed")
    raise SystemExit(0 if result["failed"] == 0 else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
