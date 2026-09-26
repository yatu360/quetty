"""Queue lifecycle transitions and deterministic state evaluation."""

from dataclasses import dataclass

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
        frozenset({QueueStatus.ACTIVE_QUEUE, QueueStatus.PAUSED}) | _INTERRUPT_STATES
    ),
    QueueStatus.ACTIVE_QUEUE: (
        frozenset({QueueStatus.SERVICED_SOON, QueueStatus.PAUSED}) | _INTERRUPT_STATES
    ),
    QueueStatus.SERVICED_SOON: (
        frozenset({QueueStatus.TURN_STARTED, QueueStatus.PAUSED}) | _INTERRUPT_STATES
    ),
    QueueStatus.TURN_STARTED: frozenset({QueueStatus.READY}) | _INTERRUPT_STATES,
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
    if page.pre_queue is True:
        return QueueStatus.PRE_QUEUE
    if page.active_queue is True:
        return QueueStatus.ACTIVE_QUEUE

    active_progress = (
        progress.queue_number is not None
        or progress.users_ahead is not None
        or progress.estimated_wait_text is not None
        or progress.expected_service_time is not None
    )
    if active_progress:
        return QueueStatus.ACTIVE_QUEUE
    return QueueStatus.CHECKING
