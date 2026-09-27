"""Bounded operator actions composed from existing Queue-it services."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from queue_load_test.metrics.logging import log_event
from queue_load_test.models import QueueSession, QueueStatus
from queue_load_test.repository import (
    OPERATOR_WORKER_PREFIX,
    ManualSessionBusyError,
    SessionNotFoundError,
    SessionRepository,
)
from queue_load_test.scheduler import (
    CreationOutcomeKind,
    CreationWorkItem,
    QueueSessionMonitor,
    SessionCreationHandler,
)
from queue_load_test.state import StateStore

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 100
_TOKEN_LIMIT = 256


class OperatorActionKind(StrEnum):
    REFRESH = "REFRESH"
    DELETE = "DELETE"
    REPLACE = "REPLACE"
    ADD = "ADD"


class OperatorActionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


_PENDING = frozenset({OperatorActionStatus.REQUESTED, OperatorActionStatus.RUNNING})


@dataclass(frozen=True, slots=True)
class OperatorAction:
    action_id: str
    kind: OperatorActionKind
    status: OperatorActionStatus
    session_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class _Work:
    action_id: str
    kind: OperatorActionKind
    session_id: str | None
    worker_id: str | None = None
    session: QueueSession | None = None


class TargetAdjustment(Protocol):
    def adjust_target(self, delta: int) -> None: ...


class OperatorActionManager:
    """Run browser-backed button actions through a fixed worker pool.

    Per-session actions acquire a persisted, fenced operator lease *before* they
    are queued, so incompatible work on one session (automatic check, headed
    a live browser, another operator action) is rejected atomically while different
    sessions proceed concurrently up to ``worker_count``. Request tokens make a
    repeated submission of the same rendered button return the original action.
    """

    def __init__(
        self,
        *,
        repository: SessionRepository,
        creator: SessionCreationHandler,
        monitor: QueueSessionMonitor,
        state_store: StateStore,
        target_adjustment: TargetAdjustment,
        worker_count: int,
        queue_capacity: int,
        lease_seconds: float,
    ) -> None:
        self._repository = repository
        self._creator = creator
        self._monitor = monitor
        self._state_store = state_store
        self._target_adjustment = target_adjustment
        self._worker_count = worker_count
        self._queue: asyncio.Queue[_Work] = asyncio.Queue(maxsize=queue_capacity)
        self._workers: list[asyncio.Task[None]] = []
        self._actions: dict[str, OperatorAction] = {}
        self._session_actions: dict[str, str] = {}
        self._tokens: OrderedDict[str, str] = OrderedDict()
        self._latest_add: str | None = None
        self._lease_seconds = lease_seconds
        self._lock = asyncio.Lock()
        self._closing = False
        self._running = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def accepting(self) -> bool:
        return not self._closing

    @property
    def running(self) -> int:
        return self._running

    async def start(self) -> None:
        if self._workers or self._closing:
            return
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"operator-worker-{index}")
            for index in range(self._worker_count)
        ]

    def stop_accepting(self) -> None:
        """Reject new requests immediately; queued and running work is left alone."""

        self._closing = True

    async def request(
        self,
        kind: OperatorActionKind,
        session_id: str | None = None,
        *,
        request_token: str | None = None,
    ) -> OperatorAction:
        if kind is not OperatorActionKind.ADD and session_id is None:
            raise ValueError("session_id is required")
        async with self._lock:
            if request_token:
                duplicate = self._for_token(request_token)
                if duplicate is not None:
                    return duplicate
            action = await self._request_locked(kind, session_id)
            if request_token:
                self._remember_token(request_token, action)
            return action

    async def _request_locked(
        self,
        kind: OperatorActionKind,
        session_id: str | None,
    ) -> OperatorAction:
        if self._closing:
            return self._rejected(kind, session_id, "Operator actions are shutting down")
        if session_id is not None and session_id in self._session_actions:
            action = self._actions.get(self._session_actions[session_id])
            if action is not None and action.status in _PENDING:
                if action.kind is kind:
                    return action
                return self._rejected(
                    kind,
                    session_id,
                    f"{self._label(action.kind)} is already {action.status.value.lower()} "
                    "for this session",
                )
        action_id = str(uuid4())
        worker_id: str | None = None
        session: QueueSession | None = None
        if session_id is not None:
            worker_id = f"{OPERATOR_WORKER_PREFIX}request-{action_id}"
            now = datetime.now(UTC)
            try:
                session = await self._repository.acquire_operator_lease(
                    session_id,
                    worker_id=worker_id,
                    now=now,
                    lease_until=now + timedelta(seconds=self._lease_seconds),
                )
            except SessionNotFoundError:
                if kind is not OperatorActionKind.DELETE:
                    return self._rejected(kind, session_id, "Session no longer exists")
                try:
                    await self._state_store.delete(session_id)
                except Exception:  # noqa: BLE001 - sanitized operator failure
                    return self._rejected(
                        kind, session_id, "Delete failed while removing browser state"
                    )
                return self._record(
                    OperatorAction(
                        action_id=action_id,
                        kind=kind,
                        status=OperatorActionStatus.SUCCESS,
                        session_id=session_id,
                        message="Session was already deleted",
                    )
                )
            except ManualSessionBusyError as exc:
                return self._rejected(kind, session_id, str(exc))
        action = OperatorAction(
            action_id=action_id,
            kind=kind,
            status=OperatorActionStatus.REQUESTED,
            session_id=session_id,
            message=f"{self._label(kind)} requested",
        )
        try:
            self._queue.put_nowait(_Work(action_id, kind, session_id, worker_id, session))
        except asyncio.QueueFull:
            if session_id is not None and worker_id is not None:
                await self._release(session_id, worker_id)
            return self._rejected(kind, session_id, "Operator work capacity unavailable")
        self._actions[action_id] = action
        if session_id is not None:
            self._session_actions[session_id] = action_id
        else:
            self._latest_add = action_id
        self._trim_history()
        return action

    def for_session(self, session_id: str) -> OperatorAction | None:
        action_id = self._session_actions.get(session_id)
        return self._actions.get(action_id) if action_id is not None else None

    def latest_add(self) -> OperatorAction | None:
        return self._actions.get(self._latest_add) if self._latest_add is not None else None

    def _for_token(self, token: str) -> OperatorAction | None:
        action_id = self._tokens.get(token)
        if action_id is None:
            return None
        self._tokens.move_to_end(token)
        return self._actions.get(action_id) or OperatorAction(
            action_id=action_id,
            kind=OperatorActionKind.ADD,
            status=OperatorActionStatus.FAILED,
            session_id=None,
            message="Duplicate submission ignored",
        )

    def _remember_token(self, token: str, action: OperatorAction) -> None:
        self._actions.setdefault(action.action_id, action)
        self._tokens[token] = action.action_id
        while len(self._tokens) > _TOKEN_LIMIT:
            self._tokens.popitem(last=False)
        self._trim_history()

    async def _worker(self, _index: int) -> None:
        while True:
            work = await self._queue.get()
            self._running += 1
            self._idle.clear()
            try:
                await self._set(work.action_id, OperatorActionStatus.RUNNING, "Running")
                try:
                    message = await self._execute(work)
                except asyncio.CancelledError:
                    await self._set(
                        work.action_id,
                        OperatorActionStatus.FAILED,
                        "Cancelled: application shutting down",
                    )
                    raise
                except SessionNotFoundError:
                    if work.kind is OperatorActionKind.DELETE:
                        await self._set(
                            work.action_id,
                            OperatorActionStatus.SUCCESS,
                            "Session was already deleted",
                        )
                    else:
                        await self._set(
                            work.action_id,
                            OperatorActionStatus.FAILED,
                            "Session no longer exists",
                        )
                except ManualSessionBusyError as exc:
                    await self._set(work.action_id, OperatorActionStatus.FAILED, str(exc))
                except Exception as exc:  # noqa: BLE001 - never expose internals to the browser
                    log_event(
                        logger,
                        logging.WARNING,
                        "operator_action_failed",
                        session_id=work.session_id,
                        error_type=type(exc).__name__,
                    )
                    await self._set(
                        work.action_id,
                        OperatorActionStatus.FAILED,
                        f"{self._label(work.kind)} failed",
                    )
                else:
                    await self._set(work.action_id, OperatorActionStatus.SUCCESS, message)
            finally:
                self._running -= 1
                if self._running == 0:
                    self._idle.set()
                self._queue.task_done()

    async def _execute(self, work: _Work) -> str:
        if work.kind is OperatorActionKind.ADD:
            if not await self._create_reserved():
                raise RuntimeError("session acquisition failed")
            return "New session added"
        assert work.session_id is not None and work.worker_id is not None
        assert work.session is not None
        worker_id = work.worker_id
        session = work.session
        deleted = False
        heartbeat = asyncio.create_task(
            self._heartbeat(session.session_id, worker_id),
            name=f"operator-heartbeat-{session.session_id}",
        )
        try:
            if work.kind is OperatorActionKind.REFRESH:
                outcome = await self._monitor.check(session)
                if not outcome.success:
                    raise RuntimeError("refresh observation failed")
                return "Session refreshed"
            if work.kind is OperatorActionKind.DELETE:
                deleted = await self._repository.delete_owned_session(
                    session.session_id,
                    worker_id=worker_id,
                    population_delta=-1,
                )
                if not deleted:
                    raise ManualSessionBusyError("Session ownership changed")
                self._target_adjustment.adjust_target(-1)
                await self._delete_state(session.session_id)
                return "Session deleted"
            if work.kind is OperatorActionKind.REPLACE:
                # Create-first: the replacement reserves +1 population before any
                # browser work, and deleting the old row applies the matching -1 in
                # the same transaction, so a crash at any point keeps the persisted
                # population target equal to the valid identities it describes.
                if not await self._create_reserved():
                    raise RuntimeError("replacement acquisition failed")
                deleted = await self._repository.delete_owned_session(
                    session.session_id,
                    worker_id=worker_id,
                    population_delta=-1,
                )
                if not deleted:
                    raise ManualSessionBusyError(
                        "Replacement added, but the old session's ownership changed; "
                        "both sessions were kept"
                    )
                self._target_adjustment.adjust_target(-1)
                await self._delete_state(session.session_id)
                return "Session replaced"
            raise AssertionError(f"Unsupported operator action: {work.kind}")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if not deleted:
                await self._release(session.session_id, worker_id)

    async def _heartbeat(self, session_id: str, worker_id: str) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._repository.renew_operator_lease(
                    session_id,
                    worker_id=worker_id,
                    lease_until=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
                )
            except Exception as exc:  # noqa: BLE001 - retry next interval; writes stay fenced
                log_event(
                    logger,
                    logging.WARNING,
                    "operator_lease_renewal_failed",
                    session_id=session_id,
                    worker_id=worker_id,
                    error_type=type(exc).__name__,
                )
                continue
            if not renewed:
                return

    async def _create_reserved(self) -> bool:
        """Create one visitor with a persisted +1 population reservation.

        The reservation is written before browser work. If the process dies before
        the visitor is committed, restart acquisition fills exactly that deficit
        through the normal bounded controller; if creation fails in-process the
        reservation is reverted. A valid identity committed before a failure or
        cancellation is kept rather than discarded.
        """

        await self._repository.adjust_operator_population(1)
        item = CreationWorkItem(sequence=0)
        created = False
        try:
            outcome = await self._creator.create(item)
            created = outcome.kind is CreationOutcomeKind.SUCCESS and outcome.session is not None
        finally:
            if not created:
                created = await self._retain_or_discard(item.session_id)
            if created:
                self._target_adjustment.adjust_target(1)
            else:
                await self._revert_reservation()
        return created

    async def _retain_or_discard(self, session_id: str) -> bool:
        try:
            persisted = await self._repository.get(session_id)
        except Exception:  # noqa: BLE001 - cannot prove a valid identity exists
            return False
        if (
            persisted is not None
            and persisted.queue_id is not None
            and persisted.status is not QueueStatus.FAILED
        ):
            return True
        with contextlib.suppress(Exception):
            await self._repository.delete_unowned_session(session_id)
        with contextlib.suppress(Exception):
            await self._state_store.delete(session_id)
        return False

    async def _revert_reservation(self) -> None:
        try:
            await self._repository.adjust_operator_population(-1)
        except Exception as exc:  # noqa: BLE001
            # The +1 stays persisted: restart acquisition will create the one visitor
            # the operator asked for. No existing identity is touched.
            log_event(
                logger,
                logging.ERROR,
                "operator_population_revert_failed",
                error_type=type(exc).__name__,
            )

    async def _delete_state(self, session_id: str) -> None:
        try:
            await self._state_store.delete(session_id)
        except Exception as exc:  # noqa: BLE001 - row is gone; an orphan file is audited
            log_event(
                logger,
                logging.WARNING,
                "operator_state_delete_failed",
                session_id=session_id,
                error_type=type(exc).__name__,
            )

    async def _release(self, session_id: str, worker_id: str) -> None:
        try:
            await self._repository.release_lease(session_id, worker_id=worker_id)
        except Exception as exc:  # noqa: BLE001
            # The lease expires and any fenced claimer may then take it over.
            log_event(
                logger,
                logging.ERROR,
                "operator_lease_release_failed",
                session_id=session_id,
                worker_id=worker_id,
                error_type=type(exc).__name__,
            )

    async def _set(
        self,
        action_id: str,
        status: OperatorActionStatus,
        message: str,
    ) -> None:
        async with self._lock:
            current = self._actions.get(action_id)
            if current is not None:
                self._actions[action_id] = replace(current, status=status, message=message)

    def _rejected(
        self,
        kind: OperatorActionKind,
        session_id: str | None,
        message: str,
    ) -> OperatorAction:
        return OperatorAction(
            action_id=str(uuid4()),
            kind=kind,
            status=OperatorActionStatus.FAILED,
            session_id=session_id,
            message=message,
        )

    def _record(self, action: OperatorAction) -> OperatorAction:
        self._actions[action.action_id] = action
        if action.session_id is not None:
            self._session_actions[action.session_id] = action.action_id
        self._trim_history()
        return action

    def _trim_history(self) -> None:
        if len(self._actions) <= _HISTORY_LIMIT:
            return
        for action_id, action in tuple(self._actions.items()):
            if len(self._actions) <= _HISTORY_LIMIT:
                break
            if action.status in _PENDING:
                continue
            self._actions.pop(action_id, None)
            if (
                action.session_id is not None
                and self._session_actions.get(action.session_id) == action_id
            ):
                self._session_actions.pop(action.session_id, None)

    @staticmethod
    def _label(kind: OperatorActionKind) -> str:
        return {
            OperatorActionKind.REFRESH: "Refresh",
            OperatorActionKind.DELETE: "Delete",
            OperatorActionKind.REPLACE: "Replace",
            OperatorActionKind.ADD: "Add",
        }[kind]

    async def close(self, *, timeout_seconds: float | None = None) -> None:
        """Stop accepting, drop queued work, and let running work finish.

        Queued work never started, so its lease is released and it is reported as
        cancelled. Running work gets ``timeout_seconds`` to finish and persist; it
        is then cancelled, and its ``finally`` blocks still release ownership,
        revert unfulfilled reservations, and close browser contexts.
        """

        self._closing = True
        while not self._queue.empty():
            work = self._queue.get_nowait()
            try:
                if work.session_id is not None and work.worker_id is not None:
                    await self._release(work.session_id, work.worker_id)
                await self._set(
                    work.action_id,
                    OperatorActionStatus.FAILED,
                    "Cancelled: application shutting down",
                )
            finally:
                self._queue.task_done()
        if self._running:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout=timeout_seconds)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
