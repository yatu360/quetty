import json
from datetime import UTC, datetime
from pathlib import Path

from queue_load_test.harness.concurrency_tuning import (
    RestoreMethodMeasurements,
    SaturationThresholds,
    TuningCase,
    TuningCaseResult,
    TuningMatrixReport,
    calculate_saturation_flags,
    collect_tuning_cases,
    generate_tuning_matrix,
)
from queue_load_test.harness.phase2_tuning import load_tuning_matrix
from queue_load_test.harness.resource_benchmark import (
    ResourceBenchmarkConfiguration,
    ResourceBenchmarkReport,
    ResourceSample,
)
from queue_load_test.transfer import RestoreMethod


def test_standard_matrix_generation_is_deterministic_and_bounded() -> None:
    cases = generate_tuning_matrix(chrome_process_counts=(1, 2))

    assert len(cases) == 10
    assert [case.max_active_contexts for case in cases[:5]] == [5, 10, 15, 20, 25]
    assert {case.chrome_process_count for case in cases} == {1, 2}
    assert all(case.max_active_contexts <= 25 for case in cases)
    assert all(
        case.creation_workers + case.monitor_workers <= case.max_active_contexts
        for case in cases
    )
    assert cases[-1].max_contexts_per_browser == 13


def test_explicit_matrix_file_controls_every_concurrency_value(tmp_path: Path) -> None:
    path = tmp_path / "matrix.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "custom-10",
                        "chrome_process_count": 2,
                        "max_active_contexts": 10,
                        "max_contexts_per_browser": 5,
                        "creation_workers": 4,
                        "monitor_workers": 6,
                        "monitor_queue_capacity": 8,
                        "monitor_claim_batch_size": 4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_tuning_matrix(path) == (
        TuningCase("custom-10", 2, 10, 5, 4, 6, 8, 4),
    )


def test_checked_in_matrix_matches_generated_two_process_plan() -> None:
    path = Path("benchmarks/phase2-concurrency-matrix.example.json")

    assert load_tuning_matrix(path) == generate_tuning_matrix(
        chrome_process_counts=(1, 2)
    )


async def test_result_collection_is_sequential_and_retains_failures() -> None:
    cases = generate_tuning_matrix(active_context_levels=(5, 10))
    active = 0
    maximum_active = 0

    async def execute(case: TuningCase) -> TuningCaseResult:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        try:
            if case.max_active_contexts == 10:
                raise RuntimeError("synthetic failure")
            return result(case)
        finally:
            active -= 1

    results = await collect_tuning_cases(cases, execute)

    assert maximum_active == 1
    assert [item.status for item in results] == ["COMPLETED", "FAILED"]
    assert results[1].error_category == "RuntimeError"


def test_saturation_flags_compare_adjacent_cases_without_ranking() -> None:
    earlier = result(generate_tuning_matrix(active_context_levels=(5,))[0])
    later_case = generate_tuning_matrix(active_context_levels=(10,))[0]
    later = result(
        later_case,
        creation_p95=2.0,
        check_p95=4.0,
        context_p95=0.2,
        cpu_peak=95,
        application_ram=200,
        chrome_ram=300,
        navigation_failures=5,
        crashes=1,
        mismatches=1,
        backlog=(1, 2, 3),
        restore_rate=0.9,
    )

    flags = calculate_saturation_flags(
        (earlier, later),
        SaturationThresholds(
            adjacent_latency_ratio=1.5,
            failure_rate_increase=0.02,
            sustained_cpu_percent=90,
            adjacent_ram_ratio=1.5,
            restore_success_rate_drop=0.05,
        ),
    )
    codes = {flag.code for flag in flags}

    assert {
        "CREATION_LATENCY_RISE",
        "CHECK_LATENCY_RISE",
        "CONTEXT_ACQUISITION_LATENCY_RISE",
        "FAILURE_RATE_INCREASE",
        "CPU_NEAR_SATURATION",
        "RAM_GROWTH",
        "BROWSER_INSTABILITY",
        "BACKLOG_CONTINUOUSLY_INCREASING",
        "RESTORE_RELIABILITY_DECREASE",
        "IDENTITY_MISMATCH",
    } <= codes
    assert not hasattr(TuningMatrixReport.build((earlier, later)), "winner")


