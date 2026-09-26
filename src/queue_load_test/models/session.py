"""Queue session identity and lifecycle state."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import uuid4


class ParseableStrEnum(StrEnum):
    """String enum with case-insensitive parsing."""

    @classmethod
    def parse(cls, value: str | Self) -> Self:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise TypeError(f"{cls.__name__} must be parsed from a string")

        normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return cls(normalized)
        except ValueError as exc:
            allowed = ", ".join(member.value for member in cls)
            raise ValueError(f"Unknown {cls.__name__}: {value!r}. Expected one of: {allowed}") from exc


class SessionMode(ParseableStrEnum):
    """How a session participates in the test."""

    HYBRID = "HYBRID"
    TRANSFER_ONLY = "TRANSFER_ONLY"


class QueueStatus(ParseableStrEnum):
    """Lifecycle status for a Queue-it browser session."""

    NEW = "NEW"
    CREATING = "CREATING"
    PRE_QUEUE = "PRE_QUEUE"
    ACTIVE_QUEUE = "ACTIVE_QUEUE"
    PARKED = "PARKED"
    CHECKING = "CHECKING"
    PAUSED = "PAUSED"
    SERVICED_SOON = "SERVICED_SOON"
    TURN_STARTED = "TURN_STARTED"
    READY = "READY"
    ADMITTED = "ADMITTED"
    CONNECTION_LOST = "CONNECTION_LOST"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(slots=True)
class QueueSession:
    """Queue-it session identity, storage, and worker ownership.

    Queue-it identity fields live here. Layout-dependent queue progress fields
    live in QueueProgress so code does not infer lifecycle from a queue id.
    """

    transfer_url: str = field(repr=False)
    mode: SessionMode
    state_path: Path = field(repr=False)
    queue_id: str | None = None
    session_id: str = field(default_factory=lambda: str(uuid4()))
    status: QueueStatus = QueueStatus.NEW
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    attempt_count: int = 0
    last_error: str | None = None
    worker_id: str | None = None
    lease_until: datetime | None = None

    def __post_init__(self) -> None:
        self.mode = SessionMode.parse(self.mode)
        self.status = QueueStatus.parse(self.status)
        self.state_path = Path(self.state_path)
        if self.queue_id is not None and not self.queue_id.strip():
            raise ValueError("queue_id cannot be blank")
        if self.attempt_count < 0:
            raise ValueError("attempt_count cannot be negative")
