import json
from pathlib import Path

import pytest

from queue_load_test.config import Settings
from queue_load_test.harness import AcceptanceStatus, Phase1AcceptanceRecorder
from queue_load_test.harness.staging import run_staging_acceptance


def test_empty_acceptance_report_is_unknown_and_has_scope_warning() -> None:
    report = Phase1AcceptanceRecorder().build_report()

    assert all(result.status is AcceptanceStatus.UNKNOWN for result in report.results)
    assert "do not generalize" in report.scope_warning
    assert "10-session" in report.scope_warning


def test_acceptance_recorder_reports_pass_fail_and_aggregate_measurements(
    tmp_path: Path,
) -> None:
    recorder = Phase1AcceptanceRecorder()
    for index in range(10):
        recorder.record_fresh_identity(f"queue-{index}")
        recorder.record_transfer_extraction(success=True)
        recorder.record_transfer_restore(success=True, identity_match=True)
        recorder.record_storage_restore(success=True, identity_match=True)
    recorder.pre_queue_with_identity = 10
    recorder.pre_queue_to_active_same_identity = 10
    recorder.progress_observations = 5
    recorder.last_updated_changes = 2
    recorder.serviced_soon_observed = True
    recorder.turn_started_observed = True
    recorder.admitted_observed = True
    recorder.restart_preserved = True
    recorder.measurements.context_creation_seconds.extend((0.1, 0.3))
    recorder.measurements.controller_cpu_seconds = 1.25

    report = recorder.build_report()
    output = tmp_path / "report.json"
    report.write_json(output)

    assert all(result.status is AcceptanceStatus.PASS for result in report.results)
    assert report.measurements["transfer_restore_success_rate"] == 1.0
    context_summary = report.measurements["context_creation_seconds"]
    assert isinstance(context_summary, dict)
    assert context_summary["average"] == 0.2
    saved = json.loads(output.read_text())
    assert saved["results"][0]["status"] == "PASS"
    assert "queue-0" not in output.read_text()


def test_duplicate_fresh_identity_and_failed_restore_are_failures() -> None:
    recorder = Phase1AcceptanceRecorder()
    for _ in range(10):
        recorder.record_fresh_identity("duplicate")
    recorder.record_transfer_restore(success=False, identity_match=False)

    report = recorder.build_report()

    assert report.results[0].status is AcceptanceStatus.FAIL
    assert report.results[4].status is AcceptanceStatus.FAIL
    assert report.measurements["identity_mismatches"] == 1


async def test_staging_harness_requires_explicit_environment_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_STAGING_TESTS", raising=False)
    settings = Settings(
        STAGING_URL="https://staging.example.test",
        TARGET_QUEUE_IDS=10,
        MAX_CONTEXTS_PER_BROWSER=5,
        MAX_ACTIVE_CONTEXTS=5,
        DATABASE_URL=f"sqlite:///{tmp_path / 'acceptance.sqlite3'}",
        STATE_DIRECTORY=tmp_path / "state",
    )

    with pytest.raises(RuntimeError, match="RUN_STAGING_TESTS=1"):
        await run_staging_acceptance(settings, report_path=tmp_path / "report.json")


async def test_staging_harness_rejects_non_phase1_profile(tmp_path: Path) -> None:
    settings = Settings(
        STAGING_URL="https://staging.example.test",
        TARGET_QUEUE_IDS=9,
        MAX_CONTEXTS_PER_BROWSER=5,
        MAX_ACTIVE_CONTEXTS=5,
        DATABASE_URL=f"sqlite:///{tmp_path / 'acceptance.sqlite3'}",
        STATE_DIRECTORY=tmp_path / "state",
    )

    with pytest.raises(ValueError, match="TARGET_QUEUE_IDS"):
        await run_staging_acceptance(settings, report_path=tmp_path / "report.json")
