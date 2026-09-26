"""SQLite implementation of the session repository boundary."""

import asyncio
import builtins
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from queue_load_test.models import QueueSession, QueueStatus, SessionMode, validate_transition
from queue_load_test.repository.base import (
    QueueIdConflictError,
    SessionNotFoundError,
)

_T = TypeVar("_T")

_SESSION_COLUMNS = """
    session_id, queue_id, transfer_url, mode, status, state_path,
    created_at, last_checked_at, next_check_at, attempt_count, last_error,
    worker_id, lease_until
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_sessions (
    session_id TEXT PRIMARY KEY,
    queue_id TEXT UNIQUE,
    transfer_url TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    state_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    next_check_at TEXT,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    last_error TEXT,
    worker_id TEXT,
    lease_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_sessions_due
    ON queue_sessions (next_check_at, lease_until, status);
"""


def _database_path(database: str | Path) -> str:
    value = str(database)
    if value.startswith("sqlite:///"):
        value = value.removeprefix("sqlite:///")
    if not value:
        raise ValueError("SQLite database path cannot be empty")
    return value


def _to_storage(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _from_storage(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _session_values(session: QueueSession) -> tuple[object, ...]:
    return (
        session.session_id,
        session.queue_id,
        session.transfer_url,
        session.mode.value,
        session.status.value,
        str(session.state_path),
        _to_storage(session.created_at),
        _to_storage(session.last_checked_at),
        _to_storage(session.next_check_at),
        session.attempt_count,
        session.last_error,
        session.worker_id,
        _to_storage(session.lease_until),
    )


def _row_to_session(row: sqlite3.Row) -> QueueSession:
    created_at = _from_storage(row["created_at"])
    if created_at is None:
        raise ValueError("Persisted session has no created_at timestamp")
    return QueueSession(
        session_id=row["session_id"],
        queue_id=row["queue_id"],
        transfer_url=row["transfer_url"],
        mode=SessionMode.parse(row["mode"]),
        status=QueueStatus.parse(row["status"]),
        state_path=Path(row["state_path"]),
        created_at=created_at,
        last_checked_at=_from_storage(row["last_checked_at"]),
        next_check_at=_from_storage(row["next_check_at"]),
        attempt_count=row["attempt_count"],
        last_error=row["last_error"],
        worker_id=row["worker_id"],
        lease_until=_from_storage(row["lease_until"]),
    )


class SQLiteSessionRepository:
    """Serialized async access to one SQLite connection.

    SQLite uses a short ``BEGIN IMMEDIATE`` transaction when claiming work. The
    repository boundary permits a future PostgreSQL implementation to use row
    locking and ``SKIP LOCKED`` without exposing those details to callers.
    """

    def __init__(self, database: str | Path) -> None:
        self._database = _database_path(database)
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create the database schema if needed."""

        await self._run(self._initialize)

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            if self._database != ":memory:":
                Path(self._database).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._database, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            connection.commit()
            if self._database != ":memory:":
                Path(self._database).chmod(0o600)
            self._connection = connection
        return self._connection

    def _initialize(self) -> None:
        self._connect()

    async def _run(self, operation: Callable[[], _T]) -> _T:
        async with self._lock:
            return await asyncio.to_thread(operation)

    async def create(self, session: QueueSession) -> QueueSession:
        def operation() -> QueueSession:
            connection = self._connect()
            try:
                connection.execute(
                    f"INSERT INTO queue_sessions ({_SESSION_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _session_values(session),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "queue_sessions.queue_id" in str(exc):
                    raise QueueIdConflictError("Queue ID already exists") from exc
                raise
            return session

        return await self._run(operation)

    async def update(self, session: QueueSession) -> QueueSession:
        def operation() -> QueueSession:
            connection = self._connect()
            existing = connection.execute(
                "SELECT status FROM queue_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if existing is None:
                raise SessionNotFoundError(f"Session {session.session_id!r} does not exist")
            validate_transition(QueueStatus.parse(existing["status"]), session.status)
            try:
                cursor = connection.execute(
                    """
                    UPDATE queue_sessions SET
                        queue_id = ?, transfer_url = ?, mode = ?, status = ?, state_path = ?,
                        created_at = ?, last_checked_at = ?, next_check_at = ?,
                        attempt_count = ?, last_error = ?, worker_id = ?, lease_until = ?
                    WHERE session_id = ?
                    """,
                    _session_values(session)[1:] + (session.session_id,),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "queue_sessions.queue_id" in str(exc):
                    raise QueueIdConflictError("Queue ID already exists") from exc
                raise
            if cursor.rowcount != 1:
                raise SessionNotFoundError(f"Session {session.session_id!r} does not exist")
            return session

        return await self._run(operation)

    async def get(self, session_id: str) -> QueueSession | None:
        def operation() -> QueueSession | None:
            row = self._connect().execute(
                f"SELECT {_SESSION_COLUMNS} FROM queue_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return _row_to_session(row) if row is not None else None

        return await self._run(operation)

    async def list(
        self, status: QueueStatus | None = None
    ) -> builtins.list[QueueSession]:
        def operation() -> builtins.list[QueueSession]:
            connection = self._connect()
            if status is None:
                rows = connection.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM queue_sessions ORDER BY created_at, session_id"
                ).fetchall()
            else:
                parsed_status = QueueStatus.parse(status)
                rows = connection.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM queue_sessions "
                    "WHERE status = ? ORDER BY created_at, session_id",
                    (parsed_status.value,),
                ).fetchall()
            return [_row_to_session(row) for row in rows]

        return await self._run(operation)

    async def count_successful_queue_ids(self) -> int:
        def operation() -> int:
            row = self._connect().execute(
                """
                SELECT COUNT(DISTINCT queue_id) AS total
                FROM queue_sessions
                WHERE queue_id IS NOT NULL AND status != ?
                """,
                (QueueStatus.FAILED.value,),
            ).fetchone()
            return int(row["total"])

        return await self._run(operation)

    async def claim_due_sessions(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> builtins.list[QueueSession]:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be blank")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        now_storage = _to_storage(now)
        lease_storage = _to_storage(lease_until)
        if now_storage is None or lease_storage is None:
            raise ValueError("now and lease_until are required")
        if lease_storage <= now_storage:
            raise ValueError("lease_until must be later than now")

        def operation() -> builtins.list[QueueSession]:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    f"""
                    SELECT {_SESSION_COLUMNS}
                    FROM queue_sessions
                    WHERE (next_check_at IS NULL OR next_check_at <= ?)
                      AND (lease_until IS NULL OR lease_until <= ?)
                      AND status NOT IN (?, ?, ?)
                    ORDER BY COALESCE(next_check_at, created_at), created_at, session_id
                    LIMIT ?
                    """,
                    (
                        now_storage,
                        now_storage,
                        QueueStatus.ADMITTED.value,
                        QueueStatus.EXPIRED.value,
                        QueueStatus.FAILED.value,
                        limit,
                    ),
                ).fetchall()
                session_ids = [row["session_id"] for row in rows]
                if session_ids:
                    placeholders = ", ".join("?" for _ in session_ids)
                    connection.execute(
                        f"UPDATE queue_sessions SET worker_id = ?, lease_until = ? "
                        f"WHERE session_id IN ({placeholders})",
                        (worker_id, lease_storage, *session_ids),
                    )
                    rows = connection.execute(
                        f"SELECT {_SESSION_COLUMNS} FROM queue_sessions "
                        f"WHERE session_id IN ({placeholders}) "
                        "ORDER BY COALESCE(next_check_at, created_at), created_at, session_id",
                        session_ids,
                    ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return [_row_to_session(row) for row in rows]

        return await self._run(operation)

    async def release_lease(self, session_id: str, *, worker_id: str | None = None) -> bool:
        def operation() -> bool:
            connection = self._connect()
            if worker_id is None:
                cursor = connection.execute(
                    "UPDATE queue_sessions SET worker_id = NULL, lease_until = NULL "
                    "WHERE session_id = ?",
                    (session_id,),
                )
            else:
                cursor = connection.execute(
                    "UPDATE queue_sessions SET worker_id = NULL, lease_until = NULL "
                    "WHERE session_id = ? AND worker_id = ?",
                    (session_id, worker_id),
                )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def close(self) -> None:
        def operation() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

        await self._run(operation)
