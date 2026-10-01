"""Phase 9 Prompt 2: real-browser per-session sticky proxy routing (local only).

Installed Patchright/Chrome contexts route through a local authenticating proxy
(``LocalAuthProxy``) to the local queue simulator. The proxy records the IPRoyal-style
sticky-session ID each forwarded request authenticated with, so every assertion is
about what was actually sent on the wire. No IPRoyal, Queue-it, or internet traffic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.browser import BrowserManager, ChromeBackend, PatchrightBackend
from queue_load_test.config import Settings
from queue_load_test.harness.local_auth_proxy import (
    MODE_REJECT_AUTH,
    MODE_UPSTREAM_DOWN,
    LocalAuthProxy,
)
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.models import (
    BrowserBackendName,
    ProxyProvider,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.proxy import (
    IPRoyalCredentials,
    ProxyDiagnostics,
    SessionProxyResolver,
    is_valid_proxy_session_id,
)
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcomeKind,
    CreationRetryPolicy,
    CreationWorkItem,
    MonitoringRetryPolicy,
    PollingPolicy,
    QueueSessionCreator,
    QueueSessionMonitor,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer, RestoreFailure
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)
from queue_load_test.web.manual import ManualChromeSessionManager

FAKE_USERNAME = "FAKEUSER_routing7Q"
FAKE_PASSWORD = "FAKEPASS_routing3Zx"
BACKENDS = (BrowserBackendName.PATCHRIGHT, BrowserBackendName.CHROME)
PROXY_UNREACHABLE = "proxy_unreachable"


class LoopbackProxyBackend:
    """Test-only backend wrapper: Chromium never proxies loopback unless told to.

    Production passes exactly server/username/password. This wrapper only adds the
    Chromium ``<-loopback>`` bypass rule so the local simulator is reached *through*
    the local proxy, as a real target is reached through IPRoyal.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def new_context(self, browser: Any, **options: Any) -> Any:
        proxy = options.get("proxy")
        if proxy is not None:
            options["proxy"] = {**proxy, "bypass": "<-loopback>"}
        return await self._inner.new_context(browser, **options)


def _backend(name: BrowserBackendName) -> LoopbackProxyBackend:
    inner = PatchrightBackend() if name is BrowserBackendName.PATCHRIGHT else ChromeBackend()
    return LoopbackProxyBackend(inner)


def _manager(name: BrowserBackendName, contexts: int = 4) -> BrowserManager:
    return BrowserManager(
        chrome_process_count=1,
        max_contexts_per_browser=contexts,
        max_active_contexts=contexts,
        headless=True,
        backend=_backend(name),  # type: ignore[arg-type]
        operation_timeout_seconds=15,
        close_timeout_seconds=5,
    )


def _run(backend: BrowserBackendName) -> RunConfig:
    return RunConfig(
        run_id="routing-run",
        target_url="http://127.0.0.1/entry",
        requested_sessions=3,
        created_at=datetime.now(UTC),
        browser_backend=backend,
        proxy_provider=ProxyProvider.IPROYAL,
        proxy_country="gb",
        proxy_lifetime="2h",
    )


@dataclass
class World:
    backend: BrowserBackendName
    root: Path
    simulator: LocalQueueSimulator
    proxy: LocalAuthProxy
    repository: SQLiteSessionRepository
    state_store: FileSystemStateStore
    manager: BrowserManager
    resolver: SessionProxyResolver
    resolved: list[ProxyDiagnostics]

    def credentials(self) -> IPRoyalCredentials:
        return IPRoyalCredentials(
            server=self.proxy.url, username=FAKE_USERNAME, base_password=FAKE_PASSWORD
        )

    def creator(self, manager: BrowserManager | None = None) -> QueueSessionCreator:
        return QueueSessionCreator(
            browser_manager=manager or self.manager,
            repository=self.repository,
            state_store=self.state_store,
            staging_url=self.simulator.entry_url,
            state_directory=self.state_store.directory,
            mode=SessionMode.HYBRID,
            browser_backend=self.backend,
            proxy_resolver=self.resolver,
            navigation_timeout_ms=8_000,
            retry_policy=CreationRetryPolicy(
                max_attempts=1,
                initial_backoff_seconds=0,
                maximum_backoff_seconds=0,
                jitter_seconds=0,
            ),
        )

    def restorer(self, manager: BrowserManager | None = None) -> QueueSessionRestorer:
        return QueueSessionRestorer(
            browser_manager=manager or self.manager,
            repository=self.repository,
            state_store=self.state_store,
            expected_journey_url=self.simulator.queue_url,
            storage_navigation_url=self.simulator.entry_url,
            admission_detector=AdmissionDetector.from_urls(self.simulator.protected_url),
            navigation_timeout_ms=8_000,
            admission_wait_timeout_ms=0,
            observation_timeout_seconds=2,
            observation_interval_seconds=0.05,
            browser_backend=self.backend,
            proxy_resolver=self.resolver,
        )

    def monitor(self, restorer: QueueSessionRestorer | None = None) -> QueueSessionMonitor:
        return QueueSessionMonitor(
            repository=self.repository,
            restorer=restorer or self.restorer(),
            polling_policy=PollingPolicy(jitter_seconds=0),
            retry_policy=MonitoringRetryPolicy(
                max_attempts=1, initial_backoff_seconds=0, maximum_backoff_seconds=0,
                jitter_seconds=0,
            ),
        )

    def assert_all_traffic_proxied(self) -> None:
        # Every simulator request arrived through the proxy: none went direct.
        assert self.simulator.requests == len(self.proxy.requests)


