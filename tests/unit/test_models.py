from pathlib import Path

import pytest

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode


def test_enum_parsing_is_case_and_separator_tolerant() -> None:
    assert SessionMode.parse("hybrid") is SessionMode.HYBRID
    assert SessionMode.parse("transfer-only") is SessionMode.TRANSFER_ONLY
    assert QueueStatus.parse("active-queue") is QueueStatus.ACTIVE_QUEUE
    assert QueueStatus.parse("PRE_QUEUE") is QueueStatus.PRE_QUEUE


def test_enum_parsing_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="Unknown QueueStatus"):
        QueueStatus.parse("waiting-room")


def test_queue_status_contains_required_lifecycle_states() -> None:
    expected = {
        "NEW",
        "CREATING",
        "PRE_QUEUE",
        "ACTIVE_QUEUE",
        "PARKED",
        "CHECKING",
        "PAUSED",
        "SERVICED_SOON",
        "TURN_STARTED",
        "READY",
        "ADMITTED",
        "CONNECTION_LOST",
        "EXPIRED",
        "FAILED",
    }

    assert {status.value for status in QueueStatus} == expected


def test_queue_session_construction_keeps_identity_separate_from_progress() -> None:
    session = QueueSession(
        queue_id="queue-a",
        transfer_url="https://staging.example.test/transfer",
        mode="hybrid",
        status="pre-queue",
        state_path=Path(".browser-state/session.json"),
    )

    assert session.queue_id == "queue-a"
    assert session.status is QueueStatus.PRE_QUEUE
    assert session.mode is SessionMode.HYBRID
    assert session.state_path == Path(".browser-state/session.json")
    assert session.created_at.tzinfo is not None
    assert not hasattr(session, "users_ahead")


def test_queue_id_does_not_imply_active_queue() -> None:
    session = QueueSession(
        queue_id="known-queue",
        transfer_url="https://staging.example.test/transfer",
        mode=SessionMode.HYBRID,
        state_path=Path(".browser-state/session.json"),
    )

    assert session.status is QueueStatus.NEW


def test_progress_values_are_optional() -> None:
    progress = QueueProgress(session_id="session-1")

    assert progress.queue_number is None
    assert progress.users_ahead is None
    assert progress.progress_percentage is None
    assert progress.estimated_wait_text is None
    assert progress.expected_service_time is None
    assert progress.queue_paused is None
    assert progress.serviced_soon is None
    assert progress.turn_started is None
    assert progress.connection_lost is None


def test_progress_percentage_validation() -> None:
    with pytest.raises(ValueError, match="progress_percentage"):
        QueueProgress(session_id="session-1", progress_percentage=101)
