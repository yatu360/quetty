import argparse
from typing import Any

import pytest

from queue_load_test.browser import ChromeBackend, PatchrightBackend
from queue_load_test.harness.phase4_recovery import Outcome, ScenarioResult
from queue_load_test.harness.phase6_camoufox_benchmark import foreign_backend, make_backend
from queue_load_test.harness.phase7_patchright_benchmark import (
    ConcurrencyCase,
    MonitoringProfile,
    _parse_profiles,
    add_queued_work_check,
    backend_summary,
    concurrency_families,
    concurrency_stop_reason,
    foreign_for,
    monitoring_polling_policy,
    summarize_samples,
)
from queue_load_test.models import BrowserBackendName

PATCHRIGHT = BrowserBackendName.PATCHRIGHT
CHROME = BrowserBackendName.CHROME


def test_benchmark_backends_exclude_camoufox_and_pair_candidate_with_control() -> None:
    assert isinstance(make_backend(PATCHRIGHT), PatchrightBackend)
    assert isinstance(make_backend(CHROME), ChromeBackend)
    assert foreign_for(PATCHRIGHT) is CHROME
    assert foreign_for(CHROME) is PATCHRIGHT
    # Phase 6 pairings are unchanged; Patchright rows are rejected by Chrome there too.
    assert foreign_backend(BrowserBackendName.CAMOUFOX) is CHROME
    assert foreign_backend(CHROME) is BrowserBackendName.CAMOUFOX
    assert foreign_backend(PATCHRIGHT) is CHROME


def test_concurrency_families_stay_inside_architectural_limits() -> None:
    families = concurrency_families(PATCHRIGHT, (1, 5, 10, 20, 25, 30), (50, 125))

    single, multi = families
    assert [case.contexts for case in single] == [1, 5, 10, 20, 25]
    assert all(case.processes == 1 for case in single)
    # 125 contexts would need 5 processes, above CHROME_PROCESS_COUNT <= 4.
    assert [(case.processes, case.contexts) for case in multi] == [(2, 50)]
    assert all(case.contexts_per_process <= 25 for family in families for case in family)
    assert multi[0].label == "patchright-p2-c50"
    assert multi[0].family == "patchright-p2"


def _healthy_case(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "COMPLETED",
        "error_category": None,
        "configured_contexts": 5,
        "achieved_active_contexts": 5,
        "failures": {
            "context_creation_failures": 0,
            "browser_crashes": 0,
            "navigation_failures": 0,
            "identity_mismatches": 0,
            "browser_operation_timeouts": 0,
        },
        "resources": {
            "application_rss_bytes": {"peak": 100.0},
            "browser_tree_rss_bytes": {"peak": 100.0},
            "browser_cpu_percent": {"average": 10.0},
        },
        "reacquire": {"failures": 0},
        "restoration": {"restores": 15, "successes": 15},
        "health_probe_seconds": 0.1,
        "final": {"new_main_processes_after_shutdown": 0},
    }
    result.update(overrides)
    return result


def test_concurrency_stop_reason_adds_reacquire_churn_wedge_and_leak_evidence() -> None:
    assert concurrency_stop_reason(_healthy_case()) is None
    assert concurrency_stop_reason(_healthy_case(reacquire={"failures": 1})) == (
        "reacquisition failures"
    )
    assert concurrency_stop_reason(
        _healthy_case(restoration={"restores": 15, "successes": 14})
    ) == "churn sweep restore failures"
    assert "wedged" in str(concurrency_stop_reason(_healthy_case(health_probe_seconds=None)))
    assert concurrency_stop_reason(
        _healthy_case(final={"new_main_processes_after_shutdown": 1})
    ) == "process leak after shutdown"
    # Phase 6 objective rules still apply first.
    assert concurrency_stop_reason(_healthy_case(achieved_active_contexts=4)) == (
        "context shortfall"
    )


def test_monitoring_policy_makes_every_active_session_due_each_interval() -> None:
    policy = monitoring_polling_policy(5.0, 0.5)

    assert policy.jitter_seconds == 0.5
    assert {
        policy.default_seconds,
        policy.pre_queue_min_seconds,
        policy.pre_queue_max_seconds,
        policy.active_early_min_seconds,
        policy.active_mid_max_seconds,
        policy.serviced_soon_max_seconds,
        policy.turn_started_seconds,
    } == {5.0}


def test_monitoring_profiles_parse_and_reject_limits() -> None:
    assert _parse_profiles("2x4, 2x8") == (
        MonitoringProfile(processes=2, workers=4),
        MonitoringProfile(processes=2, workers=8),
    )
    assert MonitoringProfile(processes=2, workers=8).label == "p2-w8"
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_profiles("5x4")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_profiles("1x26")


def test_queued_work_check_reads_the_sweep_queue_depth() -> None:
    busy = ScenarioResult(key="k", title="t", evidence="e")
    busy.measurements["sweep"] = {"queue_depth": {"average": 1.0, "peak": 3}}
    idle = ScenarioResult(key="k", title="t", evidence="e")
    idle.measurements["sweep"] = {"queue_depth": {"average": 0.0, "peak": 0}}

    assert add_queued_work_check(busy).checks["queued_work_existed_during_failure"]
    assert not add_queued_work_check(idle).checks["queued_work_existed_during_failure"]


def test_backend_summary_counts_only_that_backends_scenarios() -> None:
    passed = ScenarioResult(key="churn_patchright", title="", evidence="")
    passed.check("a", True)
    failed = ScenarioResult(key="stuck_navigation_patchright", title="", evidence="")
    failed.check("b", True)
    failed.check("c", False)
    control = ScenarioResult(key="churn_chrome", title="", evidence="")
    control.check("d", False)
    for scenario in (passed, failed, control):
        scenario.finish()

    summary = backend_summary([passed, failed, control], PATCHRIGHT)

    assert passed.outcome is Outcome.PASS and failed.outcome is Outcome.FAIL
    assert summary["scenarios_passed"] == 1
    assert summary["scenarios_failed"] == 1
    assert summary["checks_passed"] == 2
    assert summary["checks_failed"] == 1
    assert summary["failed_checks"] == {"stuck_navigation_patchright": ["c"]}


def test_sample_summary_ignores_unavailable_values() -> None:
    samples: list[dict[str, float | int | None]] = [
        {"active_contexts": 2, "browser_level_contexts": None},
        {"active_contexts": 4, "browser_level_contexts": 3},
    ]

    summary = summarize_samples(samples, ("active_contexts", "browser_level_contexts"))

    assert summary["active_contexts"] == {"average": 3.0, "peak": 4.0}
    assert summary["browser_level_contexts"] == {"average": 3.0, "peak": 3.0}


def test_concurrency_case_contexts_per_process_rounds_up() -> None:
    assert ConcurrencyCase(PATCHRIGHT, 2, 49).contexts_per_process == 25
    assert ConcurrencyCase(CHROME, 1, 1).label == "chrome-p1-c1"
