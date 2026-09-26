"""Persistence boundary for queue sessions."""

import builtins
from datetime import datetime
from typing import Protocol

from queue_load_test.models import QueueProgress, QueueSession, QueueStatus


class RepositoryError(RuntimeError):
    """Base error for session persistence failures."""


class SessionNotFoundError(RepositoryError):
    """Raised when a requested session does not exist."""


class QueueIdConflictError(RepositoryError):
    """Raised when a non-null Queue ID is already persisted."""


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