@pytest.fixture(params=BACKENDS, ids=lambda item: item.value)
async def world(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[World]:
    backend: BrowserBackendName = request.param
    simulator = LocalQueueSimulator(new_identity_prefix=f"route-{backend.value.lower()}")
    proxy = LocalAuthProxy()
    await simulator.start()
    await proxy.start()
    repository = SQLiteSessionRepository(tmp_path / "routing.sqlite3")
    await repository.create_run(_run(backend))
    resolved: list[ProxyDiagnostics] = []
    credentials = IPRoyalCredentials(
        server=proxy.url, username=FAKE_USERNAME, base_password=FAKE_PASSWORD
    )
    manager = _manager(backend)
    await manager.start()
    state = World(
        backend=backend,
        root=tmp_path,
        simulator=simulator,
        proxy=proxy,
        repository=repository,
        state_store=FileSystemStateStore(tmp_path / "state"),
        manager=manager,
        resolver=SessionProxyResolver(_run(backend), credentials, on_resolved=resolved.append),
        resolved=resolved,
    )
    try:
        yield state
    finally:
        await state.manager.shutdown()
        await state.repository.close()
        await proxy.close()
        await simulator.close()


async def _acquire(world: World, session_id: str, creator: QueueSessionCreator | None = None) -> QueueSession:
    outcome = await (creator or world.creator()).create(
        CreationWorkItem(sequence=1, session_id=session_id)
    )
    assert outcome.kind is CreationOutcomeKind.SUCCESS, outcome.failure_code
    persisted = await world.repository.get(session_id)
    assert persisted is not None and persisted.queue_id is not None
    return persisted


def _new_ids(world: World, since: int) -> set[str | None]:
    return set(world.proxy.session_ids()[since:])


async def test_one_session_keeps_one_sticky_session_across_its_whole_lifecycle(
    world: World,
) -> None:
    def mark() -> int:
        return len(world.proxy.requests)

    # 1-2. Initial acquisition is proxied from its very first request, then parks.
    session = await _acquire(world, "lifecycle")
    sticky = session.proxy_session_id
    assert is_valid_proxy_session_id(sticky)
    assert world.proxy.session_ids() and _new_ids(world, 0) == {sticky}
    queue_id = session.queue_id
    assert world.manager.active_context_count == 0

    monitor = world.monitor()
    # 3-5. Two automatic monitoring checks, each in a fresh temporary context.
    for _ in range(2):
        before = mark()
        outcome = await monitor.check(await world.repository.get("lifecycle"))  # type: ignore[arg-type]
        assert outcome.success, outcome
        assert mark() > before and _new_ids(world, before) == {sticky}
        assert world.manager.active_context_count == 0

    # Pause / Resume affects scheduling only; the next check reuses the assignment.
    await world.repository.set_monitoring_paused(True)
    await world.repository.set_monitoring_paused(False)

    # 6. Refresh Now uses the same monitor and resolver as automatic monitoring.
    actions = OperatorActionManager(
        repository=world.repository,
        creator=world.creator(),
        monitor=monitor,
        state_store=world.state_store,
        target_adjustment=_Target(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
    )
    await actions.start()
    before = mark()
    await actions.request(OperatorActionKind.REFRESH, "lifecycle")
    assert await _wait(actions, "lifecycle") is OperatorActionStatus.SUCCESS
    assert _new_ids(world, before) == {sticky}

    # 7-8. Manual Open uses the headed pool with the same sticky session; Close.
    headed = _manager(world.backend, contexts=1)
    manual = ManualChromeSessionManager(
        repository=world.repository,
        browser_manager=headed,
        restorer=world.restorer(headed),
        monitor=monitor,
        capacity=1,
        lease_seconds=30,
    )
    before = mark()
    opened = await manual.open("lifecycle")
    assert opened.status.value == "OPENED"
    assert await manual.close_session("lifecycle")
    assert _new_ids(world, before) == {sticky}
    await manual.close()
    await headed.shutdown()
    await actions.close(timeout_seconds=10)

    # 9-10. Browser-process restart: a new process, the same persisted assignment.
    await world.manager.shutdown()
    world.manager = _manager(world.backend)
    await world.manager.start()
    before = mark()
    outcome = await world.monitor().check(await world.repository.get("lifecycle"))  # type: ignore[arg-type]
    assert outcome.success
    assert _new_ids(world, before) == {sticky}

    # 11-12. Application/repository restart: reload the row, re-read credentials from
    # the environment, reconstruct authentication, and reuse the same sticky session.
    await world.repository.close()
    world.repository = SQLiteSessionRepository(world.root / "routing.sqlite3")
    run = await world.repository.get_active_run()
    assert run is not None
    settings = Settings(
        _env_file=None,
        IPROYAL_PROXY_SERVER=world.proxy.url,
        IPROYAL_PROXY_USERNAME=FAKE_USERNAME,
        IPROYAL_PROXY_PASSWORD=FAKE_PASSWORD,
    )
    world.resolver = SessionProxyResolver.for_run(run, settings)
    before = mark()
    reloaded = await world.repository.get("lifecycle")
    assert reloaded is not None and reloaded.proxy_session_id == sticky
    outcome = await world.monitor().check(reloaded)
    assert outcome.success
    assert _new_ids(world, before) == {sticky}

    final = await world.repository.get("lifecycle")
    assert final is not None
    assert final.queue_id == queue_id and final.proxy_session_id == sticky
    assert set(world.proxy.session_ids()) == {sticky}
    assert {item.provider_session_id for item in world.resolved} == {sticky}
    assert all(request.username == FAKE_USERNAME for request in world.proxy.requests)
    world.assert_all_traffic_proxied()


async def test_concurrent_sessions_share_browsers_but_never_a_sticky_session(
    world: World,
) -> None:
    sessions = await asyncio.gather(*(_acquire(world, f"multi-{index}") for index in range(3)))
    ids = [session.proxy_session_id for session in sessions]

    assert len(set(ids)) == 3 and all(is_valid_proxy_session_id(item) for item in ids)
    assert set(world.proxy.session_ids()) == set(ids)
    # One shared browser process hosted three differently proxied contexts.
    capacity = await world.manager.capacity(repair=False)
    assert capacity.browser_processes == 1
    assert world.manager.active_context_count == 0

    monitor = world.monitor()
    for session in sessions:
        before = len(world.proxy.requests)
        assert (await monitor.check(session)).success
        assert _new_ids(world, before) == {session.proxy_session_id}
    world.assert_all_traffic_proxied()


async def test_add_and_replace_acquire_through_new_sticky_sessions(world: World) -> None:
    old = await _acquire(world, "old")
    actions = OperatorActionManager(
        repository=world.repository,
        creator=world.creator(),
        monitor=world.monitor(),
        state_store=world.state_store,
        target_adjustment=_Target(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
    )
    await actions.start()

    before = len(world.proxy.requests)
    await actions.request(OperatorActionKind.ADD)
    assert await _wait(actions, None) is OperatorActionStatus.SUCCESS
    added = [item for item in await world.repository.list() if item.session_id != "old"]
    assert len(added) == 1 and added[0].proxy_session_id != old.proxy_session_id
    assert _new_ids(world, before) == {added[0].proxy_session_id}

    before = len(world.proxy.requests)
    await actions.request(OperatorActionKind.REPLACE, "old")
    assert await _wait(actions, "old") is OperatorActionStatus.SUCCESS
    await actions.close(timeout_seconds=10)
    remaining = {item.session_id: item for item in await world.repository.list()}
    assert "old" not in remaining
    replacement = next(
        item for key, item in remaining.items() if key != added[0].session_id
    )
    assert replacement.proxy_session_id not in {old.proxy_session_id, added[0].proxy_session_id}
    assert _new_ids(world, before) == {replacement.proxy_session_id}
    world.assert_all_traffic_proxied()


async def test_failed_replace_keeps_the_old_assignment(world: World) -> None:
    old = await _acquire(world, "old")
    world.proxy.mode = MODE_UPSTREAM_DOWN
    actions = OperatorActionManager(
        repository=world.repository,
        creator=world.creator(),
        monitor=world.monitor(),
        state_store=world.state_store,
        target_adjustment=_Target(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
    )
    await actions.start()
    await actions.request(OperatorActionKind.REPLACE, "old")
    assert await _wait(actions, "old") is OperatorActionStatus.FAILED
    await actions.close(timeout_seconds=10)

    rows = await world.repository.list()
    assert [(row.session_id, row.queue_id, row.proxy_session_id) for row in rows] == [
        ("old", old.queue_id, old.proxy_session_id)
    ]


async def test_unidentified_manual_open_adopts_with_the_existing_assignment(
    world: World,
) -> None:
    await world.repository.create(
        QueueSession(
            session_id="no-id",
            queue_id=None,
            transfer_url="",
            mode=SessionMode.HYBRID,
            browser_backend=world.backend,
            proxy_session_id="NoId1234",
            status=QueueStatus.FAILED,
            state_path=world.state_store.path_for("no-id"),
            last_error="creation_failed",
        )
    )
    headed = _manager(world.backend, contexts=1)
    manual = ManualChromeSessionManager(
        repository=world.repository,
        browser_manager=headed,
        restorer=world.restorer(headed),
        monitor=world.monitor(),
        capacity=1,
        lease_seconds=30,
    )
    try:
        opened = await manual.open("no-id")
        assert opened.status.value == "OPENED"
        await asyncio.sleep(0.5)
        assert await manual.close_session("no-id")
    finally:
        await manual.close()
        await headed.shutdown()

    adopted = await world.repository.get("no-id")
    assert adopted is not None
    assert adopted.queue_id is not None
    assert adopted.proxy_session_id == "NoId1234"
    assert set(world.proxy.session_ids()) == {"NoId1234"}
    world.assert_all_traffic_proxied()


@pytest.mark.parametrize("mode", [MODE_REJECT_AUTH, PROXY_UNREACHABLE])
async def test_proxy_failure_fails_closed_without_any_direct_target_request(
    world: World, mode: str
) -> None:
    session = await _acquire(world, "victim")
    served = world.simulator.requests
    if mode == PROXY_UNREACHABLE:
        await world.proxy.close()  # the port refuses: Chromium ERR_PROXY_CONNECTION_FAILED
    else:
        world.proxy.mode = mode

    outcome = await world.monitor().check(session)
    acquisition = await world.creator().create(CreationWorkItem(sequence=2, session_id="new"))

    assert not outcome.success
    assert acquisition.kind is not CreationOutcomeKind.SUCCESS
    # Not one byte reached the target outside the proxy.
    assert world.simulator.requests == served
    persisted = await world.repository.get("victim")
    assert persisted is not None
    assert persisted.queue_id == session.queue_id
    assert persisted.proxy_session_id == session.proxy_session_id
    assert persisted.status is QueueStatus.CONNECTION_LOST
    assert persisted.last_error == {
        MODE_REJECT_AUTH: "restore:PROXY_AUTH_FAILED",
        PROXY_UNREACHABLE: "restore:PROXY_CONNECT_FAILED",
    }[mode], persisted.last_error
    failed = await world.repository.get("new")
    assert failed is not None and failed.last_error == {
        MODE_REJECT_AUTH: "proxy_auth_failed",
        PROXY_UNREACHABLE: "proxy_connect_failed",
    }[mode], failed.last_error
    assert world.manager.active_context_count == 0


async def test_missing_credentials_never_open_a_context(world: World) -> None:
    session = await _acquire(world, "creds")
    served = world.simulator.requests
    proxied = len(world.proxy.requests)
    no_credentials = SessionProxyResolver(_run(world.backend), None)
    restorer = QueueSessionRestorer(
        browser_manager=world.manager,
        repository=world.repository,
        state_store=world.state_store,
        storage_navigation_url=world.simulator.entry_url,
        browser_backend=world.backend,
        proxy_resolver=no_credentials,
    )

    result = await restorer.restore(session)

    assert result.failure is RestoreFailure.PROXY_CONFIG_MISSING
    assert world.simulator.requests == served
    assert len(world.proxy.requests) == proxied
    persisted = await world.repository.get("creds")
    assert persisted is not None and persisted.queue_id == session.queue_id
    assert persisted.last_error == "restore:PROXY_CONFIG_MISSING"


class _Target:
    def adjust_target(self, delta: int) -> None:
        del delta


async def _wait(manager: OperatorActionManager, session_id: str | None) -> OperatorActionStatus:
    for _ in range(600):
        action = manager.latest_add() if session_id is None else manager.for_session(session_id)
        if action is not None and action.status in {
            OperatorActionStatus.SUCCESS,
            OperatorActionStatus.FAILED,
        }:
            return action.status
        await asyncio.sleep(0.05)
    raise AssertionError("operator action did not finish")
