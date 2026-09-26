import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from queue_load_test.config import Settings
from queue_load_test.harness import (
    MechanismAttemptRecord,
    RestoreBenchmarkAttempt,
    RestoreBenchmarkMode,
    RestoreBenchmarkReport,
    RestoreBenchmarkRunner,
    percentile,
    summarize_restore_attempts,
)
from queue_load_test.harness.phase2_restore import run_phase2_restore_benchmark
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.transfer import (
    RestoreAttempt,
    RestoreFailure,
    RestoreMethod,
    SessionRestoreResult,
)

STARTED = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def benchmark_attempt(
    *,
    success: bool,
    identity_match: bool | None,
    duration: float,
    mode: RestoreBenchmarkMode = RestoreBenchmarkMode.TRANSFER_ONLY,
    method: RestoreMethod = RestoreMethod.TRANSFER,
    failure: RestoreFailure | None = None,
    fallback: bool = False,
) -> RestoreBenchmarkAttempt:
    mechanisms = [
        MechanismAttemptRecord(
            method=method,
            success=success,
            observed_queue_id="queue-1" if identity_match is not None else None,
            identity_match=identity_match,
            error_category=failure.value if failure is not None else None,
            browser_context_failure=failure is RestoreFailure.NAVIGATION_FAILED,
        )
    ]
    if fallback:
        mechanisms.insert(
            0,
            MechanismAttemptRecord(
                method=RestoreMethod.TRANSFER,
                success=False,
                observed_queue_id=None,
                identity_match=None,
                error_category=RestoreFailure.TRANSFER_UNAVAILABLE.value,
                browser_context_failure=False,
            ),
        )
    return RestoreBenchmarkAttempt(
        session_id="session-1",
        expected_queue_id="queue-1",
        observed_queue_id="queue-1" if identity_match is not None else None,
        benchmark_mode=mode,
        restore_method=method,
        started_at=STARTED,
        duration_seconds=duration,
        success=success,
        identity_match=identity_match,
        resulting_status=QueueStatus.ACTIVE_QUEUE if success else QueueStatus.FAILED,
        error_category=failure.value if failure is not None else None,
        browser_context_failure=failure is RestoreFailure.NAVIGATION_FAILED,
        fallback_used=fallback,
        mechanism_attempts=tuple(mechanisms),
    )


def test_aggregation_counts_failures_mismatches_fallback_and_mechanisms() -> None:
    attempts = (
        benchmark_attempt(success=True, identity_match=True, duration=1),
        benchmark_attempt(
            success=True,
            identity_match=True,
            duration=2,
            mode=RestoreBenchmarkMode.HYBRID,
            method=RestoreMethod.STORAGE_STATE,
            fallback=True,
        ),
        benchmark_attempt(
            success=False,
            identity_match=False,
            duration=3,
            failure=RestoreFailure.IDENTITY_MISMATCH,
        ),
    )

    summary = summarize_restore_attempts(attempts)

    assert summary["overall"] == {
        "attempts": 3,
        "successes": 2,
        "failures": 1,
        "success_rate": 2 / 3,
    }
    assert summary["identity_mismatches"] == 1
    assert summary["identity_mismatch_rate"] == 1 / 3
    assert summary["average_duration_seconds"] == 2
    assert summary["p50_duration_seconds"] == 2
    assert summary["p95_duration_seconds"] == pytest.approx(2.9)
    assert summary["fallback_usage"] == 1
    mechanisms = summary["mechanisms"]
    assert isinstance(mechanisms, dict)
    assert mechanisms["TRANSFER"]["attempts"] == 3
    assert mechanisms["STORAGE_STATE"]["success_rate"] == 1
    assert summary["error_categories"] == {
        "IDENTITY_MISMATCH": 1,
        "TRANSFER_UNAVAILABLE": 1,
    }


def test_percentile_is_interpolated_and_validated() -> None:
    assert percentile([], 50) is None
    assert percentile([1], 95) == 1
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    with pytest.raises(ValueError, match="between 0 and 100"):
        percentile([1], 101)


