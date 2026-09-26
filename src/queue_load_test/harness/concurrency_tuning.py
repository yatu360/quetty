"""Objective comparison and saturation signals for Phase 2 concurrency runs."""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from queue_load_test.harness.resource_benchmark import ResourceBenchmarkReport
from queue_load_test.harness.restore_benchmark import RestoreBenchmarkReport, percentile
from queue_load_test.transfer import RestoreMethod


@dataclass(frozen=True, slots=True)
class TuningCase:
    name: str
    chrome_process_count: int
    max_active_contexts: int
    max_contexts_per_browser: int
    creation_workers: int
    monitor_workers: int
    monitor_queue_capacity: int
    monitor_claim_batch_size: int

    def __post_init__(self) -> None:
        if not self.name or any(character not in "-_abcdefghijklmnopqrstuvwxyz0123456789" for character in self.name):
            raise ValueError("case name must use lowercase letters, digits, '-' or '_'")
        if self.chrome_process_count not in {1, 2}:
            raise ValueError("chrome_process_count must be 1 or 2")
        if not 1 <= self.max_active_contexts <= 25:
            raise ValueError("max_active_contexts must be between 1 and 25")
        if self.max_contexts_per_browser < 1:
            raise ValueError("max_contexts_per_browser must be at least 1")
        if (
            self.chrome_process_count * self.max_contexts_per_browser
            < self.max_active_contexts
        ):
            raise ValueError("per-browser capacity cannot satisfy max_active_contexts")
        if self.creation_workers < 1 or self.monitor_workers < 1:
            raise ValueError("worker counts must be at least 1")
        if self.creation_workers + self.monitor_workers > self.max_active_contexts:
            raise ValueError("combined worker counts cannot exceed max_active_contexts")
        if self.monitor_queue_capacity < 1:
            raise ValueError("monitor_queue_capacity must be at least 1")
        if not 1 <= self.monitor_claim_batch_size <= self.monitor_queue_capacity:
            raise ValueError("claim batch size must fit the monitoring queue")


def generate_tuning_matrix(
    *,
    active_context_levels: Sequence[int] = (5, 10, 15, 20, 25),
    chrome_process_counts: Sequence[int] = (1,),
) -> tuple[TuningCase, ...]:
    """Generate a deterministic safe matrix; explicit manifests can override it."""

    cases: list[TuningCase] = []
    for process_count in chrome_process_counts:
        for active_contexts in active_context_levels:
            creation_workers = max(1, active_contexts // 2)
            monitor_workers = active_contexts - creation_workers
            cases.append(
                TuningCase(
                    name=f"p{process_count}-c{active_contexts}",
                    chrome_process_count=process_count,
                    max_active_contexts=active_contexts,
                    max_contexts_per_browser=math.ceil(active_contexts / process_count),
                    creation_workers=creation_workers,
                    monitor_workers=monitor_workers,
                    monitor_queue_capacity=active_contexts,
                    monitor_claim_batch_size=min(active_contexts, monitor_workers),
                )
            )
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("matrix case names must be unique")
    return tuple(cases)


@dataclass(frozen=True, slots=True)
class RestoreMethodMeasurements:
    attempts: int
    successes: int
    failures: int
    success_rate: float | None
    average_duration_seconds: float | None
    p50_duration_seconds: float | None
    p95_duration_seconds: float | None


def summarize_restore_methods(
    report: RestoreBenchmarkReport,
) -> dict[str, RestoreMethodMeasurements]:
    summaries: dict[str, RestoreMethodMeasurements] = {}
    for method in RestoreMethod:
        matching = [
            attempt
            for attempt in report.attempts
            if attempt.restore_method is method and len(attempt.mechanism_attempts) == 1
        ]
        durations = [attempt.duration_seconds for attempt in matching]
        successes = sum(attempt.success for attempt in matching)
        summaries[method.value] = RestoreMethodMeasurements(
            attempts=len(matching),
            successes=successes,
            failures=len(matching) - successes,
            success_rate=successes / len(matching) if matching else None,
            average_duration_seconds=(sum(durations) / len(durations) if durations else None),
            p50_duration_seconds=percentile(durations, 50),
            p95_duration_seconds=percentile(durations, 95),
        )
    return summaries


@dataclass(frozen=True, slots=True)
class TuningCaseResult:
    case: TuningCase
    status: str
    resource_report: ResourceBenchmarkReport | None
    restore_methods: dict[str, RestoreMethodMeasurements]
    error_category: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "case": asdict(self.case),
            "status": self.status,
            "error_category": self.error_category,
            "resource_report": (
                self.resource_report.to_dict() if self.resource_report is not None else None
            ),
            "restore_methods": {
                method: asdict(measurements)
                for method, measurements in self.restore_methods.items()
            },
        }


