"""Source-neutral Queue-it progress used by lifecycle and persistence."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class QueueExtractionDiagnostics:
    """Compact extraction evidence without retaining page HTML."""

    matched_selectors: dict[str, str] = field(default_factory=dict)
    field_errors: dict[str, str] = field(default_factory=dict)
    signals: tuple[str, ...] = ()


@dataclass(slots=True)
class QueueProgress:
    """Optional queue progress normalized from an observed source.

    Every field is optional because browser layouts and direct response schemas may
    omit values. Source adapters must not fabricate unavailable fields.
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
    pre_queue: bool | None = None
    active_queue: bool | None = None
    manual_update_warning: str | None = None
    diagnostics: QueueExtractionDiagnostics | None = None

    def __post_init__(self) -> None:
        if self.progress_percentage is not None and not 0 <= self.progress_percentage <= 100:
            raise ValueError("progress_percentage must be between 0 and 100")
