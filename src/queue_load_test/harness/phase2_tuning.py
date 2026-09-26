"""Explicitly gated, sequential Phase 2 concurrency matrix runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from queue_load_test.config import Settings, get_settings
from queue_load_test.harness.concurrency_tuning import (
    SaturationThresholds,
    TuningCase,
    TuningCaseResult,
    TuningMatrixReport,
    collect_tuning_cases,
    generate_tuning_matrix,
    summarize_restore_methods,
)
from queue_load_test.harness.phase2_resources import run_phase2_resource_benchmark
from queue_load_test.harness.phase2_restore import run_phase2_restore_benchmark
from queue_load_test.harness.restore_benchmark import RestoreBenchmarkMode
from queue_load_test.metrics import configure_structured_logging
from queue_load_test.models import SessionMode

_GATE = "RUN_PHASE2_CONCURRENCY_BENCHMARK"


async def run_phase2_concurrency_benchmark(
    settings: Settings,
    *,
    cases: tuple[TuningCase, ...],
    report_path: Path,
    work_directory: Path,
    monitoring_seconds: float,
    sample_interval_seconds: float,
    creation_timeout_seconds: float,
    restore_sample_size: int,
    thresholds: SaturationThresholds | None = None,
) -> TuningMatrixReport:
    """Run isolated cases sequentially and retain evidence for failed cases."""

    _validate_base_profile(settings, cases, restore_sample_size)
    if monitoring_seconds < 0:
        raise ValueError("monitoring_seconds cannot be negative")
    if sample_interval_seconds <= 0 or creation_timeout_seconds <= 0:
        raise ValueError("sample interval and creation timeout must be positive")
    if os.environ.get("RUN_STAGING_TESTS") != "1":
        raise RuntimeError("Set RUN_STAGING_TESTS=1 to run against authorised staging")
    if os.environ.get(_GATE) != "1":
        raise RuntimeError(f"Set {_GATE}=1 to run the Phase 2 concurrency benchmark")
    if work_directory.exists():
        raise FileExistsError(
            "Use a new work directory for each matrix so cases remain isolated"
        )
    work_directory.mkdir(parents=True)

    async def execute(case: TuningCase) -> TuningCaseResult:
        case_directory = work_directory / case.name
        case_directory.mkdir()
        case_settings = _settings_for_case(settings, case, case_directory)
        resource_report = await run_phase2_resource_benchmark(
            case_settings,
            report_path=case_directory / "resources.json",
            monitoring_seconds=monitoring_seconds,
            sample_interval_seconds=sample_interval_seconds,
            creation_timeout_seconds=creation_timeout_seconds,
            environment_gate=_GATE,
        )
        restore_methods = {}
        if restore_sample_size:
            restore_report = await run_phase2_restore_benchmark(
                case_settings,
                report_path=case_directory / "restore-identities.json",
                sample_size=restore_sample_size,
                modes=(
                    RestoreBenchmarkMode.TRANSFER_ONLY,
                    RestoreBenchmarkMode.STORAGE_STATE_ONLY,
                ),
                environment_gate=_GATE,
            )
            restore_methods = summarize_restore_methods(restore_report)
        return TuningCaseResult(
            case=case,
            status="COMPLETED",
            resource_report=resource_report,
            restore_methods=restore_methods,
        )

    results = await collect_tuning_cases(cases, execute)
    report = TuningMatrixReport.build(results, thresholds)
    report.write_json(report_path)
    return report


def load_tuning_matrix(path: Path) -> tuple[TuningCase, ...]:
    """Load an explicit repeatable case manifest without accepting unknown fields."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_cases: object = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("matrix file must contain a non-empty cases list")
    cases: list[TuningCase] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise TypeError("each matrix case must be an object")
        cases.append(_case_from_mapping(raw_case))
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("matrix case names must be unique")
    return tuple(cases)


def _case_from_mapping(value: dict[object, object]) -> TuningCase:
    expected = {
        "name",
        "chrome_process_count",
        "max_active_contexts",
        "max_contexts_per_browser",
        "creation_workers",
        "monitor_workers",
        "monitor_queue_capacity",
        "monitor_claim_batch_size",
    }
    if set(value) != expected:
        raise ValueError("matrix case fields must exactly match the documented schema")
    name = value["name"]
    if not isinstance(name, str):
        raise TypeError("case name must be text")
    return TuningCase(
        name=name,
        chrome_process_count=_integer(value, "chrome_process_count"),
        max_active_contexts=_integer(value, "max_active_contexts"),
        max_contexts_per_browser=_integer(value, "max_contexts_per_browser"),
        creation_workers=_integer(value, "creation_workers"),
        monitor_workers=_integer(value, "monitor_workers"),
        monitor_queue_capacity=_integer(value, "monitor_queue_capacity"),
        monitor_claim_batch_size=_integer(value, "monitor_claim_batch_size"),
    )


