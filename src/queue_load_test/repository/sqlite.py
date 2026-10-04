"""SQLite implementation of the session repository boundary."""

import asyncio
import builtins
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from queue_load_test.models import (
    BrowserBackendName,
    BrowserRuntimeState,
    MonitoringStrategy,
    ProxyProvider,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    RunStatus,
    SessionMode,
    SessionSummary,
    SessionSummaryPage,
    normalize_session_name,
    validate_transition,
)
from queue_load_test.repository.base import (
    OPERATOR_WORKER_PREFIX,
    PROGRESS_BUCKETS,
    ActiveRunExistsError,
    ClaimedSessions,
    DirectMonitorStatus,
    DueSessionSummary,
    LeaseOwnershipError,
    ManualSessionBusyError,
    ManualSessionCapacityError,
    OwnershipRecovery,
    ProxyIpRecord,
    ProxySessionAssignmentError,
    ProxySessionIdConflictError,
    QueueIdConflictError,
    RecoverySummary,
    SessionNotFoundError,
    UnownedSessionsError,
)
from queue_load_test.utils.asyncio_tools import run_to_completion

_T = TypeVar("_T")

# Proxy-exit metadata is read with every row but written only by record_proxy_ip.
_PROXY_IP_COLUMNS = "proxy_ip, proxy_ip_checked_at, proxy_ip_changed_count, proxy_ip_changed_at"
_SESSION_COLUMNS = """
    session_id, queue_id, transfer_url, mode, browser_backend, proxy_session_id,
    status, state_path,
    created_at, last_checked_at, last_queue_update, last_progress_change_at,
    next_check_at, attempt_count, last_error, worker_id, lease_until,
    manual_owner_id, manual_lease_until
"""
_SESSION_SELECT_COLUMNS = _SESSION_COLUMNS.rstrip() + ",\n    " + _PROXY_IP_COLUMNS


_NON_MONITORABLE_STATUSES_SQL = "'ADMITTED', 'EXPIRED', 'FAILED', 'NEW', 'CREATING'"
_DUE_TIME_SQL = "COALESCE(next_check_at, created_at)"
_DUE_FILTER_SQL = f"""
    {_DUE_TIME_SQL} <= ?
    AND (lease_until IS NULL OR lease_until <= ?)
    AND (manual_owner_id IS NULL OR manual_lease_until <= ?)
    AND status NOT IN ({_NON_MONITORABLE_STATUSES_SQL})
"""
_DUE_ORDER_SQL = f"{_DUE_TIME_SQL}, created_at, session_id"
_DUE_INDEX_NAME = "idx_queue_sessions_due"
_DUE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS {_DUE_INDEX_NAME}
    ON queue_sessions ({_DUE_ORDER_SQL})
    WHERE status NOT IN ({_NON_MONITORABLE_STATUSES_SQL})
"""
_DASHBOARD_ORDER_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_queue_sessions_dashboard_order
    ON queue_sessions (created_at, session_id)
"""
"""Lets the dashboard page walk rows in display order instead of sorting the population."""
_MANUAL_LEASE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_queue_sessions_manual_lease
    ON queue_sessions (manual_lease_until)
    WHERE manual_owner_id IS NOT NULL
"""
_PROXY_SESSION_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_sessions_proxy_session_id
    ON queue_sessions (proxy_session_id)
    WHERE proxy_session_id IS NOT NULL
"""
_PROXY_SESSION_IMMUTABILITY_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_queue_sessions_proxy_session_id_immutable
BEFORE UPDATE OF proxy_session_id ON queue_sessions
WHEN NOT (OLD.proxy_session_id IS NEW.proxy_session_id)
BEGIN
    SELECT RAISE(ABORT, 'proxy_session_id is immutable');
