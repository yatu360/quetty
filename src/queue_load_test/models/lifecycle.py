"""Queue lifecycle transitions and source-neutral observation evaluation."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from queue_load_test.models.progress import QueueProgress
from queue_load_test.models.session import QueueStatus


class InvalidQueueTransition(ValueError):
    """Raised when a session attempts a suspicious lifecycle transition."""


_INTERRUPT_STATES = frozenset(
    {
        QueueStatus.PARKED,
        QueueStatus.CHECKING,
        QueueStatus.CONNECTION_LOST,
        QueueStatus.EXPIRED,
        QueueStatus.FAILED,
    }
)
_OBSERVABLE_QUEUE_STATES = frozenset(
    {
        QueueStatus.PRE_QUEUE,
        QueueStatus.ACTIVE_QUEUE,
        QueueStatus.SERVICED_SOON,
        QueueStatus.TURN_STARTED,
        QueueStatus.READY,
        QueueStatus.ADMITTED,
    }
)

_ALLOWED_TRANSITIONS: dict[QueueStatus, frozenset[QueueStatus]] = {
    QueueStatus.NEW: frozenset({QueueStatus.CREATING, QueueStatus.FAILED}),
    QueueStatus.CREATING: frozenset({QueueStatus.PRE_QUEUE}) | _INTERRUPT_STATES,
    QueueStatus.PRE_QUEUE: (
        frozenset({QueueStatus.ACTIVE_QUEUE, QueueStatus.PAUSED, QueueStatus.ADMITTED})
        | _INTERRUPT_STATES
    ),
    QueueStatus.ACTIVE_QUEUE: (
        frozenset({QueueStatus.SERVICED_SOON, QueueStatus.PAUSED, QueueStatus.ADMITTED})
        | _INTERRUPT_STATES
    ),
    QueueStatus.SERVICED_SOON: (
        frozenset({QueueStatus.TURN_STARTED, QueueStatus.PAUSED, QueueStatus.ADMITTED})
        | _INTERRUPT_STATES
    ),
    QueueStatus.TURN_STARTED: frozenset({QueueStatus.READY, QueueStatus.ADMITTED})
    | _INTERRUPT_STATES,
    QueueStatus.READY: frozenset({QueueStatus.ADMITTED}) | _INTERRUPT_STATES,
    QueueStatus.PAUSED: _OBSERVABLE_QUEUE_STATES | _INTERRUPT_STATES,
    QueueStatus.CONNECTION_LOST: _OBSERVABLE_QUEUE_STATES | _INTERRUPT_STATES,
    QueueStatus.PARKED: (
        _OBSERVABLE_QUEUE_STATES | _INTERRUPT_STATES | {QueueStatus.PAUSED}
    ),
    QueueStatus.CHECKING: (
        _OBSERVABLE_QUEUE_STATES | _INTERRUPT_STATES | {QueueStatus.PAUSED}
    ),
    QueueStatus.FAILED: frozenset({QueueStatus.CREATING}),
    QueueStatus.ADMITTED: frozenset(),
    QueueStatus.EXPIRED: frozenset(),
}


def can_transition(current: QueueStatus, target: QueueStatus) -> bool:
    """Return whether a transition is legal; repeated observations are idempotent."""

    current = QueueStatus.parse(current)
    target = QueueStatus.parse(target)
    return current is target or target in _ALLOWED_TRANSITIONS[current]


def validate_transition(current: QueueStatus, target: QueueStatus) -> None:
    """Reject backward, terminal, or otherwise suspicious transitions."""

    current = QueueStatus.parse(current)
    target = QueueStatus.parse(target)
    if not can_transition(current, target):
        raise InvalidQueueTransition(f"Illegal queue transition: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class QueuePageSignals:
    """Layout-independent page markers supplied by a future browser adapter."""

    expired: bool | None = None
    admitted: bool | None = None
    pre_queue: bool | None = None
    active_queue: bool | None = None


class ObservationSource(StrEnum):
    """How a domain monitoring observation was obtained."""

    BROWSER_DOM = "browser_dom"
    DIRECT_RESPONSE = "direct_response"


@dataclass(frozen=True, slots=True)
class MonitoringObservation:
    """Browser-neutral input to the one authoritative lifecycle evaluator."""

    source: ObservationSource
    session_id: str
    observed_at: datetime
    progress: QueueProgress
    page: QueuePageSignals = QueuePageSignals()
    expected_queue_id: str | None = field(default=None, repr=False)
    observed_queue_id: str | None = field(default=None, repr=False)
    identity_match: bool | None = None
    redirect_present: bool | None = None
    poll_after_seconds: float | None = None
    missing_fields: tuple[str, ...] = ()
    unknown_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.progress.session_id != self.session_id:
            raise ValueError("observation progress belongs to another session")
        if self.poll_after_seconds is not None and self.poll_after_seconds < 0:
            raise ValueError("poll_after_seconds cannot be negative")


def evaluate_queue_status(
    progress: QueueProgress,
    page: QueuePageSignals | None = None,
) -> QueueStatus:
    """Evaluate status from ordered page and progress signals.

    A percentage by itself is deliberately insufficient evidence of an active
    queue. Explicit page markers work even when all progress fields are absent.
    """

    page = page or QueuePageSignals()
    if progress.connection_lost is True:
        return QueueStatus.CONNECTION_LOST
    if page.expired is True:
        return QueueStatus.EXPIRED
    if page.admitted is True:
        return QueueStatus.ADMITTED
    if progress.turn_started is True:
        return QueueStatus.TURN_STARTED
    if progress.first_in_line is True:
        return QueueStatus.READY
    if progress.serviced_soon is True:
        return QueueStatus.SERVICED_SOON
    if progress.queue_paused is True:
        return QueueStatus.PAUSED
    if page.pre_queue is True or progress.pre_queue is True:
        return QueueStatus.PRE_QUEUE
    if page.active_queue is True or progress.active_queue is True:
        return QueueStatus.ACTIVE_QUEUE

    active_evidence = sum(
        value is not None
        for value in (
            progress.queue_number,
            progress.users_ahead,
            progress.estimated_wait_text,
            progress.expected_service_time,
        )
    )
    if active_evidence >= 2:
        return QueueStatus.ACTIVE_QUEUE
    return QueueStatus.CHECKING


def has_valid_queue_identity(observation: MonitoringObservation) -> bool:
    """Return whether the observation carries an uncontradicted valid Queue ID.

    The expected (persisted) Queue ID, or an observed one that does not contradict
    it, proves the visitor holds a queue identity. An explicit mismatch never does.
    """

    if observation.identity_match is False:
        return False
    expected = _valid_queue_id(observation.expected_queue_id)
    observed = _valid_queue_id(observation.observed_queue_id)
    if expected is not None and observed is not None and expected != observed:
        return False
    return expected is not None or observed is not None


def _valid_queue_id(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def identity_only_status(current_status: QueueStatus | None = None) -> QueueStatus:
    """Lifecycle for a valid Queue ID whose observation exposes no usable state.

    A Queue ID proves a queue identity, so the base in-queue state ``PRE_QUEUE`` is
    used. A session that has already been explicitly observed at a later in-queue
    stage (for example ``ACTIVE_QUEUE``) keeps that stage: the earlier explicit
    signal is stronger evidence, and the transition rules never move it backwards.
    """

    if current_status is None:
        return QueueStatus.PRE_QUEUE
    current_status = QueueStatus.parse(current_status)
    if can_transition(current_status, QueueStatus.PRE_QUEUE):
        return QueueStatus.PRE_QUEUE
    return current_status


def evaluate_monitoring_observation(
    observation: MonitoringObservation,
    *,
    current_status: QueueStatus | None = None,
) -> QueueStatus:
    """Evaluate either source through the existing authoritative lifecycle rules.

    Every explicit signal (connection loss, expiry, admission, turn started, ready,
    serviced soon, paused, pre-queue, active queue, or sufficient progress) is
    evaluated first. Only when none applies and the observation carries a valid,
    uncontradicted Queue ID does the identity-only fallback replace ``CHECKING``.
    ``current_status`` is the persisted status the result will be written over.
    """

    status = evaluate_queue_status(observation.progress, observation.page)
    if status is QueueStatus.CHECKING and has_valid_queue_identity(observation):
        return identity_only_status(current_status)
    return status
