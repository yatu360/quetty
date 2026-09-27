"""Persistence boundary for queue sessions."""

import builtins
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus


class RepositoryError(RuntimeError):
    """Base error for session persistence failures."""


class SessionNotFoundError(RepositoryError):
    """Raised when a requested session does not exist."""


class QueueIdConflictError(RepositoryError):
    """Raised when a non-null Queue ID is already persisted."""


class LeaseOwnershipError(RepositoryError):
    """Raised when stale leased work attempts to overwrite a newer owner."""


class ClaimedSessions(list[QueueSession]):
    """Claimed sessions plus how many of them were taken over from expired leases.

    It is still a ``list`` so existing callers are unaffected. A recovered lease
    means the previous owner stopped without releasing it (crash, kill, or a
    shutdown that could not reach the database).
    """

    recovered_expired_leases: int
    reclaimed_own_expired_leases: int

    def __init__(
        self,
        sessions: Iterable[QueueSession] = (),
        *,
        recovered_expired_leases: int = 0,
        reclaimed_own_expired_leases: int = 0,
    ) -> None:
        super().__init__(sessions)
        self.recovered_expired_leases = recovered_expired_leases
        self.reclaimed_own_expired_leases = reclaimed_own_expired_leases


PROGRESS_BUCKETS: tuple[str, ...] = (
    "unknown",
    "0-10",
    "10-25",
    "25-50",
    "50-75",
    "75-90",
    "90-100",
)
"""Fixed progress-percentage buckets; the set never grows with the population."""


@dataclass(frozen=True, slots=True)
class DueSessionSummary:
    """Low-cost aggregate view of currently claimable monitoring work."""

    count: int
    oldest_due_at: datetime | None

    def oldest_overdue_seconds(self, *, now: datetime) -> float:
        if self.oldest_due_at is None:
            return 0.0
        return max(0.0, (now - self.oldest_due_at).total_seconds())


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """Low-cost aggregate view of persisted recovery state."""

    generated_at: datetime
    total_persisted_sessions: int
    valid_queue_ids: int
    leased_sessions: int
    expired_leases: int
    sessions_due: int
    sessions_requiring_retry: int
    terminal_sessions: int
    status_counts: dict[QueueStatus, int]
    missing_state_files: int | None = None
    corrupt_state_files: int | None = None
    lost_queue_ids: int = 0
    """Distinct Queue IDs whose sessions later became FAILED (not counted as valid)."""

    @property
    def state_scan_performed(self) -> bool:
        return self.missing_state_files is not None and self.corrupt_state_files is not None


class SessionRepository(Protocol):
    """Async session operations shared by current and future database backends."""

    async def initialize(self) -> None: ...

    async def create(
        self,
        session: QueueSession,
        progress: QueueProgress | None = None,
    ) -> QueueSession: ...

    async def update(
        self,
        session: QueueSession,
        progress: QueueProgress | None = None,
    ) -> QueueSession: ...

    async def get(self, session_id: str) -> QueueSession | None: ...

    async def list(
        self, status: QueueStatus | None = None
    ) -> builtins.list[QueueSession]: ...

    async def count_successful_queue_ids(self) -> int: ...

    async def count_lost_queue_ids(self) -> int: ...

    async def progress_distribution(self) -> dict[str, int]: ...

    async def recovery_summary(self, *, now: datetime) -> RecoverySummary: ...

    async def save_progress(self, progress: QueueProgress) -> QueueProgress: ...

    async def get_progress(self, session_id: str) -> QueueProgress | None: ...

    async def count_due_sessions(self, *, now: datetime) -> int: ...

    async def due_session_summary(self, *, now: datetime) -> DueSessionSummary: ...

    async def claim_due_sessions(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> builtins.list[QueueSession]: ...

    async def release_lease(self, session_id: str, *, worker_id: str | None = None) -> bool: ...

    async def close(self) -> None: ...
