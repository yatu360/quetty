"""Phase 9 Prompt 3: post-check proxy-exit IP observation (deterministic, local only)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from queue_load_test.config import Settings
from queue_load_test.direct_monitor import (
    DirectMonitoringHandler,
    DirectMonitorStateStore,
    DirectStatusChecker,
    accepted_evidence_scopes,
)
from queue_load_test.direct_replay import ReplayRecipe
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
from queue_load_test.proxy import (
    IPRoyalCredentials,
    ProxyIpFailure,
    ProxyIpObserver,
    ProxyIpParseError,
    ProxyPurpose,
    SessionProxyResolver,
    parse_proxy_ip_response,
)
from queue_load_test.proxy.ip_tracker import PROXY_IP_CHANGED, ProxyIpOutcome, ProxyIpTracker
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcome,
    CreationOutcomeKind,
    CreationWorkItem,
    MonitoringRetryPolicy,
    PollingPolicy,
    QueueSessionMonitor,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult
from queue_load_test.web import create_app
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)
from queue_load_test.web.service import RuntimeCapacity

FAKE_SERVER = "http://proxy.fake-iproyal.test:12321"
FAKE_USERNAME = "FAKEUSER_ipobs5Rt"
FAKE_PASSWORD = "FAKEPASS_ipobs9Lm"
CREDENTIALS = IPRoyalCredentials(
    server=FAKE_SERVER, username=FAKE_USERNAME, base_password=FAKE_PASSWORD
)
ENDPOINT = "https://api.ipify.org/?format=json"
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def run(**overrides: Any) -> RunConfig:
    values: dict[str, Any] = {
        "run_id": "ip",
        "target_url": "http://127.0.0.1:9/entry",
        "requested_sessions": 1,
        "created_at": NOW,
        "browser_backend": BrowserBackendName.PATCHRIGHT,
        "proxy_provider": ProxyProvider.IPROYAL,
        "proxy_country": "gb",
        "proxy_lifetime": "2h",
    }
    values.update(overrides)
    return RunConfig(**values)


def make_session(session_id: str = "s", proxy_session_id: str | None = "Ab12Cd34") -> QueueSession:
    queue_id = f"queue-{session_id}"
    return QueueSession(
        session_id=session_id,
        queue_id=queue_id,
        transfer_url=f"http://127.0.0.1:9/queue?q={queue_id}",
        mode=SessionMode.HYBRID,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_session_id=proxy_session_id,
        status=QueueStatus.ACTIVE_QUEUE,
        state_path=Path(f".browser-state/{session_id}.json"),
        next_check_at=NOW,
    )


@dataclass
class FakeIpify:
    """An ipify stand-in reached only through the session's structured proxy."""

    ips: dict[str, str] = field(default_factory=dict)
    mode: str = "ok"
    lookups: list[str] = field(default_factory=list)
    proxies: list[httpx.Proxy] = field(default_factory=list)

    def factory(self, proxy: httpx.Proxy) -> httpx.AsyncBaseTransport:
        self.proxies.append(proxy)
        assert proxy.auth is not None
        sticky = proxy.auth[1].split("_session-")[1].split("_")[0]

        async def handle(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == ENDPOINT
            self.lookups.append(sticky)
            if self.mode == "timeout":
                await asyncio.sleep(5)
            if self.mode == "read_timeout":
                raise httpx.ReadTimeout("slow", request=request)
            if self.mode == "proxy_error":
                raise httpx.ProxyError("refused", request=request)
            if self.mode == "http_500":
                return httpx.Response(500)
            if self.mode == "redirect":
                return httpx.Response(302, headers={"location": "https://elsewhere.test/"})
            if self.mode == "malformed":
                return httpx.Response(200, content=b"{not json")
            if self.mode == "invalid":
                return httpx.Response(200, json={"ip": "999.1.1.1"})
            if self.mode == "too_large":
                return httpx.Response(200, content=b"x" * 5_000)
            return httpx.Response(200, json={"ip": self.ips.get(sticky, "84.1.1.1")})

        return httpx.MockTransport(handle)


def observer(ipify: FakeIpify, *, timeout: float = 0.5) -> ProxyIpObserver:
    return ProxyIpObserver(
        endpoint=ENDPOINT,
        timeout_seconds=timeout,
        connect_timeout_seconds=timeout,
        transport_factory=ipify.factory,
    )


def tracker(
    repository: SQLiteSessionRepository,
    ipify: FakeIpify,
    *,
    credentials: IPRoyalCredentials | None = CREDENTIALS,
    run_config: RunConfig | None = None,
    metrics: PrometheusMetrics | None = None,
    clock: Any = None,
) -> ProxyIpTracker:
    return ProxyIpTracker(
        resolver=SessionProxyResolver(run_config or run(), credentials, observer=metrics),
        observer=observer(ipify),
        store=repository,
        observability=metrics,
        clock=clock,
    )


# --- parser ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"ip": "84.71.214.123"}', "84.71.214.123"),
        (b'{"ip": "2001:0db8:0000:0000:0000:0000:0000:0001"}', "2001:db8::1"),
        (b'{"ip": "::1"}', "::1"),
    ],
)
def test_parser_accepts_ipv4_and_ipv6(body: bytes, expected: str) -> None:
    assert parse_proxy_ip_response(body) == expected


