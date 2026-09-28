"""Phase 7 final acceptance: Patchright default with a Chrome fallback regression.

The run composes existing evidence harnesses; it adds no new scenario logic:

1. local preflights: Patchright (exact package, installed Chrome channel) and a Chrome
   fallback launch/context/close check;
2. the complete application/dashboard workflow (Phase 7 extended) on Patchright with a
   visible headed manual window, and the same workflow on Chrome as the fallback;
3. the Prompt 5 restoration/recovery scenarios that the workflow does not cover
   (repeated park/reopen, application restart with stale leases, crash during
   monitoring, state/navigation/identity faults, stuck navigation, shutdown matrix) plus
   the default-ceiling concurrency case, for both backends.

Everything targets :class:`LocalQueueSimulator` on 127.0.0.1. Camoufox is excluded.
The report is aggregate and contains no Queue IDs, transfer URLs, or browser state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from queue_load_test.browser import ChromeBackend
from queue_load_test.browser.patchright_preflight import run_patchright_preflight
from queue_load_test.harness.phase6_camoufox_benchmark import _json_default, main_pids, until
from queue_load_test.harness.phase7_patchright_benchmark import (
    MonitoringProfile,
    run_phase7_benchmark,
)
from queue_load_test.harness.phase7_patchright_runtime import run_phase7_runtime_evidence
from queue_load_test.models import BrowserBackendName

ACCEPTANCE_SCENARIOS = frozenset(
    {
        "concurrency",
        "park_reopen",
        "application_restart",
        "failure_during_monitoring",
        "restoration_failures",
        "stuck_navigation",
        "shutdown_matrix",
    }
)


async def chrome_fallback_preflight() -> dict[str, object]:
    """Launch installed Chrome via Playwright, use one data: context, and clean up."""

    from queue_load_test.browser import BrowserManager

    baseline = main_pids(BrowserBackendName.CHROME)
    manager = BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=1,
        max_active_contexts=1,
        backend=ChromeBackend(),
        operation_timeout_seconds=30.0,
    )
    started = time.perf_counter()
    version: str | None = None
    navigated = False
    error: str | None = None
    try:
        await manager.start()
        version = manager.backend_diagnostics().browser_version
        async with manager.context() as context:
            page = await context.new_page()
            await page.goto("data:text/html,<p>chrome-fallback</p>")
            navigated = (await page.inner_text("p")) == "chrome-fallback"
        contexts_after = manager.active_context_count
    except Exception as exc:  # noqa: BLE001 - recorded preflight failure
        error = type(exc).__name__
        contexts_after = manager.active_context_count if manager.started else 0
    finally:
        await manager.shutdown()
    gone = await until(lambda: not (main_pids(BrowserBackendName.CHROME) - baseline), timeout=10)
    return {
        "passed": error is None and navigated and contexts_after == 0 and gone is not None,
        "error": error,
        "observed_browser_version": version,
        "contexts_after": contexts_after,
        "processes_after_shutdown": 0 if gone is not None else "leaked",
        "duration_seconds": round(time.perf_counter() - started, 3),
    }


def _workflow_counts(summary: dict[str, Any]) -> dict[str, object]:
    return {
        "passed": summary["passed"],
        "failed": summary["failed"],
        "failed_checks": summary["failed_checks"],
    }


async def run_phase7_acceptance(directory: Path, *, headed: bool) -> dict[str, Any]:
    started = time.perf_counter()
    patchright = await run_patchright_preflight(context_cycles=5)
    chrome = await chrome_fallback_preflight()
    workflows = await run_phase7_runtime_evidence(
        directory / "workflow", patchright_headed=headed, chrome_headed=False
    )
    scenarios = await run_phase7_benchmark(
        directory / "scenarios",
        backends=[BrowserBackendName.PATCHRIGHT, BrowserBackendName.CHROME],
        single_process_levels=(),
        multi_process_levels=(50,),
        navigation_waves=3,
        hold_seconds=5.0,
        sample_interval_seconds=0.5,
        churn_levels=(1,),
        churn_cycles=1,
        population_size=30,
        sweeps=3,
        processes=2,
        workers=4,
        restart_cycles=1,
        failure_cycles=1,
        monitor_population=1,
        monitor_profiles=(MonitoringProfile(processes=1, workers=1),),
        monitor_seconds=1.0,
        monitor_interval=1.0,
        repeated_timeouts=10,
        headed=headed,
        only=ACCEPTANCE_SCENARIOS,
    )
    leftovers = {
        backend.value: len(main_pids(backend))
        for backend in (BrowserBackendName.PATCHRIGHT, BrowserBackendName.CHROME)
    }
    patchright_workflow = cast(dict[str, Any], workflows["patchright"])
    chrome_workflow = cast(dict[str, Any], workflows["chrome_control"])
    by_backend = scenarios["summary"]["by_backend"]
    concurrency = scenarios.get("concurrency", {})
    patchright_ok = (
        patchright.passed
        and patchright_workflow["failed"] == 0
        and by_backend["patchright"]["checks_failed"] == 0
        and "patchright-p2" in concurrency.get("highest_healthy_level", {})
    )
    chrome_ok = (
        bool(chrome["passed"])
        and chrome_workflow["failed"] == 0
        and by_backend["chrome"]["checks_failed"] == 0
        and "chrome-p2" in concurrency.get("highest_healthy_level", {})
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "local_queue_simulator_only",
        "staging": "NOT_RUN_UNKNOWN",
        "camoufox": "EXCLUDED (retained, dormant/experimental, not certified)",
        "default_backend_for_new_runs": BrowserBackendName.PATCHRIGHT.value,
        "decision": {
            "patchright_acceptance": "PASS" if patchright_ok else "FAIL",
            "chrome_fallback_regression": "PASS" if chrome_ok else "FAIL",
            "leftover_browser_main_processes": leftovers,
        },
        "preflight": {
            "patchright": asdict(patchright),
            "chrome_fallback": chrome,
        },
        "workflow": {
            "patchright_headed": headed,
            "patchright": _workflow_counts(patchright_workflow),
            "chrome_fallback": _workflow_counts(chrome_workflow),
            "detail": workflows,
        },
        "scenarios": scenarios,
        "duration_seconds": round(time.perf_counter() - started, 1),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--headless", action="store_true", help="hide the Patchright manual window"
    )
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="phase7-acceptance-") as work:
        report = asyncio.run(run_phase7_acceptance(Path(work), headed=not args.headless))
    text = json.dumps(report, indent=2, default=_json_default) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(json.dumps(report["decision"], indent=2))
    failed = "FAIL" in report["decision"].values()
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