def test_report_serialization_and_comparison_summary(tmp_path: Path) -> None:
    case_result = result(generate_tuning_matrix(active_context_levels=(5,))[0])
    report = TuningMatrixReport.build((case_result,))
    path = tmp_path / "matrix-result.json"

    report.write_json(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    comparison = payload["comparison"][0]

    assert payload["status"] == "COMPLETED"
    assert comparison == {
        "case": "p1-c5",
        "status": "COMPLETED",
        "browser_processes": 1,
        "max_contexts": 5,
        "creation_throughput": 10.0,
        "check_throughput": 20.0,
        "p95_creation_latency": 1.0,
        "p95_check_latency": 2.0,
        "cpu_peak": 50.0,
        "ram_peak_bytes": 300.0,
        "errors": 0,
        "identity_mismatches": 0,
    }
    assert "optimal" in payload["scope_warning"]
    assert "1000" not in json.dumps(payload)


def result(
    case: TuningCase,
    *,
    creation_p95: float = 1.0,
    check_p95: float = 2.0,
    context_p95: float = 0.1,
    cpu_peak: float = 50,
    application_ram: float = 100,
    chrome_ram: float = 200,
    navigation_failures: int = 0,
    crashes: int = 0,
    mismatches: int = 0,
    backlog: tuple[float, ...] = (3, 2, 1),
    restore_rate: float = 1.0,
) -> TuningCaseResult:
    summary: dict[str, object] = {
        "application_cpu_percent": {"average": cpu_peak, "peak": cpu_peak},
        "chrome_cpu_percent": {"average": cpu_peak, "peak": cpu_peak},
        "application_ram_bytes": {"average": application_ram, "peak": application_ram},
        "chrome_ram_bytes": {"average": chrome_ram, "peak": chrome_ram},
        "browser_crashes": crashes,
        "context_creation_failures": 0,
        "navigation_failures": navigation_failures,
        "restore_failures": 0,
        "identity_mismatches": mismatches,
        "creation_throughput_per_second": 10.0,
        "monitoring_throughput_per_second": 20.0,
        "latencies": {
            "creation": latency(100, creation_p95),
            "check": latency(100, check_p95),
            "context_creation": latency(5, context_p95),
            "context_acquisition": latency(100, context_p95),
            "restore": latency(100, 1.5),
        },
    }
    samples = tuple(sample(case, value, index) for index, value in enumerate(backlog))
    resource_report = ResourceBenchmarkReport(
        generated_at=datetime.now(UTC),
        configuration=ResourceBenchmarkConfiguration(
            target_queue_ids=100,
            session_mode="HYBRID",
            chrome_process_count=case.chrome_process_count,
            max_contexts_per_browser=case.max_contexts_per_browser,
            max_active_contexts=case.max_active_contexts,
            creation_workers=case.creation_workers,
            monitor_workers=case.monitor_workers,
        ),
        test_duration_seconds=60,
        sample_interval_seconds=5,
        summary=summary,
        samples=samples,
    )
    method = RestoreMethodMeasurements(
        attempts=10,
        successes=round(10 * restore_rate),
        failures=10 - round(10 * restore_rate),
        success_rate=restore_rate,
        average_duration_seconds=1,
        p50_duration_seconds=1,
        p95_duration_seconds=1.2,
    )
    return TuningCaseResult(
        case=case,
        status="COMPLETED",
        resource_report=resource_report,
        restore_methods={method_name.value: method for method_name in RestoreMethod},
    )


def latency(count: int, p95: float) -> dict[str, int | float]:
    return {
        "count": count,
        "average_seconds": p95 / 2,
        "p50_seconds": p95 / 2,
        "p95_seconds": p95,
    }


def sample(case: TuningCase, backlog: float, offset: int) -> ResourceSample:
    return ResourceSample(
        timestamp=datetime.now(UTC),
        application_cpu_percent=10,
        application_ram_bytes=100,
        chrome_cpu_percent=20,
        chrome_ram_bytes=200,
        observed_chrome_processes=case.chrome_process_count,
        managed_chrome_processes=case.chrome_process_count,
        active_browser_contexts=case.max_active_contexts,
        open_file_descriptors=None,
        creation_queue_depth=0,
        monitoring_queue_depth=float(offset),
        due_session_backlog=backlog,
    )
