"""Explicitly gated Phase 2 HYBRID restoration reliability benchmark."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings, get_settings
from queue_load_test.harness.restore_benchmark import (
    RestoreBenchmarkMode,
    RestoreBenchmarkReport,
    RestoreBenchmarkRunner,
)
from queue_load_test.metrics import PrometheusMetrics, configure_structured_logging
from queue_load_test.models import SessionMode
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer


async def run_phase2_restore_benchmark(
    settings: Settings,
    *,
    report_path: Path,
    sample_size: int,
    modes: tuple[RestoreBenchmarkMode, ...] = (
        RestoreBenchmarkMode.TRANSFER_ONLY,
        RestoreBenchmarkMode.STORAGE_STATE_ONLY,
        RestoreBenchmarkMode.HYBRID,
    ),
    environment_gate: str = "RUN_PHASE2_RESTORE_BENCHMARK",
) -> RestoreBenchmarkReport:
    """Benchmark persisted HYBRID sessions after both explicit safety gates."""

    _validate_phase2_profile(settings, sample_size)
    if os.environ.get("RUN_STAGING_TESTS") != "1":
        raise RuntimeError("Set RUN_STAGING_TESTS=1 to run against authorised staging")
    if os.environ.get(environment_gate) != "1":
        raise RuntimeError(f"Set {environment_gate}=1 to run the Phase 2 restore benchmark")

    metrics = PrometheusMetrics()
    repository = SQLiteSessionRepository(settings.database_url)
    state_store = FileSystemStateStore(settings.state_directory)
    browser_manager = BrowserManager.from_settings(settings, observability=metrics)
    restorer = QueueSessionRestorer.from_settings(
        settings,
        browser_manager=browser_manager,
        repository=repository,
        state_store=state_store,
        observability=metrics,
    )
    try:
        sessions = await repository.list()
        await browser_manager.start()
        report = await RestoreBenchmarkRunner(restorer).run(
            sessions,
            sample_size=sample_size,
            modes=modes,
        )
        report.write_json(report_path)
        return report
    finally:
        await browser_manager.shutdown()
        await repository.close()


def _validate_phase2_profile(settings: Settings, sample_size: int) -> None:
    if sample_size < 1 or sample_size > settings.target_queue_ids:
        raise ValueError("sample_size must be between 1 and TARGET_QUEUE_IDS")
    expected = {
        "TARGET_QUEUE_IDS": settings.target_queue_ids == 100,
        "SESSION_MODE": settings.session_mode is SessionMode.HYBRID,
        "DATABASE_URL": settings.database_url.startswith("sqlite:///"),
    }
    invalid = [name for name, matches in expected.items() if not matches]
    if invalid:
        raise ValueError("Phase 2 restore profile mismatch: " + ", ".join(invalid))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the opt-in Phase 2 HYBRID restore reliability benchmark"
    )
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("phase2-restore-benchmark.json"))
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument(
        "--mode",
        choices=("all", *(mode.value.casefold() for mode in RestoreBenchmarkMode)),
        default="all",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable browser traffic")
    modes = (
        tuple(RestoreBenchmarkMode)
        if args.mode == "all"
        else (RestoreBenchmarkMode.parse(args.mode),)
    )
    configure_structured_logging()
    report = asyncio.run(
        run_phase2_restore_benchmark(
            get_settings(),
            report_path=args.report,
            sample_size=args.sample_size,
            modes=modes,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
