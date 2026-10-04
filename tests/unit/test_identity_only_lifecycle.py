"""A valid Queue ID with no usable queue state is PRE_QUEUE (shared evaluator rule)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.direct_monitor.checker import validate_direct_observation
from queue_load_test.models import (
    MonitoringObservation,
    ObservationSource,
    QueuePageSignals,
    QueueProgress,
    QueueSession,
    QueueStatus,
    SessionMode,
    can_transition,
    evaluate_monitoring_observation,
    evaluate_queue_status,
    has_valid_queue_identity,
    identity_only_status,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import PollingPolicy, QueueSessionMonitor
from queue_load_test.scheduler.monitoring import browser_observation_from_restore_result
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
QUEUE_ID = "11111111-2222-3333-4444-555555555555"


def observation(
    progress: QueueProgress | None = None,
    *,
    page: QueuePageSignals | None = None,
    expected: str | None = QUEUE_ID,
    observed: str | None = QUEUE_ID,
    identity_match: bool | None = True,
    source: ObservationSource = ObservationSource.BROWSER_DOM,
) -> MonitoringObservation:
    return MonitoringObservation(
        source=source,
        session_id="s",
        observed_at=NOW,
        progress=progress or QueueProgress(session_id="s"),
        page=page or QueuePageSignals(),
        expected_queue_id=expected,
        observed_queue_id=observed,
        identity_match=identity_match,
    )


# --- Pure evaluator -------------------------------------------------------------------


def test_valid_queue_id_without_position_percentage_or_lifecycle_is_pre_queue() -> None:
    assert evaluate_monitoring_observation(observation()) is QueueStatus.PRE_QUEUE


def test_missing_progress_fields_with_a_valid_queue_id_is_pre_queue() -> None:
    progress = QueueProgress(
        session_id="s",
        queue_number=None,
        users_ahead=None,
        progress_percentage=None,
        estimated_wait_text=None,
        expected_service_time=None,
    )
    assert evaluate_monitoring_observation(observation(progress)) is QueueStatus.PRE_QUEUE


def test_one_progress_value_alone_still_uses_the_identity_fallback() -> None:
    # A lone percentage was never lifecycle evidence; the identity now decides.
    progress = QueueProgress(session_id="s", progress_percentage=40.0)
    assert evaluate_monitoring_observation(observation(progress)) is QueueStatus.PRE_QUEUE


def test_persisted_expected_queue_id_alone_is_enough() -> None:
    result = evaluate_monitoring_observation(
        observation(observed=None, identity_match=None)
    )
    assert result is QueueStatus.PRE_QUEUE


def test_observed_queue_id_alone_is_enough_without_an_expected_identity() -> None:
    result = evaluate_monitoring_observation(observation(expected=None))
    assert result is QueueStatus.PRE_QUEUE


@pytest.mark.parametrize(
    ("progress", "page", "expected"),
    [
        (QueueProgress(session_id="s", active_queue=True), None, QueueStatus.ACTIVE_QUEUE),
        (
            QueueProgress(session_id="s", queue_number="7", users_ahead=6),
            None,
            QueueStatus.ACTIVE_QUEUE,
        ),
        (None, QueuePageSignals(active_queue=True), QueueStatus.ACTIVE_QUEUE),
        (QueueProgress(session_id="s", queue_paused=True), None, QueueStatus.PAUSED),
        (QueueProgress(session_id="s", serviced_soon=True), None, QueueStatus.SERVICED_SOON),
        (QueueProgress(session_id="s", turn_started=True), None, QueueStatus.TURN_STARTED),
        (QueueProgress(session_id="s", first_in_line=True), None, QueueStatus.READY),
        (None, QueuePageSignals(admitted=True), QueueStatus.ADMITTED),
        (None, QueuePageSignals(expired=True), QueueStatus.EXPIRED),
        (
            QueueProgress(session_id="s", connection_lost=True),
            None,
            QueueStatus.CONNECTION_LOST,
        ),
    ],
)
def test_explicit_signals_override_the_identity_fallback(
    progress: QueueProgress | None,
    page: QueuePageSignals | None,
    expected: QueueStatus,
) -> None:
    assert evaluate_monitoring_observation(observation(progress, page=page)) is expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"identity_match": False},
        {"observed": "99999999-0000-0000-0000-000000000000", "identity_match": None},
        {"expected": None, "observed": None},
        {"expected": "  ", "observed": None},
    ],
)
def test_mismatched_or_absent_identity_never_becomes_pre_queue(kwargs: dict[str, Any]) -> None:
    assert evaluate_monitoring_observation(observation(**kwargs)) is QueueStatus.CHECKING
    assert has_valid_queue_identity(observation(**kwargs)) is False


def test_identity_only_fallback_never_moves_a_later_stage_backwards() -> None:
    for status in (
        QueueStatus.ACTIVE_QUEUE,
        QueueStatus.SERVICED_SOON,
        QueueStatus.TURN_STARTED,
        QueueStatus.READY,
    ):
        assert not can_transition(status, QueueStatus.PRE_QUEUE)
        assert identity_only_status(status) is status
        assert (
            evaluate_monitoring_observation(observation(), current_status=status) is status
        )
    for status in (
        QueueStatus.PARKED,
        QueueStatus.CHECKING,
        QueueStatus.CREATING,
        QueueStatus.PAUSED,
        QueueStatus.CONNECTION_LOST,
        QueueStatus.PRE_QUEUE,
    ):
        assert (
            evaluate_monitoring_observation(observation(), current_status=status)
            is QueueStatus.PRE_QUEUE
        )


def test_layout_only_evaluation_without_identity_is_unchanged() -> None:
    assert evaluate_queue_status(QueueProgress(session_id="s")) is QueueStatus.CHECKING


# --- Shared persistence paths -----------------------------------------------------------


def session(status: QueueStatus = QueueStatus.PARKED) -> QueueSession:
    return QueueSession(
        session_id="s",
        queue_id=QUEUE_ID,
        transfer_url=f"https://queue.example.test/?q={QUEUE_ID}",
        mode=SessionMode.HYBRID,
        status=status,
        state_path=Path("state/s.json"),
    )


def restore_result(
    *,
    success: bool = True,
    progress: QueueProgress | None = None,
    failure: RestoreFailure | None = None,
    observed: str | None = QUEUE_ID,
    identity_match: bool | None = True,
) -> SessionRestoreResult:
    return SessionRestoreResult(
        method=RestoreMethod.TRANSFER,
        success=success,
        expected_queue_id=QUEUE_ID,
        observed_queue_id=observed,
        identity_match=identity_match,
        progress=progress if progress is not None else QueueProgress(session_id="s"),
        failure=failure,
    )


class ScriptedRestorer:
    def __init__(self, result: SessionRestoreResult) -> None:
        self.result = result

    async def restore(self, _: QueueSession) -> SessionRestoreResult:
        return self.result


async def monitor_for(
    tmp_path: Path, result: SessionRestoreResult, status: QueueStatus = QueueStatus.PARKED
) -> tuple[QueueSessionMonitor, SQLiteSessionRepository, QueueSession]:
    repository = SQLiteSessionRepository(tmp_path / "lifecycle.sqlite3")
    stored = await repository.create(session(status))
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=ScriptedRestorer(result),
        polling_policy=PollingPolicy(jitter_seconds=0),
        clock=lambda: NOW,
    )
    return monitor, repository, stored


async def test_browser_observation_with_identity_and_no_state_persists_pre_queue(
    tmp_path: Path,
) -> None:
    monitor, repository, stored = await monitor_for(tmp_path, restore_result())

    outcome = await monitor.check(stored)

    persisted = await repository.get("s")
    assert outcome.success and outcome.observed_status is QueueStatus.PRE_QUEUE
    assert persisted is not None and persisted.status is QueueStatus.PRE_QUEUE
    assert persisted.queue_id == QUEUE_ID
    assert persisted.transfer_url == f"https://queue.example.test/?q={QUEUE_ID}"
    # PRE_QUEUE cadence, not the generic "unknown" cadence.
    assert persisted.next_check_at is not None
    assert (persisted.next_check_at - NOW).total_seconds() == pytest.approx(180.0)
    await repository.close()


async def test_refresh_now_uses_the_same_rule(tmp_path: Path) -> None:
    # Refresh Now runs the run's monitor ``check`` for the leased session.
    monitor, repository, stored = await monitor_for(tmp_path, restore_result())
    outcome = await monitor.check(stored)
    assert outcome.observed_status is QueueStatus.PRE_QUEUE
    await repository.close()


async def test_active_session_with_temporarily_missing_state_stays_active(
    tmp_path: Path,
) -> None:
    monitor, repository, stored = await monitor_for(
        tmp_path, restore_result(), QueueStatus.ACTIVE_QUEUE
    )
    outcome = await monitor.check(stored)
    persisted = await repository.get("s")
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    assert persisted is not None and persisted.queue_id == QUEUE_ID
    await repository.close()


async def test_identity_mismatch_is_not_pre_queue_and_keeps_the_expected_id(
    tmp_path: Path,
) -> None:
    monitor, repository, stored = await monitor_for(
        tmp_path,
        restore_result(
            success=False,
            failure=RestoreFailure.IDENTITY_MISMATCH,
            observed="99999999-0000-0000-0000-000000000000",
            identity_match=False,
        ),
    )
    outcome = await monitor.check(stored)
    persisted = await repository.get("s")
    assert outcome.observed_status is QueueStatus.FAILED
    assert persisted is not None and persisted.queue_id == QUEUE_ID
    await repository.close()


@pytest.mark.parametrize(
    "failure",
    [
        RestoreFailure.NAVIGATION_FAILED,
        RestoreFailure.HTTP_FAILURE,
        RestoreFailure.TRANSFER_UNAVAILABLE,
        RestoreFailure.IDENTITY_UNVERIFIED,
        RestoreFailure.PROXY_CONNECT_FAILED,
    ],
)
async def test_genuine_restore_failures_are_not_pre_queue(
    tmp_path: Path, failure: RestoreFailure
) -> None:
    monitor, repository, stored = await monitor_for(
        tmp_path,
        restore_result(success=False, failure=failure, observed=None, identity_match=None),
    )
    outcome = await monitor.check(stored)
    persisted = await repository.get("s")
    assert outcome.observed_status is QueueStatus.CONNECTION_LOST
    assert persisted is not None and persisted.queue_id == QUEUE_ID
    await repository.close()


def test_browser_normalization_carries_the_identity_into_the_evaluator() -> None:
    normalized = browser_observation_from_restore_result(
        session_id="s", result=restore_result(), observed_at=NOW
    )
    assert has_valid_queue_identity(normalized)
    assert evaluate_monitoring_observation(normalized) is QueueStatus.PRE_QUEUE


async def test_direct_observation_with_identity_only_is_accepted_and_persisted_pre_queue(
    tmp_path: Path,
) -> None:
    monitor, repository, stored = await monitor_for(tmp_path, restore_result())
    direct = observation(source=ObservationSource.DIRECT_RESPONSE)

    assert validate_direct_observation(stored, direct) is None
    outcome = await monitor.apply_direct_observation(stored, direct)
    persisted = await repository.get("s")
    assert outcome.observed_status is QueueStatus.PRE_QUEUE
    assert persisted is not None and persisted.status is QueueStatus.PRE_QUEUE
    assert persisted.queue_id == QUEUE_ID
    await repository.close()


def test_direct_identity_mismatch_still_falls_back_to_the_browser() -> None:
    mismatch = observation(
        source=ObservationSource.DIRECT_RESPONSE,
        observed="99999999-0000-0000-0000-000000000000",
        identity_match=False,
    )
    reason = validate_direct_observation(session(), mismatch)
    assert reason is not None and reason.value == "identity_mismatch"