def _integer(value: dict[object, object], key: str) -> int:
    item = value[key]
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError(f"{key} must be an integer")
    return item


def _settings_for_case(
    base: Settings,
    case: TuningCase,
    case_directory: Path,
) -> Settings:
    values = base.model_dump()
    values.update(
        {
            "target_queue_ids": 100,
            "session_mode": SessionMode.HYBRID,
            "chrome_process_count": case.chrome_process_count,
            "max_active_contexts": case.max_active_contexts,
            "max_contexts_per_browser": case.max_contexts_per_browser,
            "creation_workers": case.creation_workers,
            "monitor_workers": case.monitor_workers,
            "monitor_queue_capacity": case.monitor_queue_capacity,
            "monitor_claim_batch_size": case.monitor_claim_batch_size,
            "database_url": "sqlite:///"
            + (case_directory / "sessions.sqlite3").resolve().as_posix(),
            "state_directory": case_directory / "browser-state",
        }
    )
    return Settings(**values)


def _validate_base_profile(
    settings: Settings,
    cases: tuple[TuningCase, ...],
    restore_sample_size: int,
) -> None:
    if settings.target_queue_ids != 100:
        raise ValueError("Phase 2 tuning requires TARGET_QUEUE_IDS=100")
    if settings.session_mode is not SessionMode.HYBRID:
        raise ValueError("Phase 2 tuning requires SESSION_MODE=HYBRID")
    if not settings.database_url.startswith("sqlite:///"):
        raise ValueError("Phase 2 tuning requires SQLite")
    if not cases:
        raise ValueError("at least one tuning case is required")
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("matrix case names must be unique")
    if not 0 <= restore_sample_size <= 100:
        raise ValueError("restore_sample_size must be between 0 and 100")


def _parse_int_list(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not parsed:
        raise argparse.ArgumentTypeError("at least one integer is required")
    return parsed


def _default_work_directory() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".phase2-concurrency-runs") / timestamp


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an opt-in Phase 2 concurrency evidence matrix"
    )
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    parser.add_argument("--matrix-file", type=Path)
    parser.add_argument("--active-contexts", type=_parse_int_list, default=(5, 10, 15, 20, 25))
    parser.add_argument("--chrome-processes", type=_parse_int_list)
    parser.add_argument("--report", type=Path, default=Path("phase2-concurrency-benchmark.json"))
    parser.add_argument("--work-directory", type=Path, default=_default_work_directory())
    parser.add_argument("--monitoring-seconds", type=float, default=600)
    parser.add_argument("--sample-interval-seconds", type=float, default=5)
    parser.add_argument("--creation-timeout-seconds", type=float, default=3600)
    parser.add_argument("--restore-sample-size", type=int, default=10)
    parser.add_argument("--adjacent-latency-ratio", type=float, default=1.5)
    parser.add_argument("--failure-rate-increase", type=float, default=0.02)
    parser.add_argument("--sustained-cpu-percent", type=float, default=90)
    parser.add_argument("--adjacent-ram-ratio", type=float, default=1.5)
    parser.add_argument("--restore-rate-drop", type=float, default=0.02)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable browser traffic")
    settings = get_settings()
    cases = (
        load_tuning_matrix(args.matrix_file)
        if args.matrix_file is not None
        else generate_tuning_matrix(
            active_context_levels=args.active_contexts,
            chrome_process_counts=args.chrome_processes
            or (settings.chrome_process_count,),
        )
    )
    thresholds = SaturationThresholds(
        adjacent_latency_ratio=args.adjacent_latency_ratio,
        failure_rate_increase=args.failure_rate_increase,
        sustained_cpu_percent=args.sustained_cpu_percent,
        adjacent_ram_ratio=args.adjacent_ram_ratio,
        restore_success_rate_drop=args.restore_rate_drop,
    )
    configure_structured_logging()
    report = asyncio.run(
        run_phase2_concurrency_benchmark(
            settings,
            cases=cases,
            report_path=args.report,
            work_directory=args.work_directory,
            monitoring_seconds=args.monitoring_seconds,
            sample_interval_seconds=args.sample_interval_seconds,
            creation_timeout_seconds=args.creation_timeout_seconds,
            restore_sample_size=args.restore_sample_size,
            thresholds=thresholds,
        )
    )
    print(report.render_text())


if __name__ == "__main__":
    main()