def test_json_report_records_identity_but_never_transfer_url(tmp_path: Path) -> None:
    attempt = benchmark_attempt(success=True, identity_match=True, duration=0.5)
    report = RestoreBenchmarkReport(
        generated_at=STARTED,
        sample_size=1,
        modes=(RestoreBenchmarkMode.TRANSFER_ONLY,),
        attempts=(attempt,),
        summary=summarize_restore_attempts((attempt,)),
    )
    output = tmp_path / "restore.json"

    report.write_json(output)

    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["attempts"][0]["expected_queue_id"] == "queue-1"
    assert "transfer_url" not in text
    assert "https://queue.secret.test" not in text
    assert "Phase 2 HYBRID" in report.render_text()


class ScriptedRestorer:
    def __init__(self, results: list[SessionRestoreResult]) -> None:
        self.results = deque(results)
        self.calls: list[RestoreMethod | None] = []

    async def restore(self, _: QueueSession) -> SessionRestoreResult:
        self.calls.append(None)
        return self.results.popleft()

    async def restore_with_method(
        self,
        _: QueueSession,
        method: RestoreMethod,
    ) -> SessionRestoreResult:
        self.calls.append(method)
        return self.results.popleft()


def hybrid_session() -> QueueSession:
    return QueueSession(
        session_id="session-1",
        queue_id="queue-expected",
        transfer_url="https://queue.secret.test/journey?q=queue-expected",
        mode=SessionMode.HYBRID,
        status=QueueStatus.PARKED,
        state_path=Path(".browser-state/session-1.json"),
    )


async def test_runner_supports_each_mode_and_accounts_for_hybrid_fallback(
    tmp_path: Path,
) -> None:
    matching = RestoreAttempt(
        method=RestoreMethod.TRANSFER,
        success=True,
        observed_queue_id="queue-expected",
        identity_match=True,
        progress=QueueProgress(session_id="session-1", active_queue=True),
    )
    transfer_failure = RestoreAttempt(
        method=RestoreMethod.TRANSFER,
        success=False,
        failure=RestoreFailure.TRANSFER_UNAVAILABLE,
    )
    storage_success = RestoreAttempt(
        method=RestoreMethod.STORAGE_STATE,
        success=True,
        observed_queue_id="queue-expected",
        identity_match=True,
        progress=QueueProgress(session_id="session-1", pre_queue=True),
    )
    restorer = ScriptedRestorer(
        [
            SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=True,
                expected_queue_id="queue-expected",
                observed_queue_id="queue-expected",
                identity_match=True,
                progress=matching.progress,
                attempts=(matching,),
            ),
            SessionRestoreResult(
                method=RestoreMethod.STORAGE_STATE,
                success=True,
                expected_queue_id="queue-expected",
                observed_queue_id="queue-expected",
                identity_match=True,
                progress=storage_success.progress,
                attempts=(storage_success,),
            ),
            SessionRestoreResult(
                method=RestoreMethod.STORAGE_STATE,
                success=True,
                expected_queue_id="queue-expected",
                observed_queue_id="queue-expected",
                identity_match=True,
                progress=storage_success.progress,
                attempts=(transfer_failure, storage_success),
            ),
        ]
    )

    report = await RestoreBenchmarkRunner(restorer).run(
        [hybrid_session()],
        sample_size=1,
    )

    assert restorer.calls == [RestoreMethod.TRANSFER, RestoreMethod.STORAGE_STATE, None]
    assert len(report.attempts) == 3
    assert report.attempts[2].fallback_used
    assert report.summary["fallback_usage"] == 1
    assert report.summary["overall"]["successes"] == 3
    output = tmp_path / "runner-report.json"
    report.write_json(output)
    assert "https://queue.secret.test" not in output.read_text(encoding="utf-8")


async def test_staging_benchmark_requires_both_explicit_environment_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_STAGING_TESTS", raising=False)
    monkeypatch.delenv("RUN_PHASE2_RESTORE_BENCHMARK", raising=False)
    settings = Settings(
        STAGING_URL="https://staging.example.test",
        DATABASE_URL=f"sqlite:///{tmp_path / 'sessions.sqlite3'}",
        STATE_DIRECTORY=tmp_path / "state",
    )

    with pytest.raises(RuntimeError, match="RUN_STAGING_TESTS=1"):
        await run_phase2_restore_benchmark(
            settings,
            report_path=tmp_path / "report.json",
            sample_size=1,
        )
