"""Bounded operator actions composed from existing Queue-it services."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from queue_load_test.models import QueueSession
from queue_load_test.repository import (
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
    """Run browser-backed button actions through a fixed worker pool."""

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
        self._queue: asyncio.Queue[_Work | None] = asyncio.Queue(maxsize=queue_capacity)
        self._workers: list[asyncio.Task[None]] = []
        self._actions: dict[str, OperatorAction] = {}
        self._session_actions: dict[str, str] = {}
        self._latest_add: str | None = None
        self._lease_seconds = lease_seconds
        self._lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"operator-worker-{index}")
            for index in range(self._worker_count)
        ]

    async def request(
        self,
        kind: OperatorActionKind,
        session_id: str | None = None,
    ) -> OperatorAction:
        if kind is not OperatorActionKind.ADD and session_id is None:
            raise ValueError("session_id is required")
        async with self._lock:
            if self._closing:
                return self._rejected(kind, session_id, "Operator actions are shutting down")
            if session_id is not None and session_id in self._session_actions:
                action = self._actions[self._session_actions[session_id]]
                if action.status in {
                    OperatorActionStatus.REQUESTED,
                    OperatorActionStatus.RUNNING,
                }:
                    return action
            action_id = str(uuid4())
            worker_id: str | None = None
            session: QueueSession | None = None
            if session_id is not None:
                worker_id = f"operator-request-{action_id}"
                now = datetime.now(UTC)
                try:
                    session = await self._repository.acquire_operator_lease(
                        session_id,
                        worker_id=worker_id,
                        now=now,
                        lease_until=now + timedelta(seconds=self._lease_seconds),
                    )
                except (ManualSessionBusyError, SessionNotFoundError) as exc:
                    if (
                        isinstance(exc, SessionNotFoundError)
                        and kind is OperatorActionKind.DELETE
                    ):
                        try:
                            await self._state_store.delete(session_id)
                        except Exception:  # noqa: BLE001 - sanitized operator failure
                            return self._rejected(
                                kind, session_id, "Delete failed while removing browser state"
                            )
                        return OperatorAction(
                            action_id=action_id,
                            kind=kind,
                            status=OperatorActionStatus.SUCCESS,
                            session_id=session_id,
                            message="Session was already deleted",
                        )
                    return self._rejected(kind, session_id, str(exc))
            action = OperatorAction(
                action_id=action_id,
                kind=kind,
                status=OperatorActionStatus.REQUESTED,
                session_id=session_id,
                message=f"{self._label(kind)} requested",
            )
            work = _Work(action_id, kind, session_id, worker_id, session)
            try:
                self._queue.put_nowait(work)
            except asyncio.QueueFull:
                if session_id is not None and worker_id is not None:
                    await self._repository.release_lease(session_id, worker_id=worker_id)
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

    async def _worker(self, _index: int) -> None:
        while True:
            work = await self._queue.get()
            try:
                if work is None:
                    return
                await self._set(work.action_id, OperatorActionStatus.RUNNING, "Running")
                try:
                    message = await self._execute(work)
                except (ManualSessionBusyError, SessionNotFoundError) as exc:
                    if isinstance(exc, SessionNotFoundError) and work.kind is OperatorActionKind.DELETE:
                        await self._set(
                            work.action_id,
                            OperatorActionStatus.SUCCESS,
                            "Session was already deleted",
                        )
                    else:
                        await self._set(
                            work.action_id,
                            OperatorActionStatus.FAILED,
                            str(exc),
                        )
                except Exception:  # noqa: BLE001 - never expose internals to the browser
                    await self._set(
                        work.action_id,
                        OperatorActionStatus.FAILED,
                        f"{self._label(work.kind)} failed",
                    )
                else:
                    await self._set(work.action_id, OperatorActionStatus.SUCCESS, message)
            finally:
                self._queue.task_done()

    async def _execute(self, work: _Work) -> str:
        if work.kind is OperatorActionKind.ADD:
            return await self._create_one(adjust_population=True)
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
                await self._state_store.delete(session.session_id)
                self._target_adjustment.adjust_target(-1)
                return "Session deleted"
            if work.kind is OperatorActionKind.REPLACE:
                replacement = await self._create_one(adjust_population=False)
                deleted = await self._repository.delete_owned_session(
                    session.session_id,
                    worker_id=worker_id,
                )
                if not deleted:
                    raise ManualSessionBusyError("Session ownership changed")
                await self._state_store.delete(session.session_id)
                return replacement.replace("added", "replaced")
            raise AssertionError(f"Unsupported operator action: {work.kind}")
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if not deleted:
                await self._repository.release_lease(session.session_id, worker_id=worker_id)

    async def _heartbeat(self, session_id: str, worker_id: str) -> None:
        interval = max(1.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await self._repository.renew_operator_lease(
                session_id,
                worker_id=worker_id,
                lease_until=datetime.now(UTC) + timedelta(seconds=self._lease_seconds),
            )
            if not renewed:
                return

    async def _create_one(self, *, adjust_population: bool) -> str:
        item = CreationWorkItem(sequence=0)
        outcome = await self._creator.create(item)
        if outcome.kind is not CreationOutcomeKind.SUCCESS or outcome.session is None:
            with contextlib.suppress(Exception):
                await self._repository.delete_unowned_session(item.session_id)
            with contextlib.suppress(Exception):
                await self._state_store.delete(item.session_id)
            raise RuntimeError("session acquisition failed")
        if adjust_population:
            await self._repository.adjust_operator_population(1)
            self._target_adjustment.adjust_target(1)
        return "New session added"

    async def _set(
        self,
        action_id: str,
        status: OperatorActionStatus,
        message: str,
    ) -> None:
        async with self._lock:
            current = self._actions[action_id]
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

    def _trim_history(self) -> None:
        if len(self._actions) <= 100:
            return
        for action_id, action in tuple(self._actions.items()):
            if len(self._actions) <= 100:
                break
            if action.status in {
                OperatorActionStatus.REQUESTED,
                OperatorActionStatus.RUNNING,
            }:
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

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        while not self._queue.empty():
            work = self._queue.get_nowait()
            try:
                if (
                    work is not None
                    and work.session_id is not None
                    and work.worker_id is not None
                ):
                    await self._repository.release_lease(
                        work.session_id, worker_id=work.worker_id
                    )
            finally:
                self._queue.task_done()