type CaseExecutor = Callable[[TuningCase], Awaitable[TuningCaseResult]]


async def collect_tuning_cases(
    cases: Sequence[TuningCase],
    executor: CaseExecutor,
) -> tuple[TuningCaseResult, ...]:
    """Run cases sequentially so matrix size never becomes task/browser concurrency."""

    results: list[TuningCaseResult] = []
    for case in cases:
        try:
            result = await executor(case)
        except Exception as exc:  # noqa: BLE001 - preserve remaining controlled cases
            result = TuningCaseResult(
                case=case,
                status="FAILED",
                resource_report=None,
                restore_methods={},
                error_category=type(exc).__name__,
            )
        if result.case != case:
            raise ValueError("case executor returned a result for a different case")
        results.append(result)
    return tuple(results)


@dataclass(frozen=True, slots=True)
class SaturationThresholds:
    adjacent_latency_ratio: float = 1.5
    failure_rate_increase: float = 0.02
    sustained_cpu_percent: float = 90.0
    adjacent_ram_ratio: float = 1.5
    restore_success_rate_drop: float = 0.02

    def __post_init__(self) -> None:
        if self.adjacent_latency_ratio <= 1 or self.adjacent_ram_ratio <= 1:
            raise ValueError("adjacent growth ratios must be greater than 1")
        if self.failure_rate_increase < 0 or self.restore_success_rate_drop < 0:
            raise ValueError("rate thresholds cannot be negative")
        if self.sustained_cpu_percent <= 0:
            raise ValueError("sustained_cpu_percent must be positive")


@dataclass(frozen=True, slots=True)
class SaturationFlag:
    code: str
    case: str
    previous_case: str | None
    observed: float | int | str
    threshold: float | int | str
    detail: str


def calculate_saturation_flags(
    results: Sequence[TuningCaseResult],
    thresholds: SaturationThresholds | None = None,
) -> tuple[SaturationFlag, ...]:
    """Flag objective symptoms without ranking cases or choosing a winner."""

    limits = thresholds or SaturationThresholds()
    completed = [
        result
        for result in results
        if result.status == "COMPLETED" and result.resource_report is not None
    ]
    flags: list[SaturationFlag] = []
    for result in completed:
        report = result.resource_report
        assert report is not None
        summary = report.summary
        sustained_cpu = max(
            _nested_number(summary, "application_cpu_percent", "average") or 0,
            _nested_number(summary, "chrome_cpu_percent", "average") or 0,
        )
        if sustained_cpu >= limits.sustained_cpu_percent:
            flags.append(
                SaturationFlag(
                    "CPU_NEAR_SATURATION",
                    result.case.name,
                    None,
                    sustained_cpu,
                    limits.sustained_cpu_percent,
                    "Average application or aggregate Chrome CPU reached the configured threshold.",
                )
            )
        crashes = _number(summary.get("browser_crashes")) or 0
        if crashes > 0:
            flags.append(
                SaturationFlag(
                    "BROWSER_INSTABILITY",
                    result.case.name,
                    None,
                    int(crashes),
                    0,
                    "One or more managed browser crashes were observed.",
                )
            )
        mismatches = _number(summary.get("identity_mismatches")) or 0
        if mismatches > 0:
            flags.append(
                SaturationFlag(
                    "IDENTITY_MISMATCH",
                    result.case.name,
                    None,
                    int(mismatches),
                    0,
                    "Identity mismatches require investigation regardless of throughput.",
                )
            )
        backlog = [
            sample.due_session_backlog
            for sample in report.samples
            if sample.due_session_backlog is not None
        ]
        if _continuously_increasing(backlog):
            flags.append(
                SaturationFlag(
                    "BACKLOG_CONTINUOUSLY_INCREASING",
                    result.case.name,
                    None,
                    backlog[-1] - backlog[0],
                    0,
                    "Due-session backlog was non-decreasing and ended above its start.",
                )
            )

    groups: dict[int, list[TuningCaseResult]] = {}
    for result in completed:
        groups.setdefault(result.case.chrome_process_count, []).append(result)
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.case.max_active_contexts)
        for previous, current in pairwise(ordered):
            _append_adjacent_flags(flags, previous, current, limits)
    return tuple(flags)


