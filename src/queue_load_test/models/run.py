"""Persisted operator run and dashboard-only view models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from queue_load_test.models.browser import BrowserBackendName
from queue_load_test.models.session import QueueStatus


class RunStatus(StrEnum):
    """Lifecycle of the single current operator run."""

    ACTIVE = "ACTIVE"


class BrowserRuntimeState(StrEnum):
    """Browser ownership, deliberately separate from Queue-it lifecycle status."""

    PARKED = "PARKED"
    CHECKING = "CHECKING"
    OPEN_IN_CHROME = "OPEN_IN_CHROME"


@dataclass(frozen=True, slots=True)
class RunConfig:
    run_id: str
    target_url: str = field(repr=False)
    requested_sessions: int
    created_at: datetime
    browser_backend: BrowserBackendName = BrowserBackendName.CHROME
    status: RunStatus = RunStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Safe, bounded dashboard projection; sensitive session fields are excluded."""

    session_id: str
    queue_id: str | None
    status: QueueStatus
    progress_percentage: float | None
    last_queue_update: datetime | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    runtime_state: BrowserRuntimeState


@dataclass(frozen=True, slots=True)
class SessionSummaryPage:
    items: tuple[SessionSummary, ...]
    total: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)
