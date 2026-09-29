"""Deterministic tests for the Phase 8 Prompt 7 benchmark report and gates."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.config import Settings
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase8_monitoring_benchmark import (
    LIFECYCLE_STAGES,
    BenchmarkProfile,
    _direct_delta,
    _direct_gaps,
    build_report,
    main,
    percentile,
    staging_not_run,
    staging_readiness,
    summarize,
)

PROFILE = BenchmarkProfile(sessions=6, window_seconds=10, poll_min_seconds=2, poll_max_seconds=3)
LIFECYCLE = {
    "PRE_QUEUE": "PASS",
    "ACTIVE_QUEUE": "PASS",
    "PAUSED": "UNKNOWN",
    "SERVICED_SOON": "PASS",
    "TURN_STARTED": "UNKNOWN",
    "ADMITTED": "UNKNOWN",
}


def strategy_result(*, direct: bool) -> dict[str, Any]:
    window: dict[str, Any] = {
        "check_duration_seconds": {"p50": 0.001 if direct else 0.2, "p95": 0.002 if direct else 0.4},
        "browser_context_mean": 0.1 if direct else 0.8,
        "browser_cpu_percent": {"mean": 5.0 if direct else 40.0},
        "app_cpu_percent": {"mean": 4.0},
        "browser_rss_mib_peak": 600.0,
        "app_rss_mib_peak": 100.0,
        "direct": (
            {
                "checks": 20,
                "browser_fallbacks": 2,
                "fallback_successes": 2,
                "direct_success_rate": 1.0,
                "direct_errors": 0,
                "schema_failures": 0,
                "disagreements": 0,
                "poll_hint_seconds": {"min": 2.5, "max": 2.5},
            }
            if direct
            else None
        ),
        "direct_request_gap_seconds": (
            {"count": 10, "min": 2.1, "p50": 2.6} if direct else {"count": 0, "min": None}
        ),
    }
    recovery: dict[str, Any] = {
        "restart": {
            "strategy_kept": True,
            "identities_preserved": True,
            "restart_to_first_check_seconds": 0.5,
            "restart_to_first_direct_request_seconds": 0.4 if direct else None,
        },
        "browser_failure": {"recovered_seconds": 2.0},
    }
    if direct:
        recovery["direct_state_expiry"] = {"direct_resumed": True}
    return {
        "strategy": "direct" if direct else "headed_window",
        "window": window,
        "lifecycle": dict(LIFECYCLE),
        "identity": {"queue_ids_changed": 0, "new_identities_during_monitoring": 0},
        "recovery": recovery,
        "secret_in_logs": False,
        "secret_in_surfaces": False,
    }


def report(**changes: Any) -> dict[str, Any]:
    headed = strategy_result(direct=False)
    direct = strategy_result(direct=True)
    for path, value in changes.items():
        target, *keys = path.split("__")
        node = headed if target == "headed" else direct
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return build_report(
        mode="local",
        backend="chrome",
        profile=PROFILE,
        headed=headed,
        direct=direct,
        staging=staging_not_run("not configured"),
    )


def gate(result: dict[str, Any], name: str) -> str:
    return str(result["gates"][name]["result"])


def test_percentile_and_summary_are_nearest_rank() -> None:
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 0.5) == 3.0
    assert percentile(values, 0.95) == 5.0
    assert percentile([], 0.5) is None
    assert summarize(values) == {"count": 5, "p50": 3.0, "p95": 5.0, "max": 5.0, "mean": 3.0}
    with pytest.raises(ValueError):
        percentile(values, 0)


def test_all_gates_pass_or_improve_for_a_healthy_comparison() -> None:
    result = report()

    assert {name: value["result"] for name, value in result["gates"].items()} == {
        "direct_identity_safety": "PASS",
        "observation_equivalence": "PASS",
        "fallback_reliability": "PASS",
        "direct_request_stability": "PASS",
        "restart_continuity": "PASS",
        "throughput_improvement": "IMPROVED",
        "browser_context_reduction": "IMPROVED",
        "cpu_ram_effect": "REDUCED",
        "request_cadence_safety": "PASS",
        "sensitive_data_safety": "PASS",
    }
    assert result["default_strategy_changed"] is False
    assert result["scope"] == "local_simulator_only" and result["queue_it_contacted"] is False
    stages = result["gates"]["observation_equivalence"]["stages"]
    assert stages["TURN_STARTED"] == {"headed": "UNKNOWN", "direct": "UNKNOWN"}


@pytest.mark.parametrize(
    ("change", "name", "expected"),
    [
        ({"direct__identity__queue_ids_changed": 1}, "direct_identity_safety", "FAIL"),
        ({"direct__identity__new_identities_during_monitoring": 1}, "direct_identity_safety",
         "FAIL"),
        ({"direct__lifecycle__ACTIVE_QUEUE": "FAIL"}, "observation_equivalence", "FAIL"),
        ({"direct__window__direct__disagreements": 1}, "observation_equivalence", "FAIL"),
        ({"direct__window__direct__fallback_successes": 1}, "fallback_reliability", "FAIL"),
        ({"direct__window__direct__direct_success_rate": 0.8}, "direct_request_stability",
         "FAIL"),
        ({"direct__window__direct__schema_failures": 1}, "direct_request_stability", "FAIL"),
        ({"direct__recovery__restart__restart_to_first_direct_request_seconds": None},
         "restart_continuity", "FAIL"),
        ({"direct__window__check_duration_seconds__p95": 1.0}, "throughput_improvement",
         "NOT_IMPROVED"),
        ({"direct__window__browser_context_mean": 2.0}, "browser_context_reduction",
         "NOT_IMPROVED"),
        ({"direct__window__browser_cpu_percent__mean": 90.0}, "cpu_ram_effect", "NOT_REDUCED"),
        ({"direct__window__direct_request_gap_seconds__min": 0.5}, "request_cadence_safety",
         "FAIL"),
        ({"headed__secret_in_logs": True}, "sensitive_data_safety", "FAIL"),
        ({"direct__secret_in_surfaces": True}, "sensitive_data_safety", "FAIL"),
    ],
)
def test_each_gate_reports_the_measured_regression(
    change: dict[str, Any], name: str, expected: str
) -> None:
    assert gate(report(**change), name) == expected


def test_missing_measurements_are_unknown_never_pass() -> None:
    result = report(
        direct__window__direct=None,
        direct__window__direct_request_gap_seconds={"count": 0, "min": None},
        direct__window__browser_context_mean=None,
        direct__window__browser_cpu_percent={"mean": None},
        direct__lifecycle=dict.fromkeys(LIFECYCLE_STAGES, "UNKNOWN"),
        direct__recovery={},
    )

    for name in (
        "observation_equivalence",
        "fallback_reliability",
        "direct_request_stability",
        "restart_continuity",
        "browser_context_reduction",
        "cpu_ram_effect",
        "request_cadence_safety",
    ):
        assert gate(result, name) == "UNKNOWN", name


def test_cadence_reports_polling_faster_than_the_response_hint() -> None:
    result = report(direct__window__direct__poll_hint_seconds={"min": 10.0, "max": 10.0})
    cadence = result["gates"]["request_cadence_safety"]
    assert cadence["result"] == "PASS"  # the shared policy floor is respected
    assert cadence["direct_faster_than_response_hint"] is True  # reported, not hidden


def test_queue_it_results_are_not_run_and_unknown_without_staging() -> None:
    staging = report()["queue_it_staging"]
    assert staging["status"] == "NOT RUN"
    assert set(staging["lifecycle"].values()) == {"UNKNOWN"}
    assert set(staging["gates"].values()) == {"UNKNOWN"}


def test_report_is_json_serializable_and_contains_no_identities() -> None:
    result = report()
    text = json.dumps(result)
    assert "queue_id" not in text.replace("queue_ids_changed", "")
    assert "session_id" not in text
    assert copy.deepcopy(result) == json.loads(text)


def test_direct_delta_and_gap_measurements() -> None:
    before = {
        "checks": 1,
        "direct_requests": 1,
        "direct_successes": 1,
        "browser_fallbacks": 0,
        "fallback_successes": 0,
        "disagreements": 0,
        "recipes_adopted": 0,
        "poll_hints_observed": 1,
        "poll_hint_min_seconds": 2.5,
        "poll_hint_max_seconds": 2.5,
        "reasons": {"timeout": 1},
    }
    after = {
        **before,
        "checks": 11,
        "direct_requests": 9,
        "direct_successes": 8,
        "browser_fallbacks": 3,
        "fallback_successes": 3,
        "reasons": {"timeout": 2, "identity_mismatch": 1, "malformed_response": 1},
    }

    delta = _direct_delta(before, after)

    assert delta is not None
    assert delta["checks"] == 10 and delta["direct_successes"] == 7
    assert delta["fallback_reasons"] == {
        "identity_mismatch": 1,
        "malformed_response": 1,
        "timeout": 1,
    }
    assert delta["direct_errors"] == 1
    assert delta["schema_failures"] == 1
    assert delta["identity_mismatches"] == 1
    assert delta["direct_success_rate"] == 0.7
    assert delta["browser_restores_avoided"] == 7
    assert _direct_delta(None, after) is None

    simulator = LocalQueueSimulator()
    simulator.direct_request_times = {"a": [0.0, 1.0, 3.5, 6.0], "b": [10.0, 12.0]}
    gaps = _direct_gaps(simulator, {"a": 1})
    assert gaps == {"count": 3, "min": 2.0, "p50": 2.5}
    assert _direct_gaps(None, {}) is None


def _settings(tmp_path: Path, **values: Any) -> Settings:
    base: dict[str, Any] = {
        "STAGING_URL": "https://staging.example.test/",
        "STATUS_DISCOVERY_ENABLED": True,
        "STATUS_DISCOVERY_SCOPE": "authorized_queue_it_staging",
        "STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING": True,
    }
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(LocalQueueSimulator.status_schema("authorized_queue_it_staging")),
        encoding="utf-8",
    )
    base["DIRECT_MONITOR_SCHEMA_PATH"] = str(schema)
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


GATES = {"RUN_STAGING_TESTS": "1", "RUN_PHASE8_MONITORING_BENCHMARK": "1"}


def test_staging_benchmark_requires_every_gate(tmp_path: Path) -> None:
    ready = _settings(tmp_path)

    assert staging_readiness(ready, environment=GATES, confirmed=True) == (True, "ready")
    assert staging_readiness(ready, environment=GATES, confirmed=False)[0] is False
    assert staging_readiness(ready, environment={"RUN_STAGING_TESTS": "1"}, confirmed=True)[
        0
    ] is False
    assert "STAGING_URL" in staging_readiness(
        _settings(tmp_path, STAGING_URL=None), environment=GATES, confirmed=True
    )[1]
    assert "discovery" in staging_readiness(
        _settings(tmp_path, STATUS_DISCOVERY_ENABLED=False), environment=GATES, confirmed=True
    )[1]
    assert "SCHEMA_PATH" in staging_readiness(
        _settings(tmp_path, DIRECT_MONITOR_SCHEMA_PATH=""), environment=GATES, confirmed=True
    )[1]
    simulator_schema = tmp_path / "simulator.json"
    simulator_schema.write_text(json.dumps(LocalQueueSimulator.status_schema()), encoding="utf-8")
    assert "authorized_queue_it_staging" in staging_readiness(
        _settings(tmp_path, DIRECT_MONITOR_SCHEMA_PATH=str(simulator_schema)),
        environment=GATES,
        confirmed=True,
    )[1]


def test_staging_cli_refuses_without_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_STAGING_TESTS", raising=False)
    monkeypatch.delenv("RUN_PHASE8_MONITORING_BENCHMARK", raising=False)
    with pytest.raises(SystemExit, match="NOT RUN"):
        main(["--mode", "staging", "--confirm-authorized-staging"])
    with pytest.raises(SystemExit, match="sessions"):
        main(["--sessions", "0"])
