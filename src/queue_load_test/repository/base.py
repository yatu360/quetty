"""Persistence boundary for queue sessions."""

import builtins
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

    @property
    def state_scan_performed(self) -> bool:
        return self.missing_state_files is not None and self.corrupt_state_files is not None


class SessionRepository(Protocol):
    """Async session operations shared by current and future database backends."""

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

    async def recovery_summary(self, *, now: datetime) -> RecoverySummary: ...

    async def save_progress(self, progress: QueueProgress) -> QueueProgress: ...

    async def get_progress(self, session_id: str) -> QueueProgress | None: ...

    async def count_due_sessions(self, *, now: datetime) -> int: ...

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
