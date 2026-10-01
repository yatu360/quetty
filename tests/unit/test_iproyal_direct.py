"""Phase 9 Prompt 2: Direct Monitoring requests use the session's own sticky proxy.

Local fixtures only (httpx mock transports, the local queue simulator, and the local
authenticating proxy). Nothing here is Queue-it evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from prometheus_client import generate_latest

from queue_load_test.direct_monitor import (
    DirectCapability,
    DirectFallbackReason,
    DirectMonitoringHandler,
    DirectMonitorStateStore,
    DirectStatusChecker,
    accepted_evidence_scopes,
)
from queue_load_test.direct_replay import DirectStatusReplayClient, ReplayRecipe
from queue_load_test.harness.local_auth_proxy import LocalAuthProxy
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import (
    BrowserBackendName,
    ProxyProvider,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.observation_equivalence import (
    DirectField,
    DirectResponseParser,
    DirectResponseSchema,
)
from queue_load_test.proxy import IPRoyalCredentials, SessionProxyResolver
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import MonitoringRetryPolicy, PollingPolicy, QueueSessionMonitor
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
TARGET = "http://127.0.0.1:9/entry"
FAKE_USERNAME = "FAKEUSER_direct4Kp"
FAKE_PASSWORD = "FAKEPASS_direct8Wn"
FAKE_SERVER = "http://proxy.fake-iproyal.test:12321"


def _run() -> RunConfig:
    return RunConfig(
        run_id="direct-proxy",
        target_url=TARGET,
        requested_sessions=2,
        created_at=NOW,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_provider=ProxyProvider.IPROYAL,
        proxy_country="gb",
        proxy_lifetime="2h",
    )


def _resolver(server: str = FAKE_SERVER, *, credentials: bool = True) -> SessionProxyResolver:
    return SessionProxyResolver(
        _run(),
        IPRoyalCredentials(server=server, username=FAKE_USERNAME, base_password=FAKE_PASSWORD)
        if credentials
        else None,
        observer=METRICS,
    )


METRICS = PrometheusMetrics()


def _schema() -> DirectResponseSchema:
    fields = LocalQueueSimulator.status_schema()["fields"]
    assert isinstance(fields, dict)
    return DirectResponseSchema(
        {DirectField(name): tuple(path) for name, path in fields.items()}, "local_simulator"
    )


@dataclass
class _Restorer:
    """The proxied browser monitor stand-in; it can fail closed like the real one."""

    fail_closed: bool = False
    calls: list[str] = field(default_factory=list)

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        self.calls.append(session.session_id)
        if self.fail_closed:
            return SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=False,
                expected_queue_id=session.queue_id,
                failure=RestoreFailure.PROXY_CONFIG_MISSING,
            )
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(session_id=session.session_id, active_queue=True),
        )


@dataclass
class _Harness:
    repository: SQLiteSessionRepository
    state_store: FileSystemStateStore
    store: DirectMonitorStateStore
    restorer: _Restorer
    handler: DirectMonitoringHandler
    proxies: list[httpx.Proxy]
    requests: list[httpx.Request]


def _build(
    tmp_path: Path,
    *,
    resolver: SessionProxyResolver | None,
    restorer: _Restorer | None = None,
    use_factory: bool = True,
    factory_error: bool = False,
) -> _Harness:
    proxies: list[httpx.Proxy] = []
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if factory_error:
            raise httpx.ProxyError("proxy refused", request=request)
        queue_id = request.url.params.get("q", "")
        return httpx.Response(
            200,
            json={
                "queueId": queue_id,
                "preQueue": False,
                "activeQueue": True,
                "servicedSoon": False,
                "progress": 55,
                "usersAhead": 7,
            },
        )

    def factory(proxy: httpx.Proxy) -> httpx.AsyncBaseTransport:
        proxies.append(proxy)
        return httpx.MockTransport(handle)

    repository = SQLiteSessionRepository(tmp_path / "direct.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    store = DirectMonitorStateStore(tmp_path / "direct-monitor")
    restorer = restorer or _Restorer()
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=MonitoringRetryPolicy(max_attempts=1),
        clock=lambda: NOW,
    )
    checker = DirectStatusChecker(
        store=store,
        browser_state=state_store,
        parser=DirectResponseParser(_schema()),
        accepted_scopes=accepted_evidence_scopes(TARGET),
        clock=lambda: NOW,
        transport=httpx.MockTransport(handle) if not use_factory else None,
        proxy_resolver=resolver,
        proxied_transport_factory=factory if use_factory else None,
    )
    handler = DirectMonitoringHandler(browser_monitor=monitor, checker=checker, store=store)
    return _Harness(repository, state_store, store, restorer, handler, proxies, requests)


async def _add(
    harness: _Harness,
    session_id: str,
    proxy_session_id: str | None,
    *,
    status_url: str = "http://127.0.0.1:9/status",
) -> QueueSession:
    queue_id = f"queue-{session_id}"
    session = QueueSession(
        session_id=session_id,
        queue_id=queue_id,
        transfer_url=f"http://127.0.0.1:9/queue?q={queue_id}",
        mode=SessionMode.HYBRID,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_session_id=proxy_session_id,
        status=QueueStatus.ACTIVE_QUEUE,
        state_path=harness.state_store.path_for(session_id),
        next_check_at=NOW,
    )
    await harness.repository.create(session)
    await harness.state_store.save(
        session_id,
        {
            "cookies": [
                {"name": "queue_id", "value": queue_id, "domain": "127.0.0.1", "path": "/"}
            ],
            "origins": [],
        },
    )
    await harness.store.adopt_recipe(
        session_id=session_id,
        expected_queue_id=queue_id,
        recipe=ReplayRecipe(
            session_id=session_id,
            expected_queue_id=queue_id,
            source_scope="local_simulator",
            exchange_sequence=3,
            url=f"{status_url}?q={queue_id}",
            method="GET",
            headers={"accept": "application/json"},
            body=None,
            observed_identifiers={"queue_id": (queue_id,)},
            fingerprint="fp-1",
        ),
    )
    persisted = await harness.repository.get(session_id)
    assert persisted is not None
    return persisted


def _auth_session(proxy: httpx.Proxy) -> str:
    assert proxy.auth is not None
    username, password = proxy.auth
    assert username == FAKE_USERNAME
    assert password.startswith(f"{FAKE_PASSWORD}_country-gb_session-")
    assert password.endswith("_lifetime-2h")
    return password.split("_session-")[1].split("_")[0]


async def test_each_direct_session_uses_its_own_sticky_proxy(tmp_path: Path) -> None:
    harness = _build(tmp_path, resolver=_resolver())
    first = await _add(harness, "a", "Ab12Cd34")
    second = await _add(harness, "b", "X9y8Z7w6")

    assert (await harness.handler.check(first)).success
    assert (await harness.handler.check(second)).success
    assert (await harness.handler.check(first)).success

    assert [_auth_session(proxy) for proxy in harness.proxies] == [
        "Ab12Cd34",
        "X9y8Z7w6",
        "Ab12Cd34",
    ]
    assert all(str(proxy.url) == FAKE_SERVER for proxy in harness.proxies)
    assert harness.restorer.calls == []
    await harness.repository.close()


@pytest.mark.parametrize("credentials", [False, True])
async def test_unresolvable_proxy_falls_back_only_to_the_proxied_browser(
    tmp_path: Path, credentials: bool
) -> None:
    # Missing credentials, or a session whose assignment is missing in an IPRoyal run.
    harness = _build(tmp_path, resolver=_resolver(credentials=credentials))
    session = await _add(harness, "a", "Ab12Cd34" if not credentials else None)

    outcome = await harness.handler.check(session)

    assert harness.requests == [] and harness.proxies == []
    assert harness.restorer.calls == ["a"]
    assert outcome.success  # the stand-in proxied browser succeeded
    record = await harness.store.load("a")
    # A proxy fault is not a recipe fault: the capability is untouched.
    assert record is not None and record.capability is DirectCapability.DIRECT_CAPABLE
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.PROXY_UNAVAILABLE.value: 1
    }
    await harness.repository.close()


async def test_assigned_session_is_never_sent_direct_without_a_resolver(tmp_path: Path) -> None:
    harness = _build(tmp_path, resolver=None, use_factory=False)
    session = await _add(harness, "a", "Ab12Cd34")

    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.restorer.calls == ["a"]
    await harness.repository.close()


async def test_proxied_direct_failure_falls_back_without_degrading_the_recipe(
    tmp_path: Path,
) -> None:
    harness = _build(tmp_path, resolver=_resolver(), factory_error=True)
    session = await _add(harness, "a", "Ab12Cd34")

    outcome = await harness.handler.check(session)

    assert outcome.success and harness.restorer.calls == ["a"]
    assert [_auth_session(proxy) for proxy in harness.proxies] == ["Ab12Cd34"]
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.PROXY_FAILED.value: 1
    }
    record = await harness.store.load("a")
    assert record is not None and record.consecutive_failures == 0
    await harness.repository.close()


async def test_both_paths_failing_fails_closed(tmp_path: Path) -> None:
    harness = _build(
        tmp_path, resolver=_resolver(credentials=False), restorer=_Restorer(fail_closed=True)
    )
    session = await _add(harness, "a", "Ab12Cd34")

    outcome = await harness.handler.check(session)

    assert not outcome.success
    assert harness.requests == []
    persisted = await harness.repository.get("a")
    assert persisted is not None
    assert persisted.queue_id == "queue-a" and persisted.proxy_session_id == "Ab12Cd34"
    assert persisted.status is QueueStatus.CONNECTION_LOST
    await harness.repository.close()


async def test_real_http_direct_request_authenticates_through_the_local_proxy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    simulator = LocalQueueSimulator(status_enabled=True)
    proxy = LocalAuthProxy()
    await simulator.start()
    await proxy.start()
    try:
        harness = _build(tmp_path, resolver=_resolver(proxy.url), use_factory=False)
        session = await _add(harness, "a", "Ab12Cd34", status_url=simulator.status_url)
        simulator.stages["queue-a"] = "active"

        outcome = await harness.handler.check(session)

        assert outcome.success and harness.restorer.calls == []
        assert proxy.session_ids() == ["Ab12Cd34"]
        assert simulator.direct_status_requests["queue-a"] == 1
        assert simulator.requests == len(proxy.requests)
        for secret in (FAKE_USERNAME, FAKE_PASSWORD):
            assert secret not in caplog.text
        exposition = generate_latest(METRICS.registry).decode()
        assert 'proxied_attempts_total{purpose="direct"}' in exposition
        for secret in (FAKE_USERNAME, FAKE_PASSWORD, "Ab12Cd34"):
            assert secret not in exposition
        await harness.repository.close()
    finally:
        await proxy.close()
        await simulator.close()


def test_replay_client_refuses_a_transport_together_with_a_proxy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either a transport or a proxy"):
        DirectStatusReplayClient(
            recipe=ReplayRecipe(
                session_id="a",
                expected_queue_id="q",
                source_scope="local_simulator",
                exchange_sequence=1,
                url="http://127.0.0.1:9/status?q=q",
                method="GET",
                headers={},
                body=None,
                observed_identifiers={"queue_id": ("q",)},
                fingerprint="fp",
            ),
            cookies=(),
            state_store=DirectMonitorStateStore(tmp_path / "d").cookies,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            proxy=httpx.Proxy(FAKE_SERVER, auth=("u", "p")),
        )