@pytest.mark.parametrize(
    ("body", "failure"),
    [
        (b"{not json", ProxyIpFailure.PROXY_IP_MALFORMED),
        (b"\xff\xfe", ProxyIpFailure.PROXY_IP_MALFORMED),
        (b'["84.1.1.1"]', ProxyIpFailure.PROXY_IP_MALFORMED),
        (b'{"address": "84.1.1.1"}', ProxyIpFailure.PROXY_IP_MALFORMED),
        (b'{"ip": 84}', ProxyIpFailure.PROXY_IP_MALFORMED),
        (b'{"ip": "999.1.1.1"}', ProxyIpFailure.PROXY_IP_INVALID),
        (b'{"ip": "example.com"}', ProxyIpFailure.PROXY_IP_INVALID),
        (b'{"ip": " 84.1.1.1"}', ProxyIpFailure.PROXY_IP_INVALID),
        (b'{"ip": ""}', ProxyIpFailure.PROXY_IP_INVALID),
    ],
)
def test_parser_rejects_malformed_and_invalid(body: bytes, failure: ProxyIpFailure) -> None:
    with pytest.raises(ProxyIpParseError) as raised:
        parse_proxy_ip_response(body)
    assert raised.value.failure is failure


# --- observer -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "failure"),
    [
        ("timeout", ProxyIpFailure.PROXY_IP_TIMEOUT),
        ("read_timeout", ProxyIpFailure.PROXY_IP_TIMEOUT),
        ("proxy_error", ProxyIpFailure.PROXY_IP_PROXY_FAILED),
        ("http_500", ProxyIpFailure.PROXY_IP_HTTP_STATUS),
        ("redirect", ProxyIpFailure.PROXY_IP_HTTP_STATUS),
        ("malformed", ProxyIpFailure.PROXY_IP_MALFORMED),
        ("invalid", ProxyIpFailure.PROXY_IP_INVALID),
        ("too_large", ProxyIpFailure.PROXY_IP_TOO_LARGE),
    ],
)
async def test_observer_failures_are_bounded_and_sanitized(
    mode: str, failure: ProxyIpFailure
) -> None:
    ipify = FakeIpify(mode=mode)
    resolved = SessionProxyResolver(run(), CREDENTIALS).resolve(make_session())
    assert resolved is not None

    started = asyncio.get_running_loop().time()
    result = await observer(ipify, timeout=0.3).observe(resolved)

    assert result.ip is None and result.failure is failure
    assert asyncio.get_running_loop().time() - started < 2
    assert FAKE_PASSWORD not in repr(result)


async def test_observer_routes_through_the_sessions_own_sticky_proxy() -> None:
    ipify = FakeIpify(ips={"Ab12Cd34": "84.1.1.1", "X9y8Z7w6": "95.2.2.2"})
    resolver = SessionProxyResolver(run(), CREDENTIALS)
    a = resolver.resolve(make_session("a", "Ab12Cd34"))
    b = resolver.resolve(make_session("b", "X9y8Z7w6"))
    assert a is not None and b is not None

    first = await observer(ipify).observe(a)
    second = await observer(ipify).observe(b)

    assert (first.ip, second.ip) == ("84.1.1.1", "95.2.2.2")
    assert ipify.lookups == ["Ab12Cd34", "X9y8Z7w6"]
    assert all(str(proxy.url) == FAKE_SERVER for proxy in ipify.proxies)
    assert all(proxy.auth and proxy.auth[0] == FAKE_USERNAME for proxy in ipify.proxies)


