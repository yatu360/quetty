"""Per-session fencing, duplicate submissions, and failure recovery for operator work."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from queue_load_test.browser import BrowserCapacity, BrowserManager, BrowserProcessCapacity
from queue_load_test.models import (
    BrowserRuntimeState,
    QueueProgress,
    QueueSession,
    QueueStatus,
    SessionMode,
)
from queue_load_test.repository import ManualSessionBusyError, SQLiteSessionRepository
from queue_load_test.runtime import ApplicationRuntime
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionMonitor,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import (
    OpenedSessionRestore,
    QueueSessionRestorer,
    RestoreFailure,
    RestoreMethod,
    SessionRestoreResult,
)
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)
from queue_load_test.web.manual import ManualChromeSessionManager, ManualOpenError

NOW = datetime.now(UTC)
TERMINAL = {OperatorActionStatus.SUCCESS, OperatorActionStatus.FAILED}


def session(session_id: str = "s0", **fields: Any) -> QueueSession:
    return QueueSession(
        session_id=session_id,
        queue_id=f"queue-{session_id}",
        transfer_url=f"https://queue.test/?q=queue-{session_id}",
        mode=SessionMode.HYBRID,
        status=fields.pop("status", QueueStatus.ACTIVE_QUEUE),
        state_path=Path(f"{session_id}.json"),
        next_check_at=NOW + timedelta(hours=1),
        **fields,
    )


class Target:
    def __init__(self) -> None:
        self.adjustments: list[int] = []

    def adjust_target(self, delta: int) -> None:
        self.adjustments.append(delta)


class Creator:
    """Creates valid visitors; ``mode`` selects a deterministic failure."""

    def __init__(self, repository: SQLiteSessionRepository, mode: str = "ok") -> None:
        self.repository = repository
        self.mode = mode
        self.calls = 0

    async def create(self, item: CreationWorkItem) -> CreationOutcome:
        self.calls += 1
        if self.mode == "duplicate":
            # The real creator persists the attempt as FAILED "duplicate_queue_id".
            await self.repository.create(
                QueueSession(
                    session_id=item.session_id,
                    transfer_url="",
                    mode=SessionMode.HYBRID,
                    status=QueueStatus.FAILED,
                    state_path=Path(f"{item.session_id}.json"),
                    last_error="duplicate_queue_id",
                )
            )
            return CreationOutcome(
                kind=CreationOutcomeKind.DUPLICATE,
                attempts=1,
                temporary_failures=0,
                duration_seconds=0,
                failure_code="duplicate_queue_id",
            )
        if self.mode == "raise_before_commit":
            await self.repository.create(
                QueueSession(
                    session_id=item.session_id,
                    transfer_url="",
                    mode=SessionMode.HYBRID,
                    status=QueueStatus.CREATING,
                    state_path=Path(f"{item.session_id}.json"),
                )
            )
            raise RuntimeError("browser crashed mid-creation")
        created = QueueSession(
            session_id=item.session_id,
            queue_id=f"new-{self.calls}",
            transfer_url=f"https://queue.test/?q=new-{self.calls}",
            mode=SessionMode.HYBRID,
            status=QueueStatus.PRE_QUEUE,
            state_path=Path(f"{item.session_id}.json"),
        )
        await self.repository.create(created)
        if self.mode == "raise_after_commit":
            raise RuntimeError("failure after the identity was persisted")
        return CreationOutcome(
            kind=CreationOutcomeKind.SUCCESS,
            attempts=1,
            temporary_failures=0,
            duration_seconds=0,
            session=created,
        )


class Restorer:
    """Returns a scripted result; ``gate`` holds restores open to create overlap."""

    def __init__(self, result: str = "ok") -> None:
        self.result = result
        self.gate: asyncio.Event | None = None
        self.running = 0
        self.maximum_running = 0
        self.calls = 0

    async def restore(self, item: QueueSession) -> SessionRestoreResult:
        self.calls += 1
        self.running += 1
        self.maximum_running = max(self.maximum_running, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.running -= 1
        progress = QueueProgress(
            session_id=item.session_id, progress_percentage=70.0, active_queue=True
        )
        if self.result == "state_unavailable":
            return SessionRestoreResult(
                method=RestoreMethod.STORAGE_STATE,
                success=False,
                expected_queue_id=item.queue_id,
                failure=RestoreFailure.STATE_UNAVAILABLE,
            )
        if self.result == "state_save_failed":
            return SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=False,
                expected_queue_id=item.queue_id,
                observed_queue_id=item.queue_id,
                identity_match=True,
                progress=progress,
                failure=RestoreFailure.STATE_REFRESH_FAILED,
            )
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=item.queue_id,
            observed_queue_id=item.queue_id,
            identity_match=True,
            progress=progress,
        )


async def setup(
    tmp_path: Path,
    *,
    sessions: int = 1,
    creator_mode: str = "ok",
    restorer: Restorer | None = None,
    workers: int = 2,
    capacity: int = 10,
    due: bool = False,
) -> tuple[OperatorActionManager, SQLiteSessionRepository, Creator, Restorer, Target]:
    repository = SQLiteSessionRepository(tmp_path / "fencing.sqlite3")
    await repository.initialize()
    for index in range(sessions):
        item = session(f"s{index}")
        if due:
            item.next_check_at = NOW - timedelta(minutes=1)
        await repository.create(item)
    restorer = restorer or Restorer()
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
    )
    creator = Creator(repository, creator_mode)
    target = Target()
    manager = OperatorActionManager(
        repository=repository,
        creator=creator,
        monitor=monitor,
        state_store=FileSystemStateStore(tmp_path / "state"),
        target_adjustment=target,
        worker_count=workers,
        queue_capacity=capacity,
        lease_seconds=30,
    )
    await manager.start()
    return manager, repository, creator, restorer, target


async def settle(manager: OperatorActionManager, session_id: str | None) -> OperatorActionStatus:
    for _ in range(300):
        action = manager.latest_add() if session_id is None else manager.for_session(session_id)
        if action is not None and action.status in TERMINAL:
            return action.status
        await asyncio.sleep(0.01)
    raise AssertionError("action did not finish")


async def owners(repository: SQLiteSessionRepository) -> int:
    rows = await repository.list()
    return sum(row.worker_id is not None or row.manual_owner_id is not None for row in rows)


# ---------------------------------------------------------------- repository rules


async def test_runtime_state_is_ownership_not_lifecycle(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "state.sqlite3")
    past = datetime.now(UTC) - timedelta(seconds=5)
    future = datetime.now(UTC) + timedelta(minutes=5)
    await repository.create(session("lifecycle-checking", status=QueueStatus.CHECKING))
    await repository.create(session("expired-lease", worker_id="dead", lease_until=past))
    await repository.create(session("live-lease", worker_id="live", lease_until=future))
    page = await repository.list_session_summaries(page=1, page_size=10)
    states = {item.session_id: item.runtime_state for item in page.items}
    assert states == {
        "lifecycle-checking": BrowserRuntimeState.PARKED,
        "expired-lease": BrowserRuntimeState.PARKED,
        "live-lease": BrowserRuntimeState.CHECKING,
    }
    parked = await repository.list_session_summaries(
        page=1, page_size=10, runtime_state=BrowserRuntimeState.PARKED
    )
    assert {item.session_id for item in parked.items} == {"lifecycle-checking", "expired-lease"}
    checking = await repository.list_session_summaries(
        page=1, page_size=10, runtime_state=BrowserRuntimeState.CHECKING
    )
    assert [item.session_id for item in checking.items] == ["live-lease"]
    await repository.close()


async def test_expired_lease_is_taken_over_but_live_lease_is_respected(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "takeover.sqlite3")
    now = datetime.now(UTC)
    await repository.create(session("expired", worker_id="monitor-dead", lease_until=now))
    await repository.create(
        session("operator", worker_id="operator-request-x", lease_until=now + timedelta(hours=1))
    )
    await repository.create(
        session("monitor", worker_id="monitor-live", lease_until=now + timedelta(hours=1))
    )
    later = now + timedelta(seconds=1)
    taken = await repository.acquire_manual_ownership(
        "expired", owner_id="manual", now=later, lease_until=later + timedelta(seconds=30),
        capacity=5,
    )
    assert taken.manual_owner_id == "manual" and taken.worker_id is None
    with pytest.raises(ManualSessionBusyError, match="Another operator action"):
        await repository.acquire_operator_lease(
            "operator", worker_id="operator-2", now=later, lease_until=later + timedelta(seconds=9)
        )
    with pytest.raises(ManualSessionBusyError, match="currently being checked"):
        await repository.acquire_manual_ownership(
            "monitor", owner_id="m2", now=later, lease_until=later + timedelta(seconds=9),
            capacity=5,
        )
    # The dead owner's late write is fenced out after the takeover.
    stale = session("expired", worker_id="monitor-dead", lease_until=now)
    with pytest.raises(Exception, match="lease ownership changed"):
        await repository.update(stale)
    assert (await repository.get("expired")).queue_id == "queue-expired"  # type: ignore[union-attr]
    await repository.close()


async def test_exclusive_startup_recovery_clears_every_owner_but_no_identity(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "recover.sqlite3")
    future = datetime.now(UTC) + timedelta(minutes=5)
    await repository.create(session("a", manual_owner_id="m", manual_lease_until=future))
    await repository.create(session("b", worker_id="operator-request-1", lease_until=future))
    await repository.create(session("c", worker_id="monitor-1", lease_until=future))
    await repository.create(session("d"))
    recovery = await repository.recover_startup_ownership(now=datetime.now(UTC), exclusive=True)
    assert (recovery.manual_released, recovery.leases_released) == (1, 2)
    assert await owners(repository) == 0
    assert {row.session_id: row.queue_id for row in await repository.list()} == {
        key: f"queue-{key}" for key in "abcd"
    }
    await repository.close()


# ------------------------------------------------------------- duplicate submission


async def test_duplicate_replace_and_delete_and_refresh_submissions(tmp_path: Path) -> None:
    restorer = Restorer()
    restorer.gate = asyncio.Event()
    manager, repository, creator, _, _ = await setup(tmp_path, sessions=3, restorer=restorer)

    first, again = await asyncio.gather(
        manager.request(OperatorActionKind.REFRESH, "s0", request_token="t1"),
        manager.request(OperatorActionKind.REFRESH, "s0", request_token="t1"),
    )
    assert first.action_id == again.action_id
    untokened = await manager.request(OperatorActionKind.REFRESH, "s0")
    assert untokened.action_id == first.action_id  # still pending: same action returned
    restorer.gate.set()
    assert await settle(manager, "s0") is OperatorActionStatus.SUCCESS
    assert restorer.calls == 1

    replace = await manager.request(OperatorActionKind.REPLACE, "s1", request_token="r")
    assert await settle(manager, "s1") is OperatorActionStatus.SUCCESS
    repeated = await manager.request(OperatorActionKind.REPLACE, "s1", request_token="r")
    assert repeated.action_id == replace.action_id
    assert repeated.status is OperatorActionStatus.SUCCESS
    assert creator.calls == 1

    deletes = await asyncio.gather(
        *(manager.request(OperatorActionKind.DELETE, "s2") for _ in range(3))
    )
    assert len({action.action_id for action in deletes}) == 1
    assert await settle(manager, "s2") is OperatorActionStatus.SUCCESS
    after = await manager.request(OperatorActionKind.DELETE, "s2")
    assert after.status is OperatorActionStatus.SUCCESS
    assert await repository.get("s2") is None
    # Replace nets zero; one Delete is -1.
    assert await repository.get_operator_population_adjustment() == -1
    assert await owners(repository) == 0
    await manager.close()
    await repository.close()


# --------------------------------------------------------------- conflicting work


async def test_conflicting_actions_on_one_session_are_fenced(tmp_path: Path) -> None:
    restorer = Restorer()
    restorer.gate = asyncio.Event()
    manager, repository, creator, _, _ = await setup(tmp_path, restorer=restorer, due=True)
    headed = ManualChromeSessionManager(
        repository=repository,
        browser_manager=cast(BrowserManager, _Headed()),
        restorer=cast(QueueSessionRestorer, _NeverOpens()),
        monitor=cast(QueueSessionMonitor, None),
        capacity=2,
        lease_seconds=30,
    )

    results = await asyncio.gather(
        manager.request(OperatorActionKind.REFRESH, "s0"),
        manager.request(OperatorActionKind.DELETE, "s0"),
        manager.request(OperatorActionKind.REPLACE, "s0"),
    )
    accepted = [item for item in results if item.status is OperatorActionStatus.REQUESTED]
    rejected = [item for item in results if item.status is OperatorActionStatus.FAILED]
    assert [item.kind for item in accepted] == [OperatorActionKind.REFRESH]
    assert len(rejected) == 2
    assert all("already" in item.message for item in rejected)
    await asyncio.sleep(0.02)
    with pytest.raises(ManualOpenError, match="Another operator action"):
        await headed.open("s0")
    claimed = await repository.claim_due_sessions(
        worker_id="monitor",
        now=datetime.now(UTC),
        lease_until=datetime.now(UTC) + timedelta(minutes=1),
        limit=5,
    )
    assert claimed == []  # automatic CHECKING is excluded while the operator owns it

    restorer.gate.set()
    assert await settle(manager, "s0") is OperatorActionStatus.SUCCESS
    assert creator.calls == 0
    assert (await repository.get("s0")).queue_id == "queue-s0"  # type: ignore[union-attr]
    assert await owners(repository) == 0
    await headed.close()
    await manager.close()
    await repository.close()


async def test_actions_on_separate_sessions_run_concurrently(tmp_path: Path) -> None:
    restorer = Restorer()
    restorer.gate = asyncio.Event()
    manager, repository, _, _, _ = await setup(
        tmp_path, sessions=3, restorer=restorer, workers=2
    )
    for index in range(3):
        requested = await manager.request(OperatorActionKind.REFRESH, f"s{index}")
        assert requested.status is OperatorActionStatus.REQUESTED
    for _ in range(100):
        if restorer.running == 2:
            break
        await asyncio.sleep(0.01)
    assert restorer.running == 2  # bounded by OPERATOR_WORKERS, not serialized globally
    restorer.gate.set()
    for index in range(3):
        assert await settle(manager, f"s{index}") is OperatorActionStatus.SUCCESS
    assert restorer.maximum_running == 2
    assert await owners(repository) == 0
    await manager.close()
    await repository.close()


# ------------------------------------------------------------------ failed creation


async def test_failed_replacement_that_raises_keeps_old_identity(tmp_path: Path) -> None:
    manager, repository, _, _, target = await setup(tmp_path, creator_mode="raise_before_commit")
    await manager.request(OperatorActionKind.REPLACE, "s0")
    assert await settle(manager, "s0") is OperatorActionStatus.FAILED
    rows = await repository.list()
    assert [(row.session_id, row.queue_id) for row in rows] == [("s0", "queue-s0")]
    assert await repository.get_operator_population_adjustment() == 0
    assert target.adjustments == []
    assert await owners(repository) == 0
    await manager.close()
    await repository.close()


async def test_duplicate_replacement_is_not_success_and_keeps_old(tmp_path: Path) -> None:
    manager, repository, _, _, target = await setup(tmp_path, creator_mode="duplicate")
    await manager.request(OperatorActionKind.REPLACE, "s0")
    assert await settle(manager, "s0") is OperatorActionStatus.FAILED
    rows = await repository.list()
    assert [(row.session_id, row.queue_id) for row in rows] == [("s0", "queue-s0")]
    assert await repository.get_operator_population_adjustment() == 0
    assert target.adjustments == []
    await manager.close()
    await repository.close()


async def test_delete_without_any_state_file_succeeds(tmp_path: Path) -> None:
    manager, repository, _, _, _ = await setup(tmp_path, sessions=2)
    assert not (tmp_path / "state" / "s0.json").exists()
    await manager.request(OperatorActionKind.DELETE, "s0")
    assert await settle(manager, "s0") is OperatorActionStatus.SUCCESS
    assert [row.session_id for row in await repository.list()] == ["s1"]
    await manager.close()
    await repository.close()


async def test_add_failure_before_commit_reverts_reservation(tmp_path: Path) -> None:
    manager, repository, _, _, target = await setup(tmp_path, creator_mode="raise_before_commit")
    await manager.request(OperatorActionKind.ADD)
    assert await settle(manager, None) is OperatorActionStatus.FAILED
    assert [row.session_id for row in await repository.list()] == ["s0"]
    assert await repository.get_operator_population_adjustment() == 0
    assert target.adjustments == []
    await manager.close()
    await repository.close()


async def test_add_failure_after_commit_keeps_valid_identity(tmp_path: Path) -> None:
    manager, repository, _, _, target = await setup(tmp_path, creator_mode="raise_after_commit")
    await manager.request(OperatorActionKind.ADD)
    assert await settle(manager, None) is OperatorActionStatus.FAILED
    assert await repository.count_successful_queue_ids() == 2
    # The persisted population matches the identities that actually exist.
    assert await repository.get_operator_population_adjustment() == 1
    assert target.adjustments == [1]
    await manager.close()
    await repository.close()


# ------------------------------------------------------------------ state failures


@pytest.mark.parametrize(
    ("mode", "progress"),
    [("state_unavailable", None), ("state_save_failed", 70.0)],
)
async def test_state_failures_during_refresh_preserve_identity(
    tmp_path: Path, mode: str, progress: float | None
) -> None:
    manager, repository, _, _, _ = await setup(tmp_path, restorer=Restorer(mode))
    await manager.request(OperatorActionKind.REFRESH, "s0")
    assert await settle(manager, "s0") is OperatorActionStatus.FAILED
    persisted = await repository.get("s0")
    assert persisted is not None
    assert persisted.queue_id == "queue-s0"
    assert persisted.status is not QueueStatus.FAILED
    assert persisted.next_check_at is not None
    stored = await repository.get_progress("s0")
    assert (stored.progress_percentage if stored else None) == progress
    assert await owners(repository) == 0
    await manager.close()
    await repository.close()


# ------------------------------------------------------------------------ shutdown


async def test_close_finishes_running_work_and_cancels_queued_work(tmp_path: Path) -> None:
    restorer = Restorer()
    restorer.gate = asyncio.Event()
    manager, repository, _, _, _ = await setup(tmp_path, sessions=2, restorer=restorer, workers=1)
    await manager.request(OperatorActionKind.REFRESH, "s0")
    await manager.request(OperatorActionKind.REFRESH, "s1")
    while restorer.running < 1:
        await asyncio.sleep(0.01)
    closing = asyncio.create_task(manager.close(timeout_seconds=2))
    await asyncio.sleep(0.02)
    rejected = await manager.request(OperatorActionKind.REFRESH, "s1")
    assert "shutting down" in rejected.message
    restorer.gate.set()
    await closing
    assert manager.for_session("s0").status is OperatorActionStatus.SUCCESS  # type: ignore[union-attr]
    queued = manager.for_session("s1")
    assert queued is not None and "Cancelled" in queued.message
    assert restorer.calls == 1
    assert await owners(repository) == 0
    await repository.close()


async def test_close_timeout_cancels_running_work_and_releases_lease(tmp_path: Path) -> None:
    restorer = Restorer()
    restorer.gate = asyncio.Event()  # never released
    manager, repository, _, _, _ = await setup(tmp_path, restorer=restorer)
    await manager.request(OperatorActionKind.REFRESH, "s0")
    while restorer.running < 1:
        await asyncio.sleep(0.01)
    await manager.close(timeout_seconds=0.05)
    action = manager.for_session("s0")
    assert action is not None and action.status is OperatorActionStatus.FAILED
    assert await owners(repository) == 0
    assert (await repository.get("s0")).queue_id == "queue-s0"  # type: ignore[union-attr]
    await repository.close()


# ------------------------------------------------------------- headed Chrome faults


class _Headed:
    def __init__(self) -> None:
        self.started = False
        self.connected = True
        self.repair: list[bool] = []
        self.shutdowns = 0

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.started = False
        self.shutdowns += 1

    async def capacity(self, *, repair: bool = True) -> BrowserCapacity:
        self.repair.append(repair)
        return BrowserCapacity(
            chrome_processes=1,
            connected_processes=int(self.connected),
            active_contexts=1,
            available_contexts=1,
            maximum_active_contexts=2,
            processes=(BrowserProcessCapacity(0, self.connected, 1, 1),),
        )


class _NeverOpens:
    async def restore_open(self, _: QueueSession) -> OpenedSessionRestore:
        raise AssertionError("fenced sessions must not reach the browser")


class _Events:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.closed = False

    def on(self, _: str, handler: Any) -> None:
        self.handlers.append(handler)

    def is_closed(self) -> bool:
        return self.closed


class _Owned:
    open_count = 0

    def __init__(self) -> None:
        self.context = _Events()
        self.closed = False
        self.browser_id = 0
        _Owned.open_count += 1

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            _Owned.open_count -= 1


class _CrashingRestorer:
    def __init__(self, crash_during_open: bool) -> None:
        self.crash_during_open = crash_during_open
        self.owned: list[_Owned] = []

    async def restore_open(self, item: QueueSession) -> OpenedSessionRestore:
        owned = _Owned()
        self.owned.append(owned)
        if self.crash_during_open:
            await owned.close()  # the restorer closes what it created, then fails
            raise RuntimeError("Target page, context or browser has been closed")
        result = SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=item.queue_id,
            observed_queue_id=item.queue_id,
            identity_match=True,
        )
        return OpenedSessionRestore(result, cast(Any, owned), cast(Any, _Events()))

    async def inspect_open(self, *_: object, **__: object) -> SessionRestoreResult:
        raise AssertionError("a lost browser is not inspected")


async def _headed_manager(
    tmp_path: Path, restorer: _CrashingRestorer, *, lease_seconds: float = 30
) -> tuple[ManualChromeSessionManager, SQLiteSessionRepository, _Headed]:
    repository = SQLiteSessionRepository(tmp_path / "headed.sqlite3")
    await repository.create(session("s0"))
    headed = _Headed()
    manager = ManualChromeSessionManager(
        repository=repository,
        browser_manager=cast(BrowserManager, headed),
        restorer=cast(QueueSessionRestorer, restorer),
        monitor=cast(QueueSessionMonitor, None),
        capacity=2,
        lease_seconds=lease_seconds,
    )
    return manager, repository, headed


async def test_chrome_crash_during_manual_open_releases_ownership(tmp_path: Path) -> None:
    _Owned.open_count = 0
    manager, repository, _ = await _headed_manager(tmp_path, _CrashingRestorer(True))
    with pytest.raises(ManualOpenError, match="Unable to open"):
        await manager.open("s0")
    persisted = await repository.get("s0")
    assert persisted is not None and persisted.manual_owner_id is None
    assert persisted.queue_id == "queue-s0"
    assert _Owned.open_count == 0
    assert manager.open_count == 0
    await manager.close()
    await repository.close()


async def test_chrome_loss_while_open_is_detected_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Owned.open_count = 0
    manager, repository, headed = await _headed_manager(
        tmp_path, _CrashingRestorer(False), lease_seconds=3.3
    )
    await manager.open("s0")
    original = repository.renew_manual_ownership
    failures = {"remaining": 1}

    async def flaky(*args: Any, **kwargs: Any) -> bool:
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise OSError("database briefly unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "renew_manual_ownership", flaky)
    for _ in range(500):  # first heartbeat: renewal fails once, window stays open
        if failures["remaining"] == 0:
            break
        await asyncio.sleep(0.01)
    assert failures["remaining"] == 0
    await asyncio.sleep(0)
    assert manager.open_count == 1
    headed.connected = False  # Chrome crashed
    for _ in range(300):
        if manager.open_count == 0:
            break
        await asyncio.sleep(0.01)
    assert manager.open_count == 0
    assert headed.repair and not any(headed.repair)  # never relaunched a visible window
    persisted = await repository.get("s0")
    assert persisted is not None and persisted.manual_owner_id is None
    assert persisted.queue_id == "queue-s0"
    assert _Owned.open_count == 0
    await manager.close()
    await repository.close()


# ------------------------------------------------------------------------- runtime


class _Browser:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def start(self) -> None:
        self.order.append("browser-start")

    async def shutdown(self) -> None:
        self.order.append("browser-shutdown")


class _IdleHandler:
    async def check(self, _: QueueSession) -> Any:
        raise AssertionError("no work is due")


async def test_runtime_leaves_host_signal_handlers_and_orders_shutdown_hooks(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    repository = SQLiteSessionRepository(tmp_path / "runtime.sqlite3")
    scheduler = ParkedSessionScheduler(
        repository=repository,
        handler=_IdleHandler(),
        worker_count=1,
        queue_capacity=1,
        claim_batch_size=1,
        lease_seconds=5,
        failure_delay_seconds=1,
        scheduler_tick_seconds=0.01,
        shutdown_timeout_seconds=1,
    )

    async def hook(name: str) -> None:
        order.append(name)

    runtime = ApplicationRuntime(
        browser_manager=cast(BrowserManager, _Browser(order)),
        repository=repository,
        monitoring_scheduler=scheduler,
        shutdown_timeout_seconds=1,
        install_signal_handlers=False,
        before_browser_shutdown=(
            ("operator_actions", lambda: hook("operator")),
            ("manual_chrome", lambda: hook("manual")),
        ),
    )
    host_handler = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(runtime.run())
    while "browser-start" not in order:
        await asyncio.sleep(0.01)
    assert signal.getsignal(signal.SIGINT) is host_handler
    runtime.request_shutdown()
    await asyncio.wait_for(task, timeout=2)
    assert order == ["browser-start", "operator", "manual", "browser-shutdown"]
    assert signal.getsignal(signal.SIGINT) is host_handler
