from queue_load_test.harness.phase7_patchright_runtime import _summary


def test_phase7_summary_preserves_aggregate_checks_without_session_material() -> None:
    result = {
        "passed": 1,
        "failed": 0,
        "evidence": {"browser_backend": "patchright"},
        "checks": [{"name": "identity_continuity", "passed": True, "detail": {}}],
    }

    summary = _summary(result)

    assert summary["scenario_checks"] == 1
    assert summary["failed_checks"] == []
    assert "queue_id" not in repr(summary).lower()