END
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_config (
    run_id TEXT PRIMARY KEY,
    target_url TEXT NOT NULL,
    requested_sessions INTEGER NOT NULL CHECK (requested_sessions > 0),
    browser_backend TEXT NOT NULL DEFAULT 'chrome',
    browser_build TEXT,
    monitoring_strategy TEXT NOT NULL DEFAULT 'headed_window',
    proxy_provider TEXT NOT NULL DEFAULT 'none',
    proxy_country TEXT,
    proxy_lifetime TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    current_run INTEGER NOT NULL UNIQUE CHECK (current_run = 1)
);
CREATE TABLE IF NOT EXISTS runtime_control (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    monitoring_paused INTEGER NOT NULL CHECK (monitoring_paused IN (0, 1)),
    operator_population_adjustment INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO runtime_control (singleton, monitoring_paused) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS queue_sessions (
    session_id TEXT PRIMARY KEY,
    queue_id TEXT UNIQUE,
    transfer_url TEXT NOT NULL,
    mode TEXT NOT NULL,
    browser_backend TEXT NOT NULL DEFAULT 'chrome',
    proxy_session_id TEXT,
    proxy_ip TEXT,
    proxy_ip_checked_at TEXT,
    proxy_ip_changed_count INTEGER NOT NULL DEFAULT 0,
    proxy_ip_changed_at TEXT,
    display_name TEXT,
    status TEXT NOT NULL,
    state_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_queue_update TEXT,
    last_progress_change_at TEXT,
    next_check_at TEXT,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    last_error TEXT,
    worker_id TEXT,
    lease_until TEXT,
    manual_owner_id TEXT,
    manual_lease_until TEXT
);
CREATE TABLE IF NOT EXISTS queue_progress (
    session_id TEXT PRIMARY KEY REFERENCES queue_sessions(session_id) ON DELETE CASCADE,
    queue_number TEXT,
    users_ahead INTEGER,
    progress_percentage REAL,
    estimated_wait_text TEXT,
    expected_service_time TEXT,
    last_updated_at TEXT,
    queue_paused INTEGER,
    first_in_line INTEGER,
    serviced_soon INTEGER,
    turn_started INTEGER,
    connection_lost INTEGER,
    pre_queue INTEGER,
    active_queue INTEGER,
    manual_update_warning TEXT
);
CREATE TABLE IF NOT EXISTS direct_monitor_status (
    session_id TEXT PRIMARY KEY REFERENCES queue_sessions(session_id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    last_reason TEXT,
    recipe_reference TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_success_at TEXT,
    last_failure_at TEXT,
    updated_at TEXT NOT NULL
);
"""


def _is_connection_interruption(exc: sqlite3.Error) -> bool:
    """Classify errors after which the cached connection should not be reused.

    Constraint violations and lock contention leave the connection healthy.
    A closed handle, I/O failure, or unopenable file does not.
    """

    if isinstance(exc, sqlite3.IntegrityError):
        return False
    if isinstance(exc, sqlite3.ProgrammingError):
        return True
    message = str(exc).casefold()
    if "locked" in message or "busy" in message:
        return False
    return isinstance(exc, sqlite3.OperationalError | sqlite3.DatabaseError)


def _progress_bucket_sql() -> str:
    edges = ((10, "0-10"), (25, "10-25"), (50, "25-50"), (75, "50-75"), (90, "75-90"))
    cases = " ".join(f"WHEN p.progress_percentage < {edge} THEN '{label}'" for edge, label in edges)
    return f"CASE WHEN p.progress_percentage IS NULL THEN 'unknown' {cases} ELSE '90-100' END"


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


def _required_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _session_values(session: QueueSession) -> tuple[object, ...]:
    return (
        session.session_id,
        session.queue_id,
        session.transfer_url,
        session.mode.value,
        session.browser_backend.value,
        session.proxy_session_id,
        session.status.value,
        str(session.state_path),
        _to_storage(session.created_at),
        _to_storage(session.last_checked_at),
        _to_storage(session.last_queue_update),
        _to_storage(session.last_progress_change_at),
        _to_storage(session.next_check_at),
        session.attempt_count,
        session.last_error,
        session.worker_id,
        _to_storage(session.lease_until),
        session.manual_owner_id,
        _to_storage(session.manual_lease_until),
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
        browser_backend=BrowserBackendName.parse(row["browser_backend"]),
        proxy_session_id=row["proxy_session_id"],
        status=QueueStatus.parse(row["status"]),
        state_path=Path(row["state_path"]),
        created_at=created_at,
        last_checked_at=_from_storage(row["last_checked_at"]),
        last_queue_update=_from_storage(row["last_queue_update"]),
        last_progress_change_at=_from_storage(row["last_progress_change_at"]),
        next_check_at=_from_storage(row["next_check_at"]),
        attempt_count=row["attempt_count"],
        last_error=row["last_error"],
        worker_id=row["worker_id"],
        lease_until=_from_storage(row["lease_until"]),
        manual_owner_id=row["manual_owner_id"],
        manual_lease_until=_from_storage(row["manual_lease_until"]),
        proxy_ip=row["proxy_ip"],
        proxy_ip_checked_at=_from_storage(row["proxy_ip_checked_at"]),
        proxy_ip_changed_count=int(row["proxy_ip_changed_count"] or 0),
        proxy_ip_changed_at=_from_storage(row["proxy_ip_changed_at"]),
    )


def _to_boolean_storage(value: bool | None) -> int | None:
    return int(value) if value is not None else None


def _from_boolean_storage(value: int | None) -> bool | None:
    return bool(value) if value is not None else None


def _progress_values(progress: QueueProgress) -> tuple[object, ...]:
    return (
        progress.session_id,
        progress.queue_number,
        progress.users_ahead,
        progress.progress_percentage,
        progress.estimated_wait_text,
        _to_storage(progress.expected_service_time),
        _to_storage(progress.last_updated_at),
        _to_boolean_storage(progress.queue_paused),
        _to_boolean_storage(progress.first_in_line),
        _to_boolean_storage(progress.serviced_soon),
        _to_boolean_storage(progress.turn_started),
        _to_boolean_storage(progress.connection_lost),
        _to_boolean_storage(progress.pre_queue),
        _to_boolean_storage(progress.active_queue),
        progress.manual_update_warning,
    )


def _row_to_progress(row: sqlite3.Row) -> QueueProgress:
    return QueueProgress(
        session_id=row["session_id"],
        queue_number=row["queue_number"],
        users_ahead=row["users_ahead"],
        progress_percentage=row["progress_percentage"],
        estimated_wait_text=row["estimated_wait_text"],
        expected_service_time=_from_storage(row["expected_service_time"]),
        last_updated_at=_from_storage(row["last_updated_at"]),
        queue_paused=_from_boolean_storage(row["queue_paused"]),
        first_in_line=_from_boolean_storage(row["first_in_line"]),
        serviced_soon=_from_boolean_storage(row["serviced_soon"]),
        turn_started=_from_boolean_storage(row["turn_started"]),
        connection_lost=_from_boolean_storage(row["connection_lost"]),
        pre_queue=_from_boolean_storage(row["pre_queue"]),
        active_queue=_from_boolean_storage(row["active_queue"]),
        manual_update_warning=row["manual_update_warning"],
    )


def _row_runtime_state(row: sqlite3.Row, *, now: datetime) -> BrowserRuntimeState:
    """Derive browser ownership from live leases only.

    ``QueueStatus.CHECKING`` is a lifecycle observation (insufficient page evidence),
    not ownership, and an expired lease belongs to a stopped owner that any fenced
    claimer may take over. Neither may make a row look busy forever.
    """

    manual_lease_until = _from_storage(row["manual_lease_until"])
    if row["manual_owner_id"] is not None and (
        manual_lease_until is not None and manual_lease_until > now
    ):
        return BrowserRuntimeState.OPEN_IN_CHROME
    lease_until = _from_storage(row["lease_until"])
    if row["worker_id"] is not None and lease_until is not None and lease_until > now:
        return BrowserRuntimeState.CHECKING
    return BrowserRuntimeState.PARKED


def _lease_is_live(row: sqlite3.Row, *, now: datetime) -> bool:
    lease_until = _from_storage(row["lease_until"])
    return row["worker_id"] is not None and (lease_until is None or lease_until > now)


def _busy_lease_message(row: sqlite3.Row) -> str:
    if str(row["worker_id"]).startswith(OPERATOR_WORKER_PREFIX):
        return "Another operator action is already running for this session"
    return "Session is currently being checked"


def _save_progress(connection: sqlite3.Connection, progress: QueueProgress) -> None:
    connection.execute(
        """
        INSERT INTO queue_progress (
            session_id, queue_number, users_ahead, progress_percentage,
            estimated_wait_text, expected_service_time, last_updated_at,
            queue_paused, first_in_line, serviced_soon, turn_started,
            connection_lost, pre_queue, active_queue, manual_update_warning
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            queue_number = excluded.queue_number,
            users_ahead = excluded.users_ahead,
            progress_percentage = excluded.progress_percentage,
            estimated_wait_text = excluded.estimated_wait_text,
            expected_service_time = excluded.expected_service_time,
            last_updated_at = excluded.last_updated_at,
            queue_paused = excluded.queue_paused,
            first_in_line = excluded.first_in_line,
            serviced_soon = excluded.serviced_soon,
            turn_started = excluded.turn_started,
            connection_lost = excluded.connection_lost,
            pre_queue = excluded.pre_queue,
            active_queue = excluded.active_queue,
            manual_update_warning = excluded.manual_update_warning
        """,
        _progress_values(progress),
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
        self._reconnects = 0

    async def initialize(self) -> None:
        """Create the database schema if needed."""

        await self._run(self._initialize)

    async def get_active_run(self) -> RunConfig | None:
        """Return the immutable current run configuration, if setup has completed."""

        def operation() -> RunConfig | None:
            row = self._connect().execute(
                """
                SELECT run_id, target_url, requested_sessions, browser_backend,
                       browser_build, monitoring_strategy, proxy_provider,
                       proxy_country, proxy_lifetime, created_at, status
                FROM run_config WHERE current_run = 1
                """
            ).fetchone()
            if row is None:
                return None
            created_at = _from_storage(row["created_at"])
            if created_at is None:
                raise ValueError("Persisted run has no created_at timestamp")
            return RunConfig(
                run_id=str(row["run_id"]),
                target_url=str(row["target_url"]),
                requested_sessions=int(row["requested_sessions"]),
                browser_backend=BrowserBackendName.parse(row["browser_backend"]),
                monitoring_strategy=MonitoringStrategy.parse(row["monitoring_strategy"]),
                browser_build=(
                    str(row["browser_build"]) if row["browser_build"] is not None else None
                ),
                proxy_provider=ProxyProvider.parse(row["proxy_provider"]),
                proxy_country=row["proxy_country"],
                proxy_lifetime=row["proxy_lifetime"],
                created_at=created_at,
                status=RunStatus(str(row["status"])),
            )

        return await self._run(operation)

    async def create_run(self, run: RunConfig) -> RunConfig:
        """Persist setup once, refusing unsafe association with legacy sessions."""

        def operation() -> RunConfig:
            connection = self._connect()
            try:
                session_count = int(
                    connection.execute("SELECT COUNT(*) FROM queue_sessions").fetchone()[0]
                )
                if session_count:
                    raise UnownedSessionsError(
                        "Existing sessions have no persisted target; use a dedicated empty database"
                    )
                connection.execute(
                    """
                    INSERT INTO run_config (
                        run_id, target_url, requested_sessions, browser_backend,
                        browser_build, monitoring_strategy, proxy_provider,
                        proxy_country, proxy_lifetime, created_at, status, current_run
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        run.run_id,
                        run.target_url,
                        run.requested_sessions,
                        run.browser_backend.value,
                        run.browser_build,
                        run.monitoring_strategy.value,
                        run.proxy_provider.value,
                        run.proxy_country,
                        run.proxy_lifetime,
                        _to_storage(run.created_at),
                        run.status.value,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ActiveRunExistsError("An active run already exists") from exc
            except Exception:
                connection.rollback()
                raise
            return run

        return await self._run(operation)

    async def reset_all(self) -> None:
        """Delete the run, every session and its progress, and reset runtime controls.

        Only rows are removed; the schema stays, so setup can create a new run.
        Callers must stop every browser owner first.
        """

        def operation() -> None:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM queue_progress")
                connection.execute("DELETE FROM direct_monitor_status")
                connection.execute("DELETE FROM queue_sessions")
                connection.execute("DELETE FROM run_config")
                connection.execute(
                    "UPDATE runtime_control SET monitoring_paused = 0, "
                    "operator_population_adjustment = 0 WHERE singleton = 1"
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        await self._run(operation)

    async def is_monitoring_paused(self) -> bool:
        """Return the persisted global automatic-monitoring control state."""

        def operation() -> bool:
            row = self._connect().execute(
                "SELECT monitoring_paused FROM runtime_control WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ValueError("Persisted runtime control row is missing")
            return bool(row["monitoring_paused"])

        return await self._run(operation)

    async def set_monitoring_paused(self, paused: bool) -> bool:
        """Persist pause/resume in one row without modifying any queue session."""

        def operation() -> bool:
            connection = self._connect()
            connection.execute(
                "UPDATE runtime_control SET monitoring_paused = ? WHERE singleton = 1",
                (int(paused),),
            )
            connection.commit()
            return paused

        return await self._run(operation)

    async def get_operator_population_adjustment(self) -> int:
        def operation() -> int:
            row = self._connect().execute(
                "SELECT operator_population_adjustment FROM runtime_control WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ValueError("Persisted runtime control row is missing")
            return int(row["operator_population_adjustment"])

        return await self._run(operation)

    async def adjust_operator_population(self, delta: int) -> int:
        def operation() -> int:
            connection = self._connect()
            connection.execute(
                "UPDATE runtime_control SET operator_population_adjustment = "
                "operator_population_adjustment + ? WHERE singleton = 1",
                (delta,),
            )
            row = connection.execute(
                "SELECT operator_population_adjustment FROM runtime_control WHERE singleton = 1"
            ).fetchone()
            connection.commit()
            if row is None:
                raise ValueError("Persisted runtime control row is missing")
            return int(row["operator_population_adjustment"])

        return await self._run(operation)

    async def acquire_operator_lease(
        self,
        session_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> QueueSession:
        """Atomically reserve one session for a bounded operator action."""

        if not worker_id.strip():
            raise ValueError("worker_id cannot be blank")
        now_storage = _to_storage(now)
        lease_storage = _to_storage(lease_until)
        if now_storage is None or lease_storage is None or lease_storage <= now_storage:
            raise ValueError("operator lease must end after now")

        def operation() -> QueueSession:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFoundError(f"Session {session_id!r} does not exist")
                manual_until = _from_storage(row["manual_lease_until"])
                if row["manual_owner_id"] is not None and (
                    manual_until is None or manual_until > now
                ):
                    raise ManualSessionBusyError("Close the browser session before continuing")
                # An expired lease belongs to an owner that stopped renewing it; every
                # later write from that owner is fenced by worker_id, exactly as for
                # the scheduler's own expired-lease reclaim.
                if _lease_is_live(row, now=now):
                    raise ManualSessionBusyError(_busy_lease_message(row))
                cursor = connection.execute(
                    "UPDATE queue_sessions SET worker_id = ?, lease_until = ?, "
                    "manual_owner_id = NULL, manual_lease_until = NULL "
                    "WHERE session_id = ? AND (worker_id IS NULL OR lease_until <= ?) "
                    "AND (manual_owner_id IS NULL OR manual_lease_until <= ?)",
                    (worker_id, lease_storage, session_id, now_storage, now_storage),
                )
                if cursor.rowcount != 1:
                    raise ManualSessionBusyError("Session browser ownership changed")
                claimed = connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return _row_to_session(claimed)

        return await self._run(operation)

    async def record_direct_monitor_status(self, status: DirectMonitorStatus) -> bool:
        """Upsert non-secret Direct Monitoring metadata; ignored once a session is gone.

        Only a capability value, a sanitized reason enum value, a one-way recipe
        reference, counters, and timestamps are stored. Recipes, URLs, headers,
        bodies, and cookies stay in the protected direct-monitor store.
        """

        def operation() -> bool:
            connection = self._connect()
            cursor = connection.execute(
                """
                INSERT INTO direct_monitor_status (
                    session_id, capability, last_reason, recipe_reference,
                    consecutive_failures, last_success_at, last_failure_at, updated_at
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM queue_sessions WHERE session_id = ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    capability = excluded.capability,
                    last_reason = excluded.last_reason,
                    recipe_reference = excluded.recipe_reference,
                    consecutive_failures = excluded.consecutive_failures,
                    last_success_at = COALESCE(
                        excluded.last_success_at, direct_monitor_status.last_success_at
                    ),
                    last_failure_at = COALESCE(
                        excluded.last_failure_at, direct_monitor_status.last_failure_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    status.session_id,
                    status.capability,
                    status.last_reason,
                    status.recipe_reference,
                    status.consecutive_failures,
                    _to_storage(status.last_success_at),
                    _to_storage(status.last_failure_at),
                    _to_storage(status.updated_at),
                    status.session_id,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def get_direct_monitor_status(self, session_id: str) -> DirectMonitorStatus | None:
        def operation() -> DirectMonitorStatus | None:
            row = (
                self._connect()
                .execute(
                    "SELECT * FROM direct_monitor_status WHERE session_id = ?", (session_id,)
                )
                .fetchone()
            )
            if row is None:
                return None
            return DirectMonitorStatus(
                session_id=str(row["session_id"]),
                capability=str(row["capability"]),
                last_reason=row["last_reason"],
                recipe_reference=row["recipe_reference"],
                consecutive_failures=int(row["consecutive_failures"]),
                last_success_at=_from_storage(row["last_success_at"]),
                last_failure_at=_from_storage(row["last_failure_at"]),
                updated_at=_required_datetime(row["updated_at"]),
            )

        return await self._run(operation)

    async def direct_capability_counts(self) -> dict[str, int]:
        """Aggregate capability for monitorable identified sessions (no per-session data).

        A session without a metadata row has not been observed yet and counts as
        ``DISCOVERY_REQUIRED``.
        """

        def operation() -> dict[str, int]:
            rows = (
                self._connect()
                .execute(
                    f"""
                    SELECT COALESCE(d.capability, 'DISCOVERY_REQUIRED') AS capability,
                           COUNT(*) AS sessions
                    FROM queue_sessions AS s
                    LEFT JOIN direct_monitor_status AS d ON d.session_id = s.session_id
                    WHERE s.queue_id IS NOT NULL
                      AND s.status NOT IN ({_NON_MONITORABLE_STATUSES_SQL})
                    GROUP BY 1
                    """
                )
                .fetchall()
            )
            return {str(row["capability"]): int(row["sessions"]) for row in rows}

        return await self._run(operation)

    async def delete_owned_session(
        self,
        session_id: str,
        *,
        worker_id: str,
        population_delta: int = 0,
    ) -> bool:
        def operation() -> bool:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM queue_sessions WHERE session_id = ? AND worker_id = ?",
                    (session_id, worker_id),
                )
                if cursor.rowcount == 1 and population_delta:
                    connection.execute(
                        "UPDATE runtime_control SET operator_population_adjustment = "
                        "operator_population_adjustment + ? WHERE singleton = 1",
                        (population_delta,),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return cursor.rowcount == 1

        return await self._run(operation)

    async def renew_operator_lease(
        self,
        session_id: str,
        *,
        worker_id: str,
        lease_until: datetime,
    ) -> bool:
        lease_storage = _to_storage(lease_until)
        if lease_storage is None:
            raise ValueError("lease_until is required")

        def operation() -> bool:
            connection = self._connect()
            cursor = connection.execute(
                "UPDATE queue_sessions SET lease_until = ? "
                "WHERE session_id = ? AND worker_id = ?",
                (lease_storage, session_id, worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def delete_unowned_session(self, session_id: str) -> bool:
        """Remove a known failed action-created row without overriding ownership."""

        def operation() -> bool:
            connection = self._connect()
            cursor = connection.execute(
                "DELETE FROM queue_sessions WHERE session_id = ? "
                "AND worker_id IS NULL AND manual_owner_id IS NULL",
                (session_id,),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def discard_orphaned_reservations(self) -> tuple[str, ...]:
        """Delete creation reservations whose creator died before completing.

        An IPRoyal creator persists a CREATING row (holding the new sticky-session ID)
        before its first navigation. If the process stops mid-creation, the row has no
        Queue ID and no owner and nothing will ever finish it. Only such rows match:
        manual adoption sets the Queue ID before CREATING, and in-flight work holds a
        lease. Call this only at startup, before creation begins. The persisted
        population target is untouched, so the normal deficit refill re-creates the
        visitor with a fresh, never-used proxy session ID.
        """

        def operation() -> tuple[str, ...]:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT session_id FROM queue_sessions
                    WHERE status = 'CREATING' AND queue_id IS NULL
                      AND worker_id IS NULL AND manual_owner_id IS NULL
                    """
                ).fetchall()
                session_ids = tuple(str(row["session_id"]) for row in rows)
                connection.executemany(
                    "DELETE FROM queue_sessions WHERE session_id = ? AND status = 'CREATING' "
                    "AND queue_id IS NULL AND worker_id IS NULL AND manual_owner_id IS NULL",
                    [(session_id,) for session_id in session_ids],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return session_ids

        return await self._run(operation)

    async def acquire_manual_ownership(
        self,
        session_id: str,
        *,
        owner_id: str,
        now: datetime,
        lease_until: datetime,
        capacity: int,
    ) -> QueueSession:
        """Atomically exclude automatic work and reserve bounded manual ownership."""

        if not owner_id.strip():
            raise ValueError("owner_id cannot be blank")
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        now_storage = _to_storage(now)
        lease_storage = _to_storage(lease_until)
        if now_storage is None or lease_storage is None or lease_storage <= now_storage:
            raise ValueError("manual lease must end after now")

        def operation() -> QueueSession:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE queue_sessions
                    SET manual_owner_id = NULL, manual_lease_until = NULL
                    WHERE manual_owner_id IS NOT NULL AND manual_lease_until <= ?
                    """,
                    (now_storage,),
                )
                row = connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFoundError(f"Session {session_id!r} does not exist")
                if row["manual_owner_id"] is not None:
                    raise ManualSessionBusyError("Session is already open in a browser")
                # A live automatic/operator lease excludes headed ownership. An expired
                # lease is abandoned (its owner stopped renewing it, or crashed) and
                # is taken over with the same fencing the scheduler uses; otherwise
                # a paused or non-monitorable row could stay unopenable forever.
                if _lease_is_live(row, now=now):
                    raise ManualSessionBusyError(_busy_lease_message(row))
                active = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM queue_sessions
                        WHERE manual_owner_id IS NOT NULL AND manual_lease_until > ?
                        """,
                        (now_storage,),
                    ).fetchone()[0]
                )
                if active >= capacity:
                    raise ManualSessionCapacityError("Browser capacity currently unavailable")
                cursor = connection.execute(
                    """
                    UPDATE queue_sessions
                    SET manual_owner_id = ?, manual_lease_until = ?,
                        worker_id = NULL, lease_until = NULL
                    WHERE session_id = ? AND manual_owner_id IS NULL
                      AND (worker_id IS NULL OR lease_until <= ?)
                    """,
                    (owner_id, lease_storage, session_id, now_storage),
                )
                if cursor.rowcount != 1:
                    raise ManualSessionBusyError("Session browser ownership changed")
                claimed = connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return _row_to_session(claimed)

        return await self._run(operation)

    async def renew_manual_ownership(
        self,
        session_id: str,
        *,
        owner_id: str,
        lease_until: datetime,
    ) -> bool:
        lease_storage = _to_storage(lease_until)
        if lease_storage is None:
            raise ValueError("lease_until is required")

        def operation() -> bool:
            connection = self._connect()
            cursor = connection.execute(
                """
                UPDATE queue_sessions SET manual_lease_until = ?
                WHERE session_id = ? AND manual_owner_id = ?
                """,
                (lease_storage, session_id, owner_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def release_manual_ownership(self, session_id: str, *, owner_id: str) -> bool:
        def operation() -> bool:
            connection = self._connect()
            cursor = connection.execute(
                """
                UPDATE queue_sessions
                SET manual_owner_id = NULL, manual_lease_until = NULL
                WHERE session_id = ? AND manual_owner_id = ?
                """,
                (session_id, owner_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def recover_stale_manual_ownership(self, *, now: datetime) -> int:
        now_storage = _to_storage(now)
        if now_storage is None:
            raise ValueError("now is required")

        def operation() -> int:
            connection = self._connect()
            cursor = connection.execute(
                """
                UPDATE queue_sessions
                SET manual_owner_id = NULL, manual_lease_until = NULL
                WHERE manual_owner_id IS NOT NULL AND manual_lease_until <= ?
                """,
                (now_storage,),
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def recover_startup_ownership(
        self,
        *,
        now: datetime,
        exclusive: bool,
    ) -> OwnershipRecovery:
        """Clear browser ownership left by a stopped process in one transaction.

        With ``exclusive`` the caller holds the single-instance lock for this
        database, so no other live process can own a headed Chrome window or a
        lease: every persisted owner belongs to a dead process and is cleared even
        if its lease has not expired yet. Without it only expired ownership is
        cleared. Queue IDs, lifecycle status, progress, and schedules are untouched.
        """

        now_storage = _to_storage(now)
        if now_storage is None:
            raise ValueError("now is required")
        manual_filter = "manual_owner_id IS NOT NULL"
        lease_filter = "worker_id IS NOT NULL"
        parameters: tuple[str, ...] = ()
        if not exclusive:
            manual_filter += " AND manual_lease_until <= ?"
            lease_filter += " AND lease_until <= ?"
            parameters = (now_storage,)

        def operation() -> OwnershipRecovery:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                manual = connection.execute(
                    "UPDATE queue_sessions SET manual_owner_id = NULL, "
                    f"manual_lease_until = NULL WHERE {manual_filter}",
                    parameters,
                ).rowcount
                leases = connection.execute(
                    "UPDATE queue_sessions SET worker_id = NULL, lease_until = NULL "
                    f"WHERE {lease_filter}",
                    parameters,
                ).rowcount
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return OwnershipRecovery(manual_released=manual, leases_released=leases)

        return await self._run(operation)

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            if self._database != ":memory:":
                Path(self._database).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._database, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            self._migrate_session_columns(connection)
            self._migrate_run_columns(connection)
            self._migrate_runtime_control_columns(connection)
            connection.execute(_PROXY_SESSION_INDEX_SQL)
            connection.execute(_PROXY_SESSION_IMMUTABILITY_SQL)
            self._ensure_due_index(connection)
            connection.execute(_MANUAL_LEASE_INDEX_SQL)
            connection.execute(_DASHBOARD_ORDER_INDEX_SQL)
            connection.commit()
            if self._database != ":memory:":
                Path(self._database).chmod(0o600)
            self._connection = connection
        return self._connection

    @staticmethod
    def _migrate_session_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(queue_sessions)").fetchall()
        }
        for name in (
            "last_queue_update",
            "last_progress_change_at",
            "manual_owner_id",
            "manual_lease_until",
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE queue_sessions ADD COLUMN {name} TEXT")
        if "browser_backend" not in columns:
            connection.execute(
                "ALTER TABLE queue_sessions ADD COLUMN "
                "browser_backend TEXT NOT NULL DEFAULT 'chrome'"
            )
        if "proxy_session_id" not in columns:
            connection.execute("ALTER TABLE queue_sessions ADD COLUMN proxy_session_id TEXT")
        for column, definition in (
            ("proxy_ip", "TEXT"),
            ("proxy_ip_checked_at", "TEXT"),
            ("proxy_ip_changed_count", "INTEGER NOT NULL DEFAULT 0"),
            ("proxy_ip_changed_at", "TEXT"),
            ("display_name", "TEXT"),
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE queue_sessions ADD COLUMN {column} {definition}"
                )

    @staticmethod
    def _migrate_run_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(run_config)").fetchall()
        }
        if "browser_backend" not in columns:
            connection.execute(
                "ALTER TABLE run_config ADD COLUMN "
                "browser_backend TEXT NOT NULL DEFAULT 'chrome'"
            )
        if "browser_build" not in columns:
            # Unknown for runs created before build provenance was recorded.
            connection.execute("ALTER TABLE run_config ADD COLUMN browser_build TEXT")
        if "monitoring_strategy" not in columns:
            # Every pre-Phase-8 run used the browser restore + live DOM path.
            connection.execute(
                "ALTER TABLE run_config ADD COLUMN "
                "monitoring_strategy TEXT NOT NULL DEFAULT 'headed_window'"
            )
        if "proxy_provider" not in columns:
            connection.execute(
                "ALTER TABLE run_config ADD COLUMN "
                "proxy_provider TEXT NOT NULL DEFAULT 'none'"
            )
        if "proxy_country" not in columns:
            connection.execute("ALTER TABLE run_config ADD COLUMN proxy_country TEXT")
        if "proxy_lifetime" not in columns:
            connection.execute("ALTER TABLE run_config ADD COLUMN proxy_lifetime TEXT")

    @staticmethod
    def _migrate_runtime_control_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(runtime_control)").fetchall()
        }
        if "operator_population_adjustment" not in columns:
            connection.execute(
                "ALTER TABLE runtime_control ADD COLUMN "
                "operator_population_adjustment INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _ensure_due_index(connection: sqlite3.Connection) -> None:
        """Replace the Phase 2 index when it does not match the due query ordering."""

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_DUE_INDEX_NAME,),
        ).fetchone()
        existing_sql = str(row["sql"]) if row is not None else ""
        if existing_sql and "COALESCE(next_check_at, created_at)" not in existing_sql:
            connection.execute(f"DROP INDEX {_DUE_INDEX_NAME}")
        connection.execute(_DUE_INDEX_SQL)

    def _initialize(self) -> None:
        self._connect()

    @property
    def reconnects(self) -> int:
        """Connections discarded after an interruption and reopened on demand."""

        return self._reconnects

    async def _run(self, operation: Callable[[], _T]) -> _T:
        async with self._lock:
            try:
                # Holding the lock until the thread finishes keeps the shared
                # connection serialized even during a forced (cancelling) shutdown.
                return await run_to_completion(asyncio.ensure_future(asyncio.to_thread(operation)))
            except sqlite3.Error as exc:
                if _is_connection_interruption(exc):
                    await asyncio.to_thread(self._discard_connection)
                raise

    def _discard_connection(self) -> None:
        """Drop a broken connection so the next operation reconnects.

        Nothing is committed here: SQLite rolls back an open transaction when
        its connection closes, so partially applied claims cannot persist.
        """

        connection = self._connection
        self._connection = None
        if connection is None:
            return
        self._reconnects += 1
        try:
            connection.close()
        except sqlite3.Error:
            pass

    async def create(
        self,
        session: QueueSession,
        progress: QueueProgress | None = None,
    ) -> QueueSession:
        if progress is not None and progress.session_id != session.session_id:
            raise ValueError("Progress session_id must match the session")

        def operation() -> QueueSession:
            connection = self._connect()
            try:
                connection.execute(
                    f"INSERT INTO queue_sessions ({_SESSION_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _session_values(session),
                )
                if progress is not None:
                    _save_progress(connection, progress)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "queue_sessions.queue_id" in str(exc):
                    raise QueueIdConflictError("Queue ID already exists") from exc
                if "queue_sessions.proxy_session_id" in str(exc):
                    raise ProxySessionIdConflictError(
                        "Proxy session ID already exists"
                    ) from exc
                raise
            return session

        return await self._run(operation)

    async def create_many(
        self,
        sessions: Iterable[tuple[QueueSession, QueueProgress | None]],
    ) -> int:
        """Insert many new sessions in one transaction (benchmark and import seeding)."""

        rows = tuple(sessions)
        for session, progress in rows:
            if progress is not None and progress.session_id != session.session_id:
                raise ValueError("Progress session_id must match the session")

        def operation() -> int:
            connection = self._connect()
            try:
                connection.executemany(
                    f"INSERT INTO queue_sessions ({_SESSION_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_session_values(session) for session, _ in rows),
                )
                for _, progress in rows:
                    if progress is not None:
                        _save_progress(connection, progress)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "queue_sessions.queue_id" in str(exc):
                    raise QueueIdConflictError("Queue ID already exists") from exc
                if "queue_sessions.proxy_session_id" in str(exc):
                    raise ProxySessionIdConflictError(
                        "Proxy session ID already exists"
                    ) from exc
                raise
            return len(rows)

        return await self._run(operation)

    async def update(
        self,
        session: QueueSession,
        progress: QueueProgress | None = None,
    ) -> QueueSession:
        if progress is not None and progress.session_id != session.session_id:
            raise ValueError("Progress session_id must match the session")

        def operation() -> QueueSession:
            connection = self._connect()
            existing = connection.execute(
                "SELECT status, proxy_session_id FROM queue_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if existing is None:
                raise SessionNotFoundError(f"Session {session.session_id!r} does not exist")
            validate_transition(QueueStatus.parse(existing["status"]), session.status)
            if existing["proxy_session_id"] != session.proxy_session_id:
                raise ProxySessionAssignmentError(
                    "Persisted proxy session assignment cannot be changed"
                )
            try:
                ownership_sql = " AND worker_id IS NULL"
                ownership_parameters: tuple[object, ...] = ()
                if session.worker_id is not None:
                    ownership_sql = " AND worker_id = ?"
                    ownership_parameters = (session.worker_id,)
                elif session.manual_owner_id is not None:
                    ownership_sql = " AND worker_id IS NULL AND manual_owner_id = ?"
                    ownership_parameters = (session.manual_owner_id,)
                # created_at is immutable after insert. Leaving it out of SET keeps
                # SQLite from maintaining the dashboard-order index on every update.
                values = _session_values(session)
                cursor = connection.execute(
                    """
                    UPDATE queue_sessions SET
                        queue_id = ?, transfer_url = ?, mode = ?, browser_backend = ?,
                        status = ?, state_path = ?,
                        last_checked_at = ?, last_queue_update = ?,
                        last_progress_change_at = ?, next_check_at = ?, attempt_count = ?,
                        last_error = ?, worker_id = ?, lease_until = ?,
                        manual_owner_id = ?, manual_lease_until = ?
                    WHERE session_id = ?
                    """
                    + ownership_sql,
                    values[1:5]
                    + values[6:8]
                    + values[9:]
                    + (session.session_id,)
                    + ownership_parameters,
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise LeaseOwnershipError(
                        f"Session {session.session_id!r} lease ownership changed"
                    )
                if progress is not None:
                    _save_progress(connection, progress)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "queue_sessions.queue_id" in str(exc):
                    raise QueueIdConflictError("Queue ID already exists") from exc
                if "proxy_session_id is immutable" in str(exc):
                    raise ProxySessionAssignmentError(
                        "Persisted proxy session assignment cannot be changed"
                    ) from exc
                raise
            return session

        return await self._run(operation)

    async def get(self, session_id: str) -> QueueSession | None:
        def operation() -> QueueSession | None:
            row = (
                self._connect()
                .execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions WHERE session_id = ?",
                    (session_id,),
                )
                .fetchone()
            )
            return _row_to_session(row) if row is not None else None

        return await self._run(operation)

    async def list(self, status: QueueStatus | None = None) -> builtins.list[QueueSession]:
        def operation() -> builtins.list[QueueSession]:
            connection = self._connect()
            if status is None:
                rows = connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions ORDER BY created_at, session_id"
                ).fetchall()
            else:
                parsed_status = QueueStatus.parse(status)
                rows = connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions "
                    "WHERE status = ? ORDER BY created_at, session_id",
                    (parsed_status.value,),
                ).fetchall()
            return [_row_to_session(row) for row in rows]

        return await self._run(operation)

    async def count_successful_queue_ids(self) -> int:
        def operation() -> int:
            row = (
                self._connect()
                .execute(
                    """
                SELECT COUNT(DISTINCT queue_id) AS total
                FROM queue_sessions
                WHERE queue_id IS NOT NULL AND status != ?
                """,
                    (QueueStatus.FAILED.value,),
                )
                .fetchone()
            )
            return int(row["total"])

        return await self._run(operation)

    async def count_lost_queue_ids(self) -> int:
        """Count acquired identities that are no longer valid because they FAILED."""

        def operation() -> int:
            row = (
                self._connect()
                .execute(
                    """
                SELECT COUNT(DISTINCT queue_id) AS total
                FROM queue_sessions
                WHERE queue_id IS NOT NULL AND status = ?
                """,
                    (QueueStatus.FAILED.value,),
                )
                .fetchone()
            )
            return int(row["total"])

        return await self._run(operation)

    async def progress_distribution(self) -> dict[str, int]:
        """Bucket last observed progress of non-terminal sessions into fixed ranges."""

        def operation() -> dict[str, int]:
            rows = (
                self._connect()
                .execute(
                    f"""
                SELECT {_progress_bucket_sql()} AS bucket, COUNT(*) AS total
                FROM queue_sessions AS s
                LEFT JOIN queue_progress AS p ON p.session_id = s.session_id
                WHERE s.queue_id IS NOT NULL
                  AND s.status NOT IN ('ADMITTED', 'EXPIRED', 'FAILED')
                GROUP BY bucket
                """
                )
                .fetchall()
            )
            counts = dict.fromkeys(PROGRESS_BUCKETS, 0)
            for row in rows:
                counts[str(row["bucket"])] = int(row["total"])
            return counts

        return await self._run(operation)

    async def recovery_summary(self, *, now: datetime) -> RecoverySummary:
        """Return startup recovery counts without materializing session rows."""

        now_storage = _to_storage(now)
        if now_storage is None:
            raise ValueError("now is required")
        status_columns = ",\n".join(
            f"SUM(CASE WHEN status = '{status.value}' THEN 1 ELSE 0 END) "
            f"AS status_{status.value.lower()}"
            for status in QueueStatus
        )
        terminal_statuses = (
            QueueStatus.ADMITTED.value,
            QueueStatus.EXPIRED.value,
            QueueStatus.FAILED.value,
        )

        def operation() -> RecoverySummary:
            row = (
                self._connect()
                .execute(
                    f"""
                SELECT
                    COUNT(*) AS total_persisted_sessions,
                    COUNT(DISTINCT CASE
                        WHEN queue_id IS NOT NULL AND status != ? THEN queue_id
                    END) AS valid_queue_ids,
                    COUNT(DISTINCT CASE
                        WHEN queue_id IS NOT NULL AND status = ? THEN queue_id
                    END) AS lost_queue_ids,
                    SUM(CASE WHEN lease_until > ? THEN 1 ELSE 0 END) AS leased_sessions,
                    SUM(CASE WHEN lease_until IS NOT NULL AND lease_until <= ? THEN 1 ELSE 0 END)
                        AS expired_leases,
                    SUM(CASE WHEN {_DUE_FILTER_SQL} THEN 1 ELSE 0 END) AS sessions_due,
                    SUM(CASE
                        WHEN status NOT IN (?, ?, ?)
                         AND (
                            last_error IS NOT NULL
                            OR status IN ('NEW', 'CREATING', 'CONNECTION_LOST')
                         )
                        THEN 1 ELSE 0
                    END) AS sessions_requiring_retry,
                    SUM(CASE WHEN status IN (?, ?, ?) THEN 1 ELSE 0 END)
                        AS terminal_sessions,
                    {status_columns}
                FROM queue_sessions
                """,
                    (
                        QueueStatus.FAILED.value,
                        QueueStatus.FAILED.value,
                        now_storage,
                        now_storage,
                        now_storage,
                        now_storage,
                        now_storage,
                        *terminal_statuses,
                        *terminal_statuses,
                    ),
                )
                .fetchone()
            )
            status_counts = {
                status: int(row[f"status_{status.value.lower()}"] or 0) for status in QueueStatus
            }
            return RecoverySummary(
                generated_at=now,
                total_persisted_sessions=int(row["total_persisted_sessions"]),
                valid_queue_ids=int(row["valid_queue_ids"]),
                leased_sessions=int(row["leased_sessions"] or 0),
                expired_leases=int(row["expired_leases"] or 0),
                sessions_due=int(row["sessions_due"] or 0),
                sessions_requiring_retry=int(row["sessions_requiring_retry"] or 0),
                terminal_sessions=int(row["terminal_sessions"] or 0),
                status_counts=status_counts,
                lost_queue_ids=int(row["lost_queue_ids"] or 0),
            )

        return await self._run(operation)

    async def save_progress(self, progress: QueueProgress) -> QueueProgress:
        def operation() -> QueueProgress:
            connection = self._connect()
            try:
                _save_progress(connection, progress)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise SessionNotFoundError(
                    f"Session {progress.session_id!r} does not exist"
                ) from exc
            return progress

        return await self._run(operation)

    async def record_proxy_ip(
        self, session_id: str, *, ip: str, observed_at: datetime
    ) -> ProxyIpRecord | None:
        """Compare-and-record one successful proxy-exit observation atomically.

        Only the proxy-IP columns change; lifecycle, Queue ID, leases, and the
        immutable ``proxy_session_id`` are untouched. Returns ``None`` when the row
        no longer exists (for example it was deleted while the check ran).
        """

        def operation() -> ProxyIpRecord | None:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT proxy_ip FROM queue_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                previous = row["proxy_ip"]
                changed = previous is not None and previous != ip
                observed = _to_storage(observed_at)
                connection.execute(
                    """
                    UPDATE queue_sessions
                    SET proxy_ip = ?, proxy_ip_checked_at = ?,
                        proxy_ip_changed_count = proxy_ip_changed_count + ?,
                        proxy_ip_changed_at = CASE WHEN ? THEN ? ELSE proxy_ip_changed_at END
                    WHERE session_id = ?
                    """,
                    (ip, observed, int(changed), int(changed), observed, session_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return ProxyIpRecord(baseline=previous is None, changed=changed)

        return await self._run(operation)

    async def get_progress(self, session_id: str) -> QueueProgress | None:
        def operation() -> QueueProgress | None:
            row = (
                self._connect()
                .execute(
                    "SELECT * FROM queue_progress WHERE session_id = ?",
                    (session_id,),
                )
                .fetchone()
            )
            return _row_to_progress(row) if row is not None else None

        return await self._run(operation)

    async def list_session_summaries(
        self,
        *,
        page: int,
        page_size: int,
        search: str | None = None,
        status: QueueStatus | None = None,
        runtime_state: BrowserRuntimeState | None = None,
    ) -> SessionSummaryPage:
        """Return a safe dashboard page using one bounded SQL query plus COUNT."""

        if page < 1:
            raise ValueError("page must be at least 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        parsed_status = QueueStatus.parse(status) if status is not None else None
        parsed_runtime = (
            BrowserRuntimeState(runtime_state) if runtime_state is not None else None
        )
        clauses: list[str] = []
        parameters: list[object] = []
        normalized_search = search.strip() if search is not None else ""
        if normalized_search:
            clauses.append(
                "(s.session_id LIKE ? OR s.queue_id LIKE ? OR s.display_name LIKE ?)"
            )
            pattern = f"%{normalized_search}%"
            parameters.extend((pattern, pattern, pattern))
        if parsed_status is not None:
            clauses.append("s.status = ?")
            parameters.append(parsed_status.value)
        now = datetime.now(UTC)
        now_storage = _to_storage(now)
        manual_live = "(s.manual_owner_id IS NOT NULL AND s.manual_lease_until > ?)"
        lease_live = "(s.worker_id IS NOT NULL AND s.lease_until > ?)"
        if parsed_runtime is BrowserRuntimeState.CHECKING:
            clauses.append(f"(NOT {manual_live} AND {lease_live})")
            parameters.extend((now_storage, now_storage))
        elif parsed_runtime is BrowserRuntimeState.PARKED:
            clauses.append(f"(NOT {manual_live} AND NOT {lease_live})")
            parameters.extend((now_storage, now_storage))
        elif parsed_runtime is BrowserRuntimeState.OPEN_IN_CHROME:
            clauses.append(manual_live)
            parameters.append(now_storage)
        where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
        offset = (page - 1) * page_size

        def operation() -> SessionSummaryPage:
            connection = self._connect()
            total_row = connection.execute(
                "SELECT COUNT(*) AS total FROM queue_sessions AS s" + where_sql,
                tuple(parameters),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT s.session_id, s.queue_id, s.status, p.progress_percentage,
                       s.last_queue_update, s.last_checked_at, s.next_check_at,
                       s.worker_id, s.lease_until, s.manual_owner_id, s.manual_lease_until,
                       s.proxy_ip, s.proxy_ip_checked_at, s.proxy_ip_changed_count,
                       TRIM(COALESCE(s.transfer_url, '')) <> '' AS has_transfer_url,
                       s.display_name
                FROM queue_sessions AS s
                LEFT JOIN queue_progress AS p ON p.session_id = s.session_id
                """
                + where_sql
                + " ORDER BY s.created_at, s.session_id LIMIT ? OFFSET ?",
                (*parameters, page_size, offset),
            ).fetchall()
            items = tuple(
                SessionSummary(
                    session_id=str(row["session_id"]),
                    queue_id=row["queue_id"],
                    status=QueueStatus.parse(row["status"]),
                    progress_percentage=row["progress_percentage"],
                    last_queue_update=_from_storage(row["last_queue_update"]),
                    last_checked_at=_from_storage(row["last_checked_at"]),
                    next_check_at=_from_storage(row["next_check_at"]),
                    proxy_ip=row["proxy_ip"],
                    proxy_ip_checked_at=_from_storage(row["proxy_ip_checked_at"]),
                    proxy_ip_changed_count=int(row["proxy_ip_changed_count"] or 0),
                    has_transfer_url=bool(row["has_transfer_url"]),
                    display_name=row["display_name"],
                    runtime_state=_row_runtime_state(row, now=now),
                )
                for row in rows
            )
            return SessionSummaryPage(
                items=items,
                total=int(total_row["total"]),
                page=page,
                page_size=page_size,
            )

        return await self._run(operation)

    async def rename_session(self, session_id: str, display_name: str | None) -> bool:
        """Set or clear one session's operator label; the only writer of the column.

        Ordinary row updates never include it, so monitoring, adoption, and lease
        writes cannot overwrite a name, and naming needs no lease or ownership.
        """

        name = normalize_session_name(display_name)

        def operation() -> bool:
            connection = self._connect()
            cursor = connection.execute(
                "UPDATE queue_sessions SET display_name = ? WHERE session_id = ?",
                (name, session_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def count_due_sessions(self, *, now: datetime) -> int:
        """Count sessions currently eligible for a monitoring lease."""

        return (await self.due_session_summary(now=now)).count

    async def due_session_summary(self, *, now: datetime) -> DueSessionSummary:
        """Return count and oldest due time without loading individual sessions."""

        now_storage = _to_storage(now)
        if now_storage is None:
            raise ValueError("now is required")

        def operation() -> DueSessionSummary:
            row = (
                self._connect()
                .execute(
                    f"""
                SELECT COUNT(*) AS total, MIN({_DUE_TIME_SQL}) AS oldest_due_at
                FROM queue_sessions
                WHERE {_DUE_FILTER_SQL}
                """,
                    (now_storage, now_storage, now_storage),
                )
                .fetchone()
            )
            oldest = row["oldest_due_at"]
            return DueSessionSummary(
                count=int(row["total"]),
                oldest_due_at=_from_storage(oldest) if oldest is not None else None,
            )

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

        def operation() -> ClaimedSessions:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            try:
                control = connection.execute(
                    "SELECT monitoring_paused FROM runtime_control WHERE singleton = 1"
                ).fetchone()
                if control is None:
                    raise ValueError("Persisted runtime control row is missing")
                if bool(control["monitoring_paused"]):
                    connection.commit()
                    return ClaimedSessions()
                rows = connection.execute(
                    f"""
                    SELECT {_SESSION_SELECT_COLUMNS}
                    FROM queue_sessions
                    WHERE {_DUE_FILTER_SQL}
                    ORDER BY {_DUE_ORDER_SQL}
                    LIMIT ?
                    """,
                    (now_storage, now_storage, now_storage, limit),
                ).fetchall()
                session_ids = [row["session_id"] for row in rows]
                # A due row that still carries a lease can only have been selected
                # because that lease expired: its previous owner never released it.
                # A claimer renewing its own lease (a slow local check) is not a
                # recovery from a stopped owner.
                expired = [row["worker_id"] for row in rows if row["lease_until"] is not None]
                recovered = sum(owner != worker_id for owner in expired)
                reclaimed_own = len(expired) - recovered
                if session_ids:
                    placeholders = ", ".join("?" for _ in session_ids)
                    connection.execute(
                        f"UPDATE queue_sessions SET worker_id = ?, lease_until = ?, "
                        "manual_owner_id = NULL, manual_lease_until = NULL "
                        f"WHERE session_id IN ({placeholders})",
                        (worker_id, lease_storage, *session_ids),
                    )
                    rows = connection.execute(
                        f"SELECT {_SESSION_SELECT_COLUMNS} FROM queue_sessions "
                        f"WHERE session_id IN ({placeholders}) "
                        f"ORDER BY {_DUE_ORDER_SQL}",
                        session_ids,
                    ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return ClaimedSessions(
                (_row_to_session(row) for row in rows),
                recovered_expired_leases=recovered,
                reclaimed_own_expired_leases=reclaimed_own,
            )

        return await self._run(operation)

    async def explain_due_session_query(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        """Return SQLite's plan details for the bounded due-session selection."""

        if limit < 1:
            raise ValueError("limit must be at least 1")
        now_storage = _to_storage(now)
        if now_storage is None:
            raise ValueError("now is required")

        def operation() -> tuple[str, ...]:
            rows = (
                self._connect()
                .execute(
                    f"""
                EXPLAIN QUERY PLAN
                SELECT session_id
                FROM queue_sessions
                WHERE {_DUE_FILTER_SQL}
                ORDER BY {_DUE_ORDER_SQL}
                LIMIT ?
                """,
                    (now_storage, now_storage, now_storage, limit),
                )
                .fetchall()
            )
            return tuple(str(row["detail"]) for row in rows)

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
