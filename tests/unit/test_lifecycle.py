from pathlib import Path

import pytest

from queue_load_test.models import (
    InvalidQueueTransition,
    QueuePageSignals,
    QueueProgress,
    QueueSession,
    QueueStatus,
    SessionMode,
    can_transition,
    evaluate_queue_status,
    validate_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (QueueStatus.NEW, QueueStatus.CREATING),
        (QueueStatus.CREATING, QueueStatus.PRE_QUEUE),
        (QueueStatus.PRE_QUEUE, QueueStatus.ACTIVE_QUEUE),
        (QueueStatus.ACTIVE_QUEUE, QueueStatus.SERVICED_SOON),
        (QueueStatus.SERVICED_SOON, QueueStatus.TURN_STARTED),
        (QueueStatus.TURN_STARTED, QueueStatus.READY),
        (QueueStatus.READY, QueueStatus.ADMITTED),
        (QueueStatus.ACTIVE_QUEUE, QueueStatus.PAUSED),
        (QueueStatus.PAUSED, QueueStatus.ACTIVE_QUEUE),
        (QueueStatus.CONNECTION_LOST, QueueStatus.CHECKING),
        (QueueStatus.PARKED, QueueStatus.CHECKING),
        (QueueStatus.FAILED, QueueStatus.CREATING),
    ],
)
def test_legal_transitions(current: QueueStatus, target: QueueStatus) -> None:
    assert can_transition(current, target)
    validate_transition(current, target)


def test_repeated_transition_is_idempotent() -> None:
    assert can_transition(QueueStatus.ACTIVE_QUEUE, QueueStatus.ACTIVE_QUEUE)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (QueueStatus.ADMITTED, QueueStatus.PRE_QUEUE),
        (QueueStatus.EXPIRED, QueueStatus.ACTIVE_QUEUE),
        (QueueStatus.CREATING, QueueStatus.ACTIVE_QUEUE),
        (QueueStatus.ACTIVE_QUEUE, QueueStatus.PRE_QUEUE),
        (QueueStatus.SERVICED_SOON, QueueStatus.READY),
        (QueueStatus.READY, QueueStatus.SERVICED_SOON),
    ],
)
def test_suspicious_transitions_are_rejected(
    current: QueueStatus,
    target: QueueStatus,
) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidQueueTransition, match=f"{current.value} -> {target.value}"):
        validate_transition(current, target)


def test_pre_queue_can_have_an_existing_queue_id() -> None:
    session = QueueSession(
        queue_id="known-before-active",
        transfer_url="https://staging.example.test/transfer",
        mode=SessionMode.HYBRID,
        status=QueueStatus.PRE_QUEUE,
        state_path=Path(".browser-state/session.json"),
    )

    evaluated = evaluate_queue_status(
        QueueProgress(session_id=session.session_id, progress_percentage=0),
        QueuePageSignals(pre_queue=True),
    )

    assert session.queue_id == "known-before-active"
    assert evaluated is QueueStatus.PRE_QUEUE


def test_active_queue_marker_does_not_require_progress() -> None:
    progress = QueueProgress(session_id="session-1")

    assert (
        evaluate_queue_status(progress, QueuePageSignals(active_queue=True))
        is QueueStatus.ACTIVE_QUEUE
    )


def test_active_queue_can_be_inferred_from_stable_progress_fields() -> None:
    progress = QueueProgress(session_id="session-1", users_ahead=42)

    assert evaluate_queue_status(progress) is QueueStatus.ACTIVE_QUEUE


def test_percentage_alone_is_not_lifecycle_truth() -> None:
    progress = QueueProgress(session_id="session-1", progress_percentage=75)

    assert evaluate_queue_status(progress) is QueueStatus.CHECKING


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (QueueProgress("s", queue_paused=True), QueueStatus.PAUSED),
        (QueueProgress("s", serviced_soon=True), QueueStatus.SERVICED_SOON),
        (QueueProgress("s", turn_started=True), QueueStatus.TURN_STARTED),
        (QueueProgress("s", first_in_line=True), QueueStatus.READY),
        (QueueProgress("s", connection_lost=True), QueueStatus.CONNECTION_LOST),
    ],
)
def test_progress_signals_evaluate_deterministically(
    progress: QueueProgress,
    expected: QueueStatus,
) -> None:
    assert evaluate_queue_status(progress) is expected


def test_higher_priority_connection_loss_wins_over_other_signals() -> None:
    progress = QueueProgress(
        "s",
        connection_lost=True,
        turn_started=True,
        serviced_soon=True,
        queue_paused=True,
    )
    page = QueuePageSignals(expired=True, admitted=True, active_queue=True)

    assert evaluate_queue_status(progress, page) is QueueStatus.CONNECTION_LOST


def test_missing_signals_result_in_checking() -> None:
    assert evaluate_queue_status(QueueProgress("s")) is QueueStatus.CHECKING
