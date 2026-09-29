"""Quick Direct vs Headed benchmark against the local simulator (not Queue-it)."""

from pathlib import Path

import pytest

from queue_load_test.harness.phase8_monitoring_benchmark import QUICK_PROFILE, run_local_benchmark
from queue_load_test.models import BrowserBackendName

pytest.importorskip("psutil")


async def test_local_benchmark_reports_every_gate_from_measurements(tmp_path: Path) -> None:
    report = await run_local_benchmark(
        tmp_path, backend=BrowserBackendName.CHROME, profile=QUICK_PROFILE
    )

    gates = {name: value["result"] for name, value in report["gates"].items()}
    # Safety gates must hold; performance gates are reported, not predetermined.
    for name in (
        "direct_identity_safety",
        "observation_equivalence",
        "fallback_reliability",
        "restart_continuity",
        "request_cadence_safety",
        "sensitive_data_safety",
    ):
        assert gates[name] == "PASS", (name, report["gates"][name])
    assert gates["throughput_improvement"] in {"IMPROVED", "NOT_IMPROVED"}
    assert report["report_contains_secret"] is False
    assert report["queue_it_staging"]["status"] == "NOT RUN"
    assert report["default_strategy_changed"] is False
    assert report["direct"]["lifecycle"]["TURN_STARTED"] == "UNKNOWN"
