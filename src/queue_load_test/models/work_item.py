"""Small scheduling work item model for future phases."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class WorkItem:
    """A lightweight unit of work for a queue session."""

    session_id: str
    available_at: datetime
    priority: int = 0
