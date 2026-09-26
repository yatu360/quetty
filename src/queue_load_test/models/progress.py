"""Queue-it progress observed from browser-rendered pages."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class QueueProgress:
    """Layout-dependent queue progress.

    Every scraped Queue-it field is optional because layouts and pre-queue pages
    may omit or rename values.
    """

    session_id: str
    queue_number: str | None = None
    users_ahead: int | None = None
    progress_percentage: float | None = None
    estimated_wait_text: str | None = None
    expected_service_time: datetime | None = None
    last_updated_at: datetime | None = None
    queue_paused: bool | None = None
    first_in_line: bool | None = None
    serviced_soon: bool | None = None
    turn_started: bool | None = None
    connection_lost: bool | None = None

    def __post_init__(self) -> None:
        if self.progress_percentage is not None and not 0 <= self.progress_percentage <= 100:
            raise ValueError("progress_percentage must be between 0 and 100")