async def test_real_observer_client_uses_the_proxy_and_ignores_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a test transport, the client must carry exactly the session proxy."""

    seen: dict[str, Any] = {}
    original = httpx.AsyncClient.__init__

    def spy(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://direct-egress.invalid:1")
    resolved = SessionProxyResolver(run(), CREDENTIALS).resolve(make_session())
    assert resolved is not None

    # The fake provider host does not resolve, so the lookup fails fast and closed.
    result = await ProxyIpObserver(endpoint=ENDPOINT, timeout_seconds=2).observe(resolved)

    assert result.ip is None
    assert isinstance(seen["proxy"], httpx.Proxy)
    assert str(seen["proxy"].url) == FAKE_SERVER
    assert seen["trust_env"] is False and seen["follow_redirects"] is False


# --- tracker + repository -------------------------------------------------------------


async def test_baseline_unchanged_changed_and_failure_retention(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    repository = SQLiteSessionRepository(tmp_path / "ip.sqlite3")
    await repository.create(make_session())
    ipify = FakeIpify(ips={"Ab12Cd34": "84.1.1.1"})
    times = iter(NOW + timedelta(minutes=minute) for minute in range(10))
    metrics = PrometheusMetrics()
    track = tracker(repository, ipify, metrics=metrics, clock=lambda: next(times))
    current = await repository.get("s")
    assert current is not None and current.proxy_ip is None

    assert await track.observe_after_check(current) is ProxyIpOutcome.BASELINE
    baseline = await repository.get("s")
    assert baseline is not None
    assert (baseline.proxy_ip, baseline.proxy_ip_checked_at) == ("84.1.1.1", NOW)
    assert baseline.proxy_ip_changed_count == 0

    assert await track.observe_after_check(current) is ProxyIpOutcome.UNCHANGED
    unchanged = await repository.get("s")
    assert unchanged is not None
    assert unchanged.proxy_ip_checked_at == NOW + timedelta(minutes=1)

    ipify.mode = "timeout"
    assert await track.observe_after_check(current) is ProxyIpOutcome.FAILED
    retained = await repository.get("s")
    assert retained is not None
    assert retained.proxy_ip == "84.1.1.1"
    assert retained.proxy_ip_checked_at == NOW + timedelta(minutes=1)

    ipify.mode = "ok"
    ipify.ips["Ab12Cd34"] = "95.2.2.2"
    assert await track.observe_after_check(current) is ProxyIpOutcome.CHANGED
    changed = await repository.get("s")
    assert changed is not None
    assert changed.proxy_ip == "95.2.2.2"
    assert changed.proxy_ip_changed_count == 1
    assert changed.proxy_ip_changed_at == changed.proxy_ip_checked_at
    # The routing and journey identities are untouched; nothing rotated or replaced.
    assert changed.proxy_session_id == "Ab12Cd34" and changed.queue_id == "queue-s"
    assert [row.session_id for row in await repository.list()] == ["s"]
    assert set(ipify.lookups) == {"Ab12Cd34"}

    contexts = [getattr(record, "observability_context", {}) for record in caplog.records]
    assert {"session_id": "s", "error_type": PROXY_IP_CHANGED} in contexts
    rendered = caplog.text + repr(contexts)
    for secret in ("84.1.1.1", "95.2.2.2", FAKE_USERNAME, FAKE_PASSWORD):
        assert secret not in rendered
    exposition = generate_latest(metrics.registry).decode()
    assert 'proxy_ip_observations_total{result="changed"} 1.0' in exposition
    assert 'proxy_ip_observations_total{result="PROXY_IP_TIMEOUT"} 1.0' in exposition
    assert 'proxied_attempts_total{purpose="ip_observation"} 4.0' in exposition
    for secret in ("84.1.1.1", "95.2.2.2", "Ab12Cd34", FAKE_PASSWORD):
        assert secret not in exposition
    await repository.close()


async def test_no_lookup_without_the_sessions_own_proxy(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "closed.sqlite3")
    await repository.create(make_session())
    await repository.create(make_session("plain", None))
    ipify = FakeIpify()

    missing_credentials = tracker(repository, ipify, credentials=None)
    disabled = tracker(
        repository,
        ipify,
        run_config=run(proxy_provider=ProxyProvider.NONE, proxy_country=None, proxy_lifetime=None),
    )
    missing_assignment = tracker(repository, ipify)

    session = await repository.get("s")
    plain = await repository.get("plain")
    assert session is not None and plain is not None
    assert await missing_credentials.observe_after_check(session) is ProxyIpOutcome.FAILED
    assert await disabled.observe_after_check(plain) is ProxyIpOutcome.SKIPPED
    assert await missing_assignment.observe_after_check(plain) is ProxyIpOutcome.FAILED

    assert ipify.lookups == [] and ipify.proxies == []
    assert (await repository.get("s")).proxy_ip is None  # type: ignore[union-attr]
    await repository.close()


async def test_deleted_row_and_store_failure_never_raise(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "gone.sqlite3")
    ipify = FakeIpify()
    track = tracker(repository, ipify)

    assert await track.observe_after_check(make_session()) is ProxyIpOutcome.SKIPPED

    class Broken:
        async def record_proxy_ip(self, *_: Any, **__: Any) -> None:
            raise sqlite3.OperationalError("locked")

    broken = ProxyIpTracker(
        resolver=SessionProxyResolver(run(), CREDENTIALS),
        observer=observer(ipify),
        store=cast(Any, Broken()),
    )
    assert await broken.observe_after_check(make_session()) is ProxyIpOutcome.FAILED
    await repository.close()


async def test_ordinary_updates_never_overwrite_proxy_ip_metadata(tmp_path: Path) -> None:
    database = tmp_path / "stale.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create(make_session())
    stale = await repository.get("s")
    assert stale is not None
    await repository.record_proxy_ip("s", ip="84.1.1.1", observed_at=NOW)

    stale.status = QueueStatus.PARKED
    stale.last_checked_at = NOW + timedelta(minutes=5)
    await repository.update(stale)  # a stale in-memory row with proxy_ip=None

    persisted = await repository.get("s")
    assert persisted is not None
    assert (persisted.proxy_ip, persisted.proxy_ip_checked_at) == ("84.1.1.1", NOW)
    assert persisted.last_checked_at == NOW + timedelta(minutes=5)
    assert "84.1.1.1" not in repr(persisted)
    await repository.close()

    reopened = SQLiteSessionRepository(database)
    survived = await reopened.get("s")
    assert survived is not None and survived.proxy_ip == "84.1.1.1"
    await reopened.reset_all()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM queue_sessions WHERE proxy_ip IS NOT NULL"
        ).fetchone() == (0,)
    await reopened.close()


async def test_legacy_database_gains_null_ip_columns(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE queue_sessions (
                session_id TEXT PRIMARY KEY, queue_id TEXT UNIQUE,
                transfer_url TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                state_path TEXT NOT NULL, created_at TEXT NOT NULL,
                last_checked_at TEXT, next_check_at TEXT, attempt_count INTEGER NOT NULL,
                last_error TEXT, worker_id TEXT, lease_until TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO queue_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old", "q-old", "https://q.test/t", "HYBRID", "PARKED", "s.json",
             NOW.isoformat(), None, NOW.isoformat(), 0, None, None, None),
        )
    repository = SQLiteSessionRepository(database)
    legacy = await repository.get("old")
    assert legacy is not None
    assert legacy.proxy_ip is None and legacy.proxy_ip_checked_at is None
    assert legacy.proxy_ip_changed_count == 0
    await repository.close()


# --- hook points ----------------------------------------------------------------------


@dataclass
class Restorer:
    """Like the real restorer, records Queue Checked on the session it restores."""

    success: bool = True
    calls: int = 0
    clock: Any = None

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        self.calls += 1
        session.last_checked_at = self.clock() if self.clock else datetime.now(UTC)
        if not self.success:
            return SessionRestoreResult(
                method=RestoreMethod.TRANSFER,
                success=False,
                expected_queue_id=session.queue_id,
                failure=RestoreFailure.NAVIGATION_FAILED,
            )
        return SessionRestoreResult(
            method=RestoreMethod.TRANSFER,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(
                session_id=session.session_id, active_queue=True, progress_percentage=43
            ),
        )


def monitor_for(
    repository: SQLiteSessionRepository,
    track: ProxyIpTracker | None,
    restorer: Restorer,
    clock: Any = None,
) -> QueueSessionMonitor:
    return QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=MonitoringRetryPolicy(max_attempts=1),
        clock=clock,
        proxy_ip_tracker=track,
    )


async def test_queue_checked_and_ip_checked_are_independent(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "independent.sqlite3")
    await repository.create(make_session())
    ipify = FakeIpify(ips={"Ab12Cd34": "84.1.1.1"})
    queue_times = iter(NOW + timedelta(minutes=minute) for minute in range(0, 20, 2))
    ip_times = iter(NOW + timedelta(minutes=minute, seconds=1) for minute in range(0, 20, 2))
    track = tracker(repository, ipify, clock=lambda: next(ip_times))
    monitor = monitor_for(repository, track, Restorer(clock=lambda: next(queue_times)))

    # Case 1: queue and IP both succeed; both advance.
    outcome = await monitor.check(await repository.get("s"))  # type: ignore[arg-type]
    first = await repository.get("s")
    assert outcome.success and first is not None
    assert first.proxy_ip == "84.1.1.1"
    assert first.proxy_ip_checked_at == NOW + timedelta(seconds=1)

    # Case 2: queue succeeds, ipify fails; Queue Checked advances, IP Checked does not.
    first_queue_checked = first.last_checked_at
    first_ip_checked = first.proxy_ip_checked_at
    ipify.mode = "http_500"
    outcome = await monitor.check(first)
    second = await repository.get("s")
    assert outcome.success and second is not None
    assert second.status is QueueStatus.ACTIVE_QUEUE
    assert second.last_checked_at is not None and first_queue_checked is not None
    assert second.last_checked_at > first_queue_checked
    assert second.proxy_ip == "84.1.1.1"
    assert second.proxy_ip_checked_at == first_ip_checked
    assert second.last_error is None
    assert len(ipify.lookups) == 2  # exactly one lookup per check
    await repository.close()


async def test_failed_queue_check_makes_no_ip_lookup(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "failed.sqlite3")
    await repository.create(make_session())
    ipify = FakeIpify()
    monitor = monitor_for(repository, tracker(repository, ipify), Restorer(success=False))

    outcome = await monitor.check(await repository.get("s"))  # type: ignore[arg-type]

    assert not outcome.success and ipify.lookups == []
    await repository.close()


def _schema() -> DirectResponseSchema:
    fields = LocalQueueSimulator.status_schema()["fields"]
    assert isinstance(fields, dict)
    return DirectResponseSchema(
        {DirectField(name): tuple(path) for name, path in fields.items()}, "local_simulator"
    )


@pytest.mark.parametrize("direct_succeeds", [True, False])
async def test_direct_check_and_its_fallback_each_cause_exactly_one_lookup(
    tmp_path: Path, direct_succeeds: bool
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "direct.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    store = DirectMonitorStateStore(tmp_path / "direct-monitor")
    ipify = FakeIpify(ips={"Ab12Cd34": "84.1.1.1"})
    resolver = SessionProxyResolver(run(), CREDENTIALS)
    direct_proxies: list[httpx.Proxy] = []

    def direct_transport(proxy: httpx.Proxy) -> httpx.AsyncBaseTransport:
        direct_proxies.append(proxy)

        def handle(request: httpx.Request) -> httpx.Response:
            if not direct_succeeds:
                return httpx.Response(500)
            queue_id = request.url.params.get("q", "")
            return httpx.Response(
                200,
                json={"queueId": queue_id, "preQueue": False, "activeQueue": True,
                      "servicedSoon": False, "progress": 55, "usersAhead": 7},
            )

        return httpx.MockTransport(handle)

    restorer = Restorer()
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=MonitoringRetryPolicy(max_attempts=1),
        proxy_ip_tracker=ProxyIpTracker(
            resolver=resolver, observer=observer(ipify), store=repository
        ),
    )
    handler = DirectMonitoringHandler(
        browser_monitor=monitor,
        checker=DirectStatusChecker(
            store=store,
            browser_state=state_store,
            parser=DirectResponseParser(_schema()),
            accepted_scopes=accepted_evidence_scopes("http://127.0.0.1:9/entry"),
            proxy_resolver=resolver,
            proxied_transport_factory=direct_transport,
        ),
        store=store,
    )
    session = make_session()
    await repository.create(session)
    await state_store.save(
        "s",
        {"cookies": [{"name": "queue_id", "value": "queue-s", "domain": "127.0.0.1",
                      "path": "/"}], "origins": []},
    )
    await store.adopt_recipe(
        session_id="s",
        expected_queue_id="queue-s",
        recipe=ReplayRecipe(
            session_id="s", expected_queue_id="queue-s", source_scope="local_simulator",
            exchange_sequence=3, url="http://127.0.0.1:9/status?q=queue-s", method="GET",
            headers={"accept": "application/json"}, body=None,
            observed_identifiers={"queue_id": ("queue-s",)}, fingerprint="fp",
        ),
    )

    outcome = await handler.check(await repository.get("s"))  # type: ignore[arg-type]

    assert outcome.success
    assert restorer.calls == (0 if direct_succeeds else 1)
    assert ipify.lookups == ["Ab12Cd34"]
    assert len(direct_proxies) == 1
    assert direct_proxies[0].auth is not None and "_session-Ab12Cd34_" in direct_proxies[0].auth[1]
    persisted = await repository.get("s")
    assert persisted is not None and persisted.proxy_ip == "84.1.1.1"
    await repository.close()


class _Creator:
    def __init__(self, repository: SQLiteSessionRepository, track: ProxyIpTracker) -> None:
        self.repository = repository
        self.track = track

    async def create(self, item: CreationWorkItem) -> CreationOutcome:
        session = make_session(item.session_id, "Nw12Ad34")
        await self.repository.create(session)
        await self.track.observe_after_check(session)
        return CreationOutcome(
            kind=CreationOutcomeKind.SUCCESS, attempts=1, temporary_failures=0,
            duration_seconds=0, session=session,
        )


class _Target:
    def adjust_target(self, delta: int) -> None:
        del delta


async def test_refresh_now_causes_one_lookup_even_while_paused(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "refresh.sqlite3")
    await repository.initialize()
    await repository.create(make_session())
    await repository.set_monitoring_paused(True)
    ipify = FakeIpify()
    track = tracker(repository, ipify)
    manager = OperatorActionManager(
        repository=repository,
        creator=_Creator(repository, track),
        monitor=monitor_for(repository, track, Restorer()),
        state_store=FileSystemStateStore(tmp_path / "state"),
        target_adjustment=_Target(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
    )
    await manager.start()
    await manager.request(OperatorActionKind.REFRESH, "s")
    for _ in range(200):
        action = manager.for_session("s")
        if action is not None and action.status is OperatorActionStatus.SUCCESS:
            break
        await asyncio.sleep(0.01)
    await manager.close()

    assert ipify.lookups == ["Ab12Cd34"]
    persisted = await repository.get("s")
    assert persisted is not None and persisted.proxy_ip is not None
    assert persisted.proxy_session_id == "Ab12Cd34"
    await repository.close()


async def test_real_creator_records_a_baseline_only_after_success(tmp_path: Path) -> None:
    from contextlib import asynccontextmanager

    from queue_load_test.scheduler import CreationRetryPolicy, QueueSessionCreator
    from queue_load_test.transfer import TransferExtractionResult

    class Page:
        url = "http://127.0.0.1:9/queue"

        def __init__(self, status: int) -> None:
            self.status = status

        async def goto(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(status=self.status)

        def locator(self, _selector: str) -> Any:
            async def inner_text(**_: Any) -> str:
                return "Queue-it waiting room"

            return SimpleNamespace(first=SimpleNamespace(inner_text=inner_text))

    class Context:
        def __init__(self, status: int) -> None:
            self.status = status

        async def new_page(self) -> Page:
            return Page(self.status)

        async def storage_state(self) -> dict[str, object]:
            return {"cookies": [], "origins": []}

    class Manager:
        status = 200

        def report_navigation(self, *_: Any, **__: Any) -> None:
            return None

        @asynccontextmanager
        async def context(self, **_: Any) -> Any:
            yield Context(self.status)

    class Live:
        async def extract(self, _: Any, *, session_id: str) -> QueueProgress:
            return QueueProgress(session_id=session_id, active_queue=True)

    class Transfer:
        async def extract(self, _: Any, **__: Any) -> TransferExtractionResult:
            return TransferExtractionResult(
                transfer_url="http://127.0.0.1:9/queue?q=queue-new", observed_queue_id="queue-new"
            )

    repository = SQLiteSessionRepository(tmp_path / "creator.sqlite3")
    ipify = FakeIpify()
    resolver = SessionProxyResolver(run(), CREDENTIALS)
    manager = Manager()
    creator = QueueSessionCreator(
        browser_manager=cast(Any, manager),
        repository=repository,
        state_store=FileSystemStateStore(tmp_path / "state"),
        staging_url="http://127.0.0.1:9/entry",
        state_directory=tmp_path / "state",
        mode=SessionMode.TRANSFER_ONLY,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=resolver,
        proxy_ip_tracker=ProxyIpTracker(resolver=resolver, observer=observer(ipify), store=repository),
        live_extractor=cast(Any, Live()),
        transfer_extractor_factory=lambda _: Transfer(),
        retry_policy=CreationRetryPolicy(max_attempts=1),
    )

    ok = await creator.create(CreationWorkItem(sequence=1, session_id="new"))
    manager.status = 403
    failed = await creator.create(CreationWorkItem(sequence=2, session_id="bad"))

    assert ok.kind is CreationOutcomeKind.SUCCESS
    assert failed.kind is CreationOutcomeKind.PERMANENT_FAILURE
    created = await repository.get("new")
    assert created is not None and created.proxy_ip is not None
    assert ipify.lookups == [created.proxy_session_id]
    assert (await repository.get("bad")).proxy_ip is None  # type: ignore[union-attr]
    await repository.close()


# --- dashboard ------------------------------------------------------------------------


class _Runtime:
    async def start_run(self, run_config: RunConfig) -> None:
        del run_config

    async def capacity(self) -> RuntimeCapacity:
        return RuntimeCapacity(active_contexts=0, maximum_active_contexts=25, chrome_processes=0)

    def error(self) -> str | None:
        return None

    def stop_accepting(self) -> None:
        return None

    def latest_add_action(self) -> None:
        return None

    def session_action(self, session_id: str) -> None:
        del session_id

    async def close(self) -> None:
        return None


async def test_dashboard_shows_proxy_ip_and_separate_timestamps_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "dash.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(run())
    for index in range(55):
        await repository.create(make_session(f"s{index:02d}", f"Px{index:02d}abc"[:8]))
    await repository.record_proxy_ip("s00", ip="84.71.214.123", observed_at=NOW)
    await repository.record_proxy_ip("s00", ip="95.144.23.44", observed_at=NOW)
    await repository.close()

    async def no_network(*_: Any, **__: Any) -> Any:
        raise AssertionError("dashboard rendering must not perform any HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    app = create_app(
        settings=Settings(_env_file=None, DATABASE_URL=f"sqlite:///{database}",
                          CHROME_PROCESS_COUNT=1, MAX_CONTEXTS_PER_BROWSER=25,
                          MAX_ACTIVE_CONTEXTS=25),
        repository=SQLiteSessionRepository(database),
        runtime=cast(Any, _Runtime()),
    )
    with TestClient(app) as client:
        page_one = client.get("/partials/sessions?page=1").text
        page_two = client.get("/partials/sessions?page=2").text
        for _ in range(5):
            client.get("/partials/sessions?page=1")

    for header in ("Proxy IP", "Queue Checked", "IP Checked"):
        assert f"<th>{header}</th>" in page_one
    assert "95.144.23.44" in page_one and "changed ×1" in page_one
    assert "PROXY_IP_CHANGED" in page_one
    assert "84.71.214.123" not in page_one  # only the latest IP is kept
    assert ">s54<" in page_two and ">s00<" not in page_two and ">s00<" in page_one
    assert page_one.count('<td class="mono proxy-ip">—</td>') >= 1
    for secret in (FAKE_USERNAME, FAKE_PASSWORD, "Px00abc"):
        assert secret not in page_one


def test_proxy_purpose_includes_ip_observation() -> None:
    assert ProxyPurpose.IP_OBSERVATION.value == "ip_observation"


def test_endpoint_setting_rejects_credentials() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, PROXY_IP_ENDPOINT="https://u:p@api.ipify.org/")
    assert Settings(_env_file=None).proxy_ip_endpoint == ENDPOINT