def _append_adjacent_flags(
    flags: list[SaturationFlag],
    previous: TuningCaseResult,
    current: TuningCaseResult,
    thresholds: SaturationThresholds,
) -> None:
    assert previous.resource_report is not None and current.resource_report is not None
    previous_summary = previous.resource_report.summary
    current_summary = current.resource_report.summary
    for latency_name, code in (
        ("creation", "CREATION_LATENCY_RISE"),
        ("check", "CHECK_LATENCY_RISE"),
        ("context_acquisition", "CONTEXT_ACQUISITION_LATENCY_RISE"),
    ):
        earlier = _latency(previous_summary, latency_name, "p95_seconds")
        later = _latency(current_summary, latency_name, "p95_seconds")
        ratio = _ratio(later, earlier)
        if ratio is not None and ratio >= thresholds.adjacent_latency_ratio:
            flags.append(
                SaturationFlag(
                    code,
                    current.case.name,
                    previous.case.name,
                    ratio,
                    thresholds.adjacent_latency_ratio,
                    f"p95 {latency_name} latency rose materially between adjacent cases.",
                )
            )

    earlier_failure_rate = _failure_rate(previous_summary)
    later_failure_rate = _failure_rate(current_summary)
    if (
        earlier_failure_rate is not None
        and later_failure_rate is not None
        and later_failure_rate - earlier_failure_rate >= thresholds.failure_rate_increase
    ):
        flags.append(
            SaturationFlag(
                "FAILURE_RATE_INCREASE",
                current.case.name,
                previous.case.name,
                later_failure_rate - earlier_failure_rate,
                thresholds.failure_rate_increase,
                "Aggregate failure rate increased between adjacent cases.",
            )
        )

    earlier_ram = _combined_peak_ram(previous_summary)
    later_ram = _combined_peak_ram(current_summary)
    ram_ratio = _ratio(later_ram, earlier_ram)
    if ram_ratio is not None and ram_ratio >= thresholds.adjacent_ram_ratio:
        flags.append(
            SaturationFlag(
                "RAM_GROWTH",
                current.case.name,
                previous.case.name,
                ram_ratio,
                thresholds.adjacent_ram_ratio,
                "Combined application and Chrome peak RAM rose materially.",
            )
        )

    for method in RestoreMethod:
        earlier_restore = previous.restore_methods.get(method.value)
        later_restore = current.restore_methods.get(method.value)
        if (
            earlier_restore is not None
            and later_restore is not None
            and earlier_restore.success_rate is not None
            and later_restore.success_rate is not None
            and earlier_restore.success_rate - later_restore.success_rate
            >= thresholds.restore_success_rate_drop
        ):
            flags.append(
                SaturationFlag(
                    "RESTORE_RELIABILITY_DECREASE",
                    current.case.name,
                    previous.case.name,
                    earlier_restore.success_rate - later_restore.success_rate,
                    thresholds.restore_success_rate_drop,
                    f"{method.value} success rate decreased between adjacent cases.",
                )
            )


