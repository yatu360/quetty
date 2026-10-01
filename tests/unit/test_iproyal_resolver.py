"""Phase 9 Prompt 2: the central per-session proxy resolver and its runtime wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from prometheus_client import generate_latest

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.models import (
    BrowserBackendName,
    MonitoringStrategy,
    ProxyProvider,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.proxy import (
    IPRoyalCredentials,
    ProxyAuthWatch,
    ProxyDiagnostics,
    ProxyFailure,
    ProxyPurpose,
    ProxyResolutionError,
    SessionProxyResolver,
    classify_proxy_error,
    resolve_proxy_for_session,
    resolve_session_proxy,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    CreationOutcomeKind,
    CreationRetryPolicy,
    CreationWorkItem,
    QueueSessionCreator,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer, RestoreFailure, RestoreMethod
from queue_load_test.web import service as service_module
from queue_load_test.web.service import ApplicationRunRuntime

FAKE_SERVER = "http://proxy.fake-iproyal.test:12321"
FAKE_USERNAME = "FAKEUSER_resolver2Hd"
FAKE_PASSWORD = "FAKEPASS_resolver6Tc"
CREDENTIALS = IPRoyalCredentials(
    server=FAKE_SERVER, username=FAKE_USERNAME, base_password=FAKE_PASSWORD
)
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def run(**overrides: Any) -> RunConfig:
    values: dict[str, Any] = {
        "run_id": "resolver",
        "target_url": "https://staging.example.test/",
        "requested_sessions": 1,
        "created_at": NOW,
        "browser_backend": BrowserBackendName.PATCHRIGHT,
        "proxy_provider": ProxyProvider.IPROYAL,
        "proxy_country": "gb",
        "proxy_lifetime": "2h",
    }
    values.update(overrides)
    return RunConfig(**values)


def session(proxy_session_id: str | None = "Ab12Cd34", **overrides: Any) -> QueueSession:
    values: dict[str, Any] = {
        "session_id": "s",
        "queue_id": "queue-s",
        "transfer_url": "https://queue.example.test/?q=queue-s",
        "mode": SessionMode.HYBRID,
        "browser_backend": BrowserBackendName.PATCHRIGHT,
        "proxy_session_id": proxy_session_id,
        "status": QueueStatus.PARKED,
        "state_path": Path(".browser-state/s.json"),
    }
    values.update(overrides)
    return QueueSession(**values)


def test_resolver_builds_structured_proxy_from_persisted_identity() -> None:
    resolved = resolve_proxy_for_session(run(), session(), CREDENTIALS)

    assert resolved is not None
    assert resolved.browser_proxy() == {
        "server": FAKE_SERVER,
        "username": FAKE_USERNAME,
        "password": f"{FAKE_PASSWORD}_country-gb_session-Ab12Cd34_lifetime-2h",
    }
    assert resolved.diagnostics == ProxyDiagnostics(
        provider=ProxyProvider.IPROYAL,
        provider_session_id="Ab12Cd34",
        country="gb",
        lifetime="2h",
        purpose=ProxyPurpose.BROWSER,
    )
    text = repr(resolved) + str(resolved) + repr([resolved])
    assert "Ab12Cd34" in text  # the non-secret reference is safe diagnostic metadata
    for secret in (FAKE_USERNAME, FAKE_PASSWORD, FAKE_SERVER):
        assert secret not in text


def test_resolution_is_deterministic_and_never_rotates() -> None:
    resolver = SessionProxyResolver(run(), CREDENTIALS)
    first = resolver.resolve(session())
    second = resolver.resolve(session())

    assert first is not None and second is not None
    assert first.browser_proxy() == second.browser_proxy()


def test_proxy_disabled_runs_resolve_to_none() -> None:
    disabled = run(proxy_provider=ProxyProvider.NONE, proxy_country=None, proxy_lifetime=None)

    assert resolve_proxy_for_session(disabled, session(None), None) is None
    assert resolve_session_proxy(None, session(None)) is None


@pytest.mark.parametrize(
    ("run_overrides", "session_id", "credentials", "failure"),
    [
        ({}, "Ab12Cd34", None, ProxyFailure.PROXY_CONFIG_MISSING),
        ({"proxy_lifetime": None}, "Ab12Cd34", CREDENTIALS, ProxyFailure.PROXY_CONFIG_MISSING),
        ({"proxy_country": "g_b"}, "Ab12Cd34", CREDENTIALS, ProxyFailure.PROXY_CONFIG_MISSING),
        ({}, None, CREDENTIALS, ProxyFailure.PROXY_ASSIGNMENT_MISSING),
        ({}, "bad-id!!", CREDENTIALS, ProxyFailure.PROXY_ASSIGNMENT_INVALID),
        (
            {"browser_backend": BrowserBackendName.CAMOUFOX},
            "Ab12Cd34",
            CREDENTIALS,
            ProxyFailure.PROXY_UNSUPPORTED_BACKEND,
        ),
        (
            {"proxy_provider": ProxyProvider.NONE, "proxy_country": None, "proxy_lifetime": None},
            "Ab12Cd34",
            None,
            ProxyFailure.PROXY_ASSIGNMENT_INVALID,
        ),
    ],
)
def test_unresolvable_sessions_fail_closed_with_sanitized_classification(
    run_overrides: dict[str, Any],
    session_id: str | None,
    credentials: IPRoyalCredentials | None,
    failure: ProxyFailure,
) -> None:
    with pytest.raises(ProxyResolutionError) as raised:
        resolve_proxy_for_session(run(**run_overrides), session(session_id), credentials)

    assert raised.value.failure is failure
    assert str(raised.value) == failure.value
    assert raised.value.__cause__ is None


def test_assigned_session_without_a_resolver_is_never_unproxied() -> None:
    with pytest.raises(ProxyResolutionError) as raised:
        resolve_session_proxy(None, session())

    assert raised.value.failure is ProxyFailure.PROXY_CONFIG_MISSING


def test_for_run_reads_environment_credentials_and_fails_closed_without_them() -> None:
    settings = Settings(
        _env_file=None,
        IPROYAL_PROXY_SERVER=FAKE_SERVER,
        IPROYAL_PROXY_USERNAME=FAKE_USERNAME,
        IPROYAL_PROXY_PASSWORD=FAKE_PASSWORD,
    )
    assert SessionProxyResolver.for_run(run(), settings).resolve(session()) is not None

    missing = SessionProxyResolver.for_run(run(), Settings(_env_file=None))
    with pytest.raises(ProxyResolutionError) as raised:
        missing.resolve(session())
    assert raised.value.failure is ProxyFailure.PROXY_CONFIG_MISSING
    assert FAKE_PASSWORD not in repr(SessionProxyResolver.for_run(run(), settings))


@pytest.mark.parametrize(
    ("message", "failure"),
    [
        ("net::ERR_PROXY_CONNECTION_FAILED at https://x", ProxyFailure.PROXY_CONNECT_FAILED),
        ("net::ERR_TUNNEL_CONNECTION_FAILED at https://x", ProxyFailure.PROXY_CONNECT_FAILED),
        ("net::ERR_INVALID_AUTH_CREDENTIALS", ProxyFailure.PROXY_AUTH_FAILED),
        ("net::ERR_NAME_NOT_RESOLVED", None),
    ],
)
def test_proxy_error_classification(message: str, failure: ProxyFailure | None) -> None:
    assert classify_proxy_error(RuntimeError(message)) is failure


def test_auth_watch_turns_a_generic_failure_into_proxy_auth_failed() -> None:
    listeners: dict[str, Any] = {}

    class Page:
        def on(self, event: str, callback: Any) -> None:
            listeners[event] = callback

    watch = ProxyAuthWatch.attach(Page())
    listeners["response"](type("R", (), {"status": 407})())
    error = RuntimeError("net::ERR_HTTP_RESPONSE_CODE_FAILURE")

    assert classify_proxy_error(error) is None
    assert classify_proxy_error(error, watch) is ProxyFailure.PROXY_AUTH_FAILED


def test_metrics_are_low_cardinality_and_secret_free() -> None:
    metrics = PrometheusMetrics()
    resolver = SessionProxyResolver(run(), CREDENTIALS, observer=metrics)
    metrics.set_proxy_provider("iproyal")
    resolver.resolve(session())
    resolver.record_attempt()
    resolver.record_attempt(purpose=ProxyPurpose.DIRECT)
    with pytest.raises(ProxyResolutionError):
        resolver.resolve(session(None))

    text = generate_latest(metrics.registry).decode()
    assert 'proxy_provider_info{provider="iproyal"} 1.0' in text
    assert 'proxied_attempts_total{purpose="browser"} 1.0' in text
    assert 'proxied_attempts_total{purpose="direct"} 1.0' in text
    assert (
        'proxy_failures_total{purpose="browser",reason="PROXY_ASSIGNMENT_MISSING"} 1.0' in text
    )
    for forbidden in ("Ab12Cd34", FAKE_USERNAME, FAKE_PASSWORD, "queue-s", 'session_id="'):
        assert forbidden not in text


def test_proxied_creator_cannot_be_built_without_a_resolver(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a session proxy resolver"):
        QueueSessionCreator(
            browser_manager=cast(BrowserManager, object()),
            repository=cast(Any, object()),
            state_store=FileSystemStateStore(tmp_path),
            staging_url="https://staging.test",
            state_directory=tmp_path,
            mode=SessionMode.HYBRID,
            proxy_provider=ProxyProvider.IPROYAL,
        )


class _CountingManager:
    def __init__(self) -> None:
        self.contexts = 0

    @asynccontextmanager
    async def context(self, **_: Any) -> AsyncIterator[Any]:
        self.contexts += 1
        raise AssertionError("a context must never be opened")
        yield  # pragma: no cover

    async def create_context(self, **_: Any) -> Any:
        self.contexts += 1
        raise AssertionError("a context must never be opened")


async def test_creation_resolution_failure_opens_no_context(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "c.sqlite3")
    manager = _CountingManager()
    creator = QueueSessionCreator(
        browser_manager=cast(BrowserManager, manager),
        repository=repository,
        state_store=FileSystemStateStore(tmp_path / "state"),
        staging_url="https://staging.test",
        state_directory=tmp_path / "state",
        mode=SessionMode.HYBRID,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=SessionProxyResolver(run(), None),
        retry_policy=CreationRetryPolicy(max_attempts=3),
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="new"))

    assert outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE
    assert outcome.failure_code == "proxy_config_missing"
    assert manager.contexts == 0
    failed = await repository.get("new")
    assert failed is not None and failed.proxy_session_id is not None
    await repository.close()


@pytest.mark.parametrize("entry", ["restore", "restore_open", "transfer", "storage"])
async def test_every_restore_entry_fails_closed_before_any_context(
    tmp_path: Path, entry: str
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "r.sqlite3")
    await repository.create(session())
    manager = _CountingManager()
    restorer = QueueSessionRestorer(
        browser_manager=cast(BrowserManager, manager),
        repository=repository,
        state_store=FileSystemStateStore(tmp_path / "state"),
        storage_navigation_url="https://staging.example.test/",
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=SessionProxyResolver(run(), None),
    )
    current = await repository.get("s")
    assert current is not None

    if entry == "restore":
        result = await restorer.restore(current)
    elif entry == "restore_open":
        result = (await restorer.restore_open(current)).result
    else:
        method = RestoreMethod.TRANSFER if entry == "transfer" else RestoreMethod.STORAGE_STATE
        result = await restorer.restore_with_method(current, method)

    assert result.failure is RestoreFailure.PROXY_CONFIG_MISSING
    assert manager.contexts == 0
    persisted = await repository.get("s")
    assert persisted is not None
    assert persisted.queue_id == "queue-s" and persisted.proxy_session_id == "Ab12Cd34"
    await repository.close()


async def test_unidentified_open_fails_closed_without_a_context(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "u.sqlite3")
    manager = _CountingManager()
    restorer = QueueSessionRestorer(
        browser_manager=cast(BrowserManager, manager),
        repository=repository,
        state_store=FileSystemStateStore(tmp_path / "state"),
        storage_navigation_url="https://staging.example.test/",
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=SessionProxyResolver(run(), CREDENTIALS),
    )

    opened = await restorer.restore_open(session(None, queue_id=None, transfer_url=""))

    assert opened.owned_context is None
    assert opened.result.failure is RestoreFailure.PROXY_ASSIGNMENT_MISSING
    assert manager.contexts == 0
    await repository.close()


async def test_start_run_wires_one_resolver_into_every_traffic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def no_run(self: object) -> None:
        return None

    monkeypatch.setattr(service_module.ApplicationRuntime, "run", no_run)
    database = tmp_path / "wiring.sqlite3"
    repository = SQLiteSessionRepository(database)
    settings = Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite:///{database}",
        STATE_DIRECTORY=str(tmp_path / "state"),
        CHROME_PROCESS_COUNT=1,
        MAX_CONTEXTS_PER_BROWSER=5,
        MAX_ACTIVE_CONTEXTS=5,
        IPROYAL_PROXY_SERVER=FAKE_SERVER,
        IPROYAL_PROXY_USERNAME=FAKE_USERNAME,
        IPROYAL_PROXY_PASSWORD=FAKE_PASSWORD,
    )
    runtime = ApplicationRunRuntime(settings=settings, repository=repository)
    await runtime.start_run(run(monitoring_strategy=MonitoringStrategy.DIRECT))
    try:
        actions: Any = runtime._operator_actions
        manual: Any = runtime._manual_sessions
        direct: Any = runtime._direct_handler
        resolver = actions._creator._proxy_resolver
        assert isinstance(resolver, SessionProxyResolver) and resolver.enabled
        assert manual._restorer._proxy_resolver is resolver
        assert manual._monitor._restorer._proxy_resolver is resolver
        assert direct._checker._proxy_resolver is resolver
        assert direct._browser_monitor._restorer._proxy_resolver is resolver
        # Refresh Now uses the run's automatic monitor (Direct, with proxied fallback).
        assert actions._monitor is direct
        assert FAKE_PASSWORD not in caplog.text and FAKE_USERNAME not in caplog.text
    finally:
        await runtime.close()
        await repository.close()


async def test_proxy_disabled_run_wires_no_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_run(self: object) -> None:
        return None

    monkeypatch.setattr(service_module.ApplicationRuntime, "run", no_run)
    database = tmp_path / "plain.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = ApplicationRunRuntime(
        settings=Settings(
            _env_file=None,
            DATABASE_URL=f"sqlite:///{database}",
            CHROME_PROCESS_COUNT=1,
            MAX_CONTEXTS_PER_BROWSER=5,
            MAX_ACTIVE_CONTEXTS=5,
        ),
        repository=repository,
    )
    await runtime.start_run(
        run(proxy_provider=ProxyProvider.NONE, proxy_country=None, proxy_lifetime=None)
    )
    try:
        actions: Any = runtime._operator_actions
        assert actions._creator._proxy_resolver.enabled is False
        assert actions._creator._proxy_provider is ProxyProvider.NONE
    finally:
        await runtime.close()
        await repository.close()
