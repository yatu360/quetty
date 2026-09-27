import asyncio
import json
import re
from pathlib import Path

import pytest

from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.harness.phase4_recovery import (
    Outcome,
    PopulationLayout,
    ScenarioResult,
    queue_id_for,
)
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.metrics.prometheus import FORBIDDEN_LABEL_NAMES

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "docs" / "dashboards" / "queue_load_test_phase4.json"
RESULT = ROOT / "docs" / "results" / "phase4_recovery_result.json"


def test_population_layout_at_ten_thousand_is_disjoint_and_complete() -> None:
    layout = PopulationLayout(10_000, 20)
    named = {
        "expired_leases": layout.expired_leases,
        "live_leases": layout.live_leases,
        "monitorable": layout.monitorable,
        "admitted": layout.admitted,
        "expired_status": layout.expired_status,
        "creation_failed": layout.creation_failed,
    }
    covered: list[int] = []
    for indices in named.values():
        covered.extend(indices)
    assert sorted(covered) == list(range(10_000))
    assert len(layout.expired_leases) == 200
    assert len(layout.live_leases) == 200
    assert len(layout.admitted) == 200
    assert len(layout.creation_failed) == 100
    assert layout.valid_queue_ids == 9_900
    groups = (
        layout.navigation_failure,
        layout.transfer_fallback,
        layout.storage_corrupt,
        layout.storage_unavailable,
        layout.context_failure,
        layout.identity_mismatch,
        layout.navigation_timeout,
    )
    members = [index for group in groups for index in group]
    assert len(members) == len(set(members))
    assert all(layout.monitorable.start <= index < layout.due_end for index in members)
    assert layout.expected_permanent_failures == set(layout.storage_corrupt) | set(
        layout.identity_mismatch
    )


def test_population_layout_rejects_unusable_sizes() -> None:
    with pytest.raises(ValueError, match="population"):
        PopulationLayout(100, 1)
    with pytest.raises(ValueError, match="fault groups"):
        PopulationLayout(500, 200)


def test_scenario_outcome_requires_executed_checks() -> None:
    assert ScenarioResult("a", "A", "local").finish().outcome is Outcome.UNKNOWN
    passed = ScenarioResult("b", "B", "local")
    passed.check("x", True)
    assert passed.finish().outcome is Outcome.PASS
    failed = ScenarioResult("c", "C", "local")
    failed.check("x", True)
    failed.check("y", False)
    assert failed.finish().outcome is Outcome.FAIL


async def _get(simulator: LocalQueueSimulator, path: str, cookie: str | None = None) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", simulator.port)
    headers = f"Cookie: queue_id={cookie}\r\n" if cookie else ""
    writer.write(f"GET {path} HTTP/1.1\r\nHost: local\r\n{headers}\r\n".encode())
    await writer.drain()
    body = await reader.read()
    writer.close()
    return body


async def test_local_simulator_fault_sets_are_deterministic() -> None:
    simulator = LocalQueueSimulator()
    await simulator.start()
    try:
        healthy, down, mismatch, empty = (queue_id_for(index) for index in range(4))
        simulator.transfer_down_ids.add(down)
        simulator.mismatch_ids.add(mismatch)
        simulator.empty_response_ids.add(empty)

        ok = await _get(simulator, f"/queue?q={healthy}")
        assert b"200" in ok.split(b"\r\n", 1)[0]
        assert f"q={healthy}".encode() in ok
        assert b"503" in (await _get(simulator, f"/queue?q={down}")).split(b"\r\n", 1)[0]
        fallback = await _get(simulator, "/queue", cookie=down)
        assert f"q={down}".encode() in fallback
        assert f"q={mismatch}-other".encode() in await _get(simulator, f"/queue?q={mismatch}")
        assert await _get(simulator, f"/queue?q={empty}") == b""
        fresh = await _get(simulator, "/queue")
        assert b"sim-new-00001" in fresh
    finally:
        await simulator.close()


def _dashboard_expressions() -> list[str]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def test_dashboard_references_only_exported_low_cardinality_metrics() -> None:
    metrics = PrometheusMetrics()
    exported = set()
    for family in metrics.registry.collect():
        for sample in family.samples:
            exported.add(sample.name)
    functions = {"rate", "sum", "by", "histogram_quantile", "le", "operation"}
    expressions = _dashboard_expressions()
    assert expressions
    for expression in expressions:
        for forbidden in FORBIDDEN_LABEL_NAMES:
            assert not re.search(rf"\b{forbidden}\b", expression), expression
        names = set(re.findall(r"[a-z_][a-z0-9_]*", expression)) - functions
        referenced = {name for name in names if "_" in name}
        assert referenced, expression
        assert referenced <= exported, (expression, referenced - exported)


def test_checked_in_recovery_result_contains_no_identities_or_urls() -> None:
    if not RESULT.exists():
        pytest.skip("recovery result not generated yet")
    text = RESULT.read_text(encoding="utf-8")
    assert "sim-q-" not in text
    assert "sim-session-" not in text
    assert "sim-new-" not in text
    assert "http://" not in text
    assert "https://" not in text
    payload = json.loads(text)
    assert payload["population"] == 10_000
    outcomes = {scenario["outcome"] for scenario in payload["scenarios"]}
    assert outcomes <= {"PASS", "FAIL", "UNKNOWN"}