@dataclass(frozen=True, slots=True)
class TuningMatrixReport:
    generated_at: datetime
    status: str
    cases: tuple[TuningCaseResult, ...]
    saturation_flags: tuple[SaturationFlag, ...]
    scope_warning: str = (
        "Results describe only these authorised Phase 2 cases; they do not select an "
        "optimal configuration or predict 1,000/10,000-session behavior."
    )

    @classmethod
    def build(
        cls,
        cases: Sequence[TuningCaseResult],
        thresholds: SaturationThresholds | None = None,
    ) -> TuningMatrixReport:
        case_tuple = tuple(cases)
        completed = sum(case.status == "COMPLETED" for case in case_tuple)
        status = "COMPLETED" if completed == len(case_tuple) and case_tuple else "PARTIAL"
        if not case_tuple:
            status = "NOT RUN"
        return cls(
            generated_at=datetime.now(UTC),
            status=status,
            cases=case_tuple,
            saturation_flags=calculate_saturation_flags(case_tuple, thresholds),
        )

    def comparison_rows(self) -> tuple[dict[str, object], ...]:
        return tuple(_comparison_row(result) for result in self.cases)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "status": self.status,
            "scope_warning": self.scope_warning,
            "cases": [case.to_dict() for case in self.cases],
            "comparison": list(self.comparison_rows()),
            "saturation_flags": [asdict(flag) for flag in self.saturation_flags],
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def render_text(self) -> str:
        headers = (
            "case",
            "proc",
            "ctx",
            "create/s",
            "check/s",
            "p95 create",
            "p95 check",
            "CPU peak",
            "RAM peak",
            "errors",
            "mismatch",
        )
        lines = ["Phase 2 concurrency tuning matrix", "", self.scope_warning, ""]
        lines.append(" | ".join(headers))
        for row in self.comparison_rows():
            lines.append(
                " | ".join(
                    str(row[key])
                    for key in (
                        "case",
                        "browser_processes",
                        "max_contexts",
                        "creation_throughput",
                        "check_throughput",
                        "p95_creation_latency",
                        "p95_check_latency",
                        "cpu_peak",
                        "ram_peak_bytes",
                        "errors",
                        "identity_mismatches",
                    )
                )
            )
        lines.extend(("", f"Saturation flags: {len(self.saturation_flags)}"))
        lines.extend(f"- {flag.code}: {flag.case} — {flag.detail}" for flag in self.saturation_flags)
        return "\n".join(lines) + "\n"


def _comparison_row(result: TuningCaseResult) -> dict[str, object]:
    if result.resource_report is None:
        return {
            "case": result.case.name,
            "status": result.status,
            "browser_processes": result.case.chrome_process_count,
            "max_contexts": result.case.max_active_contexts,
            "creation_throughput": None,
            "check_throughput": None,
            "p95_creation_latency": None,
            "p95_check_latency": None,
            "cpu_peak": None,
            "ram_peak_bytes": None,
            "errors": None,
            "identity_mismatches": None,
        }
    summary = result.resource_report.summary
    return {
        "case": result.case.name,
        "status": result.status,
        "browser_processes": result.case.chrome_process_count,
        "max_contexts": result.case.max_active_contexts,
        "creation_throughput": summary.get("creation_throughput_per_second"),
        "check_throughput": summary.get("monitoring_throughput_per_second"),
        "p95_creation_latency": _latency(summary, "creation", "p95_seconds"),
        "p95_check_latency": _latency(summary, "check", "p95_seconds"),
        "cpu_peak": max(
            _nested_number(summary, "application_cpu_percent", "peak") or 0,
            _nested_number(summary, "chrome_cpu_percent", "peak") or 0,
        ),
        "ram_peak_bytes": _combined_peak_ram(summary),
        "errors": int(_error_count(summary)),
        "identity_mismatches": int(_number(summary.get("identity_mismatches")) or 0),
    }


def _latency(summary: dict[str, object], name: str, field: str) -> float | None:
    latencies = summary.get("latencies")
    if not isinstance(latencies, dict):
        return None
    value = latencies.get(name)
    if not isinstance(value, dict):
        return None
    return _number(value.get(field))


def _nested_number(summary: dict[str, object], name: str, field: str) -> float | None:
    value = summary.get(name)
    if not isinstance(value, dict):
        return None
    return _number(value.get(field))


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _ratio(later: float | None, earlier: float | None) -> float | None:
    if later is None or earlier is None or earlier <= 0:
        return None
    return later / earlier


def _combined_peak_ram(summary: dict[str, object]) -> float | None:
    values = [
        value
        for value in (
            _nested_number(summary, "application_ram_bytes", "peak"),
            _nested_number(summary, "chrome_ram_bytes", "peak"),
        )
        if value is not None
    ]
    return sum(values) if values else None


def _error_count(summary: dict[str, object]) -> float:
    return sum(
        _number(summary.get(name)) or 0
        for name in (
            "browser_crashes",
            "context_creation_failures",
            "navigation_failures",
            "restore_failures",
        )
    )


def _failure_rate(summary: dict[str, object]) -> float | None:
    operations = sum(
        _latency(summary, name, "count") or 0
        for name in ("creation", "check")
    )
    return _error_count(summary) / operations if operations else None


def _continuously_increasing(values: Sequence[float]) -> bool:
    return (
        len(values) >= 3
        and values[-1] > values[0]
        and all(later >= earlier for earlier, later in pairwise(values))
    )


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
