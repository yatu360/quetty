"""Phase 9 Prompt 1: IPRoyal configuration, provenance, and sticky-session assignment."""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.models import (
    BrowserBackendName,
    ProxyProvider,
    QueueProgress,
    QueueSession,
    QueueStatus,
    RunConfig,
    SessionMode,
)
from queue_load_test.proxy import (
    IPRoyalCredentials,
    IPRoyalProxyConfigurationError,
    SessionProxyResolver,
    construct_effective_password,
    generate_proxy_session_id,
    is_valid_proxy_session_id,
    validate_proxy_session_id,
)
from queue_load_test.repository import (
    ProxySessionAssignmentError,
    ProxySessionIdConflictError,
    SQLiteSessionRepository,
)
from queue_load_test.scheduler import (
    CreationOutcomeKind,
    CreationRetryPolicy,
    CreationWorkItem,
    QueueSessionCreator,
    SessionCreationController,
)
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker
from queue_load_test.transfer import TransferExtractionResult
from queue_load_test.web import create_app
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)
from queue_load_test.web.service import ApplicationRunRuntime, RuntimeCapacity

# Distinctive fake credentials: none of these may leak anywhere observable.
FAKE_SERVER = "http://proxy.fake-iproyal.test:12321"
FAKE_USERNAME = "FAKEUSER_zq81Lk"
FAKE_PASSWORD = "FAKEPASS_x7Rm2Qv9"
SECRETS = (FAKE_USERNAME, FAKE_PASSWORD)
FAKE_CREDENTIALS = IPRoyalCredentials(
    server=FAKE_SERVER, username=FAKE_USERNAME, base_password=FAKE_PASSWORD
)
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def proxy_settings(database: Path | None = None, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "IPROYAL_PROXY_ENABLED": True,
        "IPROYAL_PROXY_SERVER": FAKE_SERVER,
        "IPROYAL_PROXY_USERNAME": FAKE_USERNAME,
        "IPROYAL_PROXY_PASSWORD": FAKE_PASSWORD,
        "IPROYAL_PROXY_COUNTRY": "gb",
        "IPROYAL_PROXY_LIFETIME": "2h",
        "CHROME_PROCESS_COUNT": 1,
        "MAX_CONTEXTS_PER_BROWSER": 25,
        "MAX_ACTIVE_CONTEXTS": 25,
    }
    if database is not None:
        values["DATABASE_URL"] = f"sqlite:///{database}"
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def iproyal_run(**overrides: Any) -> RunConfig:
    values: dict[str, Any] = {
        "run_id": "iproyal-run",
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


def proxied_session(session_id: str, proxy_session_id: str | None, **overrides: Any) -> QueueSession:
    values: dict[str, Any] = {
        "session_id": session_id,
        "queue_id": f"queue-{session_id}",
        "transfer_url": f"https://queue.test/{session_id}",
        "mode": SessionMode.TRANSFER_ONLY,
        "status": QueueStatus.PARKED,
        "state_path": Path(f".browser-state/{session_id}.json"),
        "proxy_session_id": proxy_session_id,
        "created_at": NOW,
    }
    values.update(overrides)
    return QueueSession(**values)


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


# --- Configuration -------------------------------------------------------------------


def test_production_config_parses_typed_values_without_exposing_credentials() -> None:
    settings = proxy_settings()

    assert settings.iproyal_proxy_enabled is True
    assert settings.iproyal_proxy_server == FAKE_SERVER
    assert settings.iproyal_proxy_country == "gb"
    assert settings.iproyal_proxy_lifetime == "2h"
    assert_no_secrets(repr(settings))
    assert_no_secrets(str(settings.model_dump()))
    credentials = settings.require_iproyal_credentials()
    assert credentials.username == FAKE_USERNAME
    assert credentials.base_password == FAKE_PASSWORD
    assert_no_secrets(repr(credentials))
    assert_no_secrets(str(credentials))


def test_proxy_disabled_by_default_and_lifetime_is_not_altered() -> None:
    assert Settings(_env_file=None).iproyal_proxy_enabled is False
    assert proxy_settings(IPROYAL_PROXY_LIFETIME="90m").iproyal_proxy_lifetime == "90m"


@pytest.mark.parametrize(
    "missing",
    [
        "IPROYAL_PROXY_SERVER",
        "IPROYAL_PROXY_USERNAME",
        "IPROYAL_PROXY_PASSWORD",
        "IPROYAL_PROXY_COUNTRY",
        "IPROYAL_PROXY_LIFETIME",
    ],
)
def test_enabled_proxy_requires_every_field(missing: str) -> None:
    with pytest.raises(ValidationError) as raised:
        proxy_settings(**{missing: ""})

    assert missing in str(raised.value)
    assert_no_secrets(str(raised.value))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("IPROYAL_PROXY_SERVER", f"http://{FAKE_USERNAME}:{FAKE_PASSWORD}@proxy.test:1"),
        ("IPROYAL_PROXY_SERVER", "http://proxy.test"),
        ("IPROYAL_PROXY_COUNTRY", "gb_session-x"),
        ("IPROYAL_PROXY_LIFETIME", "2h_session-x"),
        ("IPROYAL_PROXY_LIFETIME", "0h"),
    ],
)
def test_malformed_proxy_values_are_rejected_without_echoing_secrets(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        proxy_settings(**{field: value})

    assert_no_secrets(str(raised.value))


def test_env_example_placeholders_do_not_break_disabled_startup() -> None:
    settings = Settings(
        _env_file=None,
        IPROYAL_PROXY_ENABLED=False,
        IPROYAL_PROXY_SERVER="http://YOUR_HOSTNAME:YOUR_PORT",
        IPROYAL_PROXY_USERNAME="YOUR_USERNAME",
        IPROYAL_PROXY_PASSWORD="YOUR_BASE_PASSWORD",
    )

    assert settings.iproyal_proxy_enabled is False
    with pytest.raises(IPRoyalProxyConfigurationError, match="explicit port"):
        settings.require_iproyal_credentials()


def test_spike_gate_never_enables_production_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_IPROYAL_PROXY_SPIKE", "1")
    monkeypatch.setenv("IPROYAL_PROXY_SERVER", FAKE_SERVER)
    monkeypatch.setenv("IPROYAL_PROXY_USERNAME", FAKE_USERNAME)
    monkeypatch.setenv("IPROYAL_PROXY_PASSWORD", FAKE_PASSWORD)
    monkeypatch.delenv("IPROYAL_PROXY_ENABLED", raising=False)

    assert Settings(_env_file=None).iproyal_proxy_enabled is False


def test_missing_credentials_fail_closed_with_sanitized_message() -> None:
    settings = Settings(_env_file=None, IPROYAL_PROXY_USERNAME=FAKE_USERNAME)

    with pytest.raises(IPRoyalProxyConfigurationError) as raised:
        settings.require_iproyal_credentials()

    assert "IPROYAL_PROXY_PASSWORD" in str(raised.value)
    assert_no_secrets(str(raised.value))


# --- Proxy identity helpers ----------------------------------------------------------


def test_generated_session_ids_are_eight_alphanumeric_and_distinct() -> None:
    generated = {generate_proxy_session_id() for _ in range(2_000)}

    assert len(generated) == 2_000
    assert all(len(value) == 8 and value.isalnum() and value.isascii() for value in generated)
    assert all(is_valid_proxy_session_id(value) for value in generated)


@pytest.mark.parametrize(
    "value", ["", "Ab12Cd3", "Ab12Cd345", "Ab12-d34", "Ab12_d34", "Ab12Cd3é", "Ab12Cd3\n"]
)
def test_malformed_session_ids_are_rejected(value: str) -> None:
    assert not is_valid_proxy_session_id(value)
    with pytest.raises(IPRoyalProxyConfigurationError):
        validate_proxy_session_id(value)


def test_effective_password_format_and_validation() -> None:
    assert (
        construct_effective_password(
            "basePassword", country="gb", session_id="Ab12Cd34", lifetime="2h"
        )
        == "basePassword_country-gb_session-Ab12Cd34_lifetime-2h"
    )
    with pytest.raises(IPRoyalProxyConfigurationError) as raised:
        construct_effective_password(
            FAKE_PASSWORD, country="gb", session_id="bad", lifetime="2h"
        )
    assert_no_secrets(str(raised.value))
    with pytest.raises(IPRoyalProxyConfigurationError):
        construct_effective_password("", country="gb", session_id="Ab12Cd34", lifetime="2h")


def test_credentials_repr_is_redacted() -> None:
    credentials = IPRoyalCredentials(
        server=FAKE_SERVER, username=FAKE_USERNAME, base_password=FAKE_PASSWORD
    )

    assert_no_secrets(repr(credentials))
    assert_no_secrets(f"{credentials!s} {[credentials]}")


# --- Persistence ---------------------------------------------------------------------


async def test_legacy_run_and_sessions_migrate_to_proxy_disabled(tmp_path: Path) -> None:
    database = tmp_path / "legacy-proxy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE run_config (
                run_id TEXT PRIMARY KEY, target_url TEXT NOT NULL,
                requested_sessions INTEGER NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, current_run INTEGER NOT NULL UNIQUE
            );
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
            "INSERT INTO run_config VALUES (?, ?, ?, ?, ?, 1)",
            ("legacy", "https://staging.example.test/", 1, NOW.isoformat(), "ACTIVE"),
        )
        connection.execute(
            "INSERT INTO queue_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-session", "legacy-queue", "https://queue.test/t", "HYBRID", "PARKED",
                ".browser-state/legacy-session.json", NOW.isoformat(), None,
                NOW.isoformat(), 0, None, None, None,
            ),
        )

    repository = SQLiteSessionRepository(database)
    run = await repository.get_active_run()
    session = await repository.get("legacy-session")
    await repository.close()

    assert run is not None
    assert run.proxy_provider is ProxyProvider.NONE
    assert run.proxy_country is None and run.proxy_lifetime is None
    assert session is not None and session.proxy_session_id is None


async def test_run_provenance_and_assignments_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "restart.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(iproyal_run())
    await repository.create(proxied_session("a", "Ab12Cd34"))
    await repository.create(proxied_session("b", "X9y8Z7w6"))
    await repository.close()

    reopened = SQLiteSessionRepository(database)
    run = await reopened.get_active_run()
    a = await reopened.get("a")
    b = await reopened.get("b")
    assert run is not None
    assert (run.proxy_provider, run.proxy_country, run.proxy_lifetime) == (
        ProxyProvider.IPROYAL,
        "gb",
        "2h",
    )
    assert a is not None and a.proxy_session_id == "Ab12Cd34"
    assert b is not None and b.proxy_session_id == "X9y8Z7w6"

    # Ordinary lifecycle updates (monitoring, restore, retry) keep the assignment.
    a.status = QueueStatus.ACTIVE_QUEUE
    await reopened.update(a, QueueProgress(session_id="a", active_queue=True))
    assert (await reopened.get("a")).proxy_session_id == "Ab12Cd34"  # type: ignore[union-attr]
    await reopened.close()


async def test_persisted_assignment_is_unique_and_immutable(tmp_path: Path) -> None:
    database = tmp_path / "immutable.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create(proxied_session("a", "Ab12Cd34"))

    with pytest.raises(ProxySessionIdConflictError):
        await repository.create(proxied_session("b", "Ab12Cd34"))
    with pytest.raises(ProxySessionIdConflictError):
        await repository.create_many([(proxied_session("c", "Ab12Cd34"), None)])

    changed = await repository.get("a")
    assert changed is not None
    changed.proxy_session_id = "Zz99Yy88"
    with pytest.raises(ProxySessionAssignmentError):
        await repository.update(changed)
    await repository.close()

    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE queue_sessions SET proxy_session_id = 'Zz99Yy88' WHERE session_id = 'a'"
        )
    reopened = SQLiteSessionRepository(database)
    assert (await reopened.get("a")).proxy_session_id == "Ab12Cd34"  # type: ignore[union-attr]
    await reopened.close()


async def test_reset_removes_provenance_and_assignments(tmp_path: Path) -> None:
    database = tmp_path / "reset.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(iproyal_run())
    await repository.create(proxied_session("a", "Ab12Cd34"))

    await repository.reset_all()

    assert await repository.get_active_run() is None
    assert await repository.list() == []
    await repository.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM queue_sessions WHERE proxy_session_id IS NOT NULL"
        ).fetchone() == (0,)


# --- Creation, Add, Replace, Delete ----------------------------------------------------


class _Response:
    status = 200


class _Page:
    def __init__(self, manager: "_BrowserManager") -> None:
        self._manager = manager
        self.url = "https://queue.staging.test/journey"

    async def goto(self, *_: object, **__: object) -> _Response:
        await self._manager.on_navigation()
        if self._manager.fail:
            response = _Response()
            response.status = 403
            return response
        return _Response()

    def locator(self, _selector: str) -> SimpleNamespace:
        restricted = self._manager.restricted_navigations >= len(
            self._manager.assignments_at_navigation
        )

        async def inner_text(**_: object) -> str:
            if restricted:
                return "We are sorry, your access has been restricted"
            return "Queue-it waiting room"

        return SimpleNamespace(first=SimpleNamespace(inner_text=inner_text))


class _Context:
    def __init__(self, manager: "_BrowserManager") -> None:
        self._manager = manager

    async def new_page(self) -> _Page:
        return _Page(self._manager)

    async def storage_state(self) -> dict[str, object]:
        return {"cookies": [], "origins": []}


class _BrowserManager:
    """Records what SQLite held at the moment each external navigation started."""

    def __init__(
        self,
        repository: SQLiteSessionRepository,
        *,
        fail: bool = False,
        restricted_navigations: int = 0,
    ) -> None:
        self.repository = repository
        self.fail = fail
        # The first N navigations render the pre-Queue access-restriction page.
        self.restricted_navigations = restricted_navigations
        self.assignments_at_navigation: list[dict[str, str | None]] = []
        self.context_proxies: list[object] = []

    async def on_navigation(self) -> None:
        sessions = await self.repository.list()
        self.assignments_at_navigation.append(
            {session.session_id: session.proxy_session_id for session in sessions}
        )

    def report_navigation(self, context: object, *, responsive: bool) -> None:
        del context, responsive

    @asynccontextmanager
    async def context(self, **options: Any) -> AsyncIterator[_Context]:
        self.context_proxies.append(options.get("proxy"))
        yield _Context(self)


class _Live:
    async def extract(self, _: object, *, session_id: str) -> QueueProgress:
        return QueueProgress(session_id=session_id, active_queue=True)


class _Transfer:
    counter = 0

    async def extract(
        self, _: object, *, expected_queue_id: str | None = None
    ) -> TransferExtractionResult:
        del expected_queue_id
        _Transfer.counter += 1
        queue_id = f"queue-new-{_Transfer.counter}"
        return TransferExtractionResult(
            transfer_url=f"https://queue.staging.test/journey?q={queue_id}",
            observed_queue_id=queue_id,
        )


def creator_for(
    tmp_path: Path,
    repository: SQLiteSessionRepository,
    browser_manager: _BrowserManager,
    *,
    provider: ProxyProvider = ProxyProvider.IPROYAL,
    factory: Iterator[str] | None = None,
) -> QueueSessionCreator:
    state_store = FileSystemStateStore(tmp_path / "state")
    kwargs: dict[str, Any] = {}
    if factory is not None:
        kwargs["proxy_session_id_factory"] = lambda: next(factory)
    return QueueSessionCreator(
        browser_manager=cast(BrowserManager, browser_manager),
        repository=repository,
        state_store=state_store,
        staging_url="https://staging.test",
        state_directory=state_store.directory,
        mode=SessionMode.TRANSFER_ONLY,
        browser_backend=BrowserBackendName.PATCHRIGHT,
        proxy_resolver=(
            SessionProxyResolver(iproyal_run(), FAKE_CREDENTIALS)
            if provider is ProxyProvider.IPROYAL
            else None
        ),
        live_extractor=cast(Any, _Live()),
        transfer_extractor_factory=lambda _: _Transfer(),
        retry_policy=CreationRetryPolicy(max_attempts=1),
        **kwargs,
    )


async def test_assignment_is_persisted_before_first_navigation(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "before-nav.sqlite3")
    browser = _BrowserManager(repository)
    creator = creator_for(tmp_path, repository, browser)

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="new"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    persisted = await repository.get("new")
    assert persisted is not None and persisted.status is QueueStatus.PARKED
    assert is_valid_proxy_session_id(persisted.proxy_session_id)
    assert browser.assignments_at_navigation == [{"new": persisted.proxy_session_id}]
    await repository.close()


async def test_proxy_disabled_creation_is_unchanged(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "disabled.sqlite3")
    browser = _BrowserManager(repository)
    creator = creator_for(tmp_path, repository, browser, provider=ProxyProvider.NONE)

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="plain"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    assert browser.assignments_at_navigation == [{}]
    assert (await repository.get("plain")).proxy_session_id is None  # type: ignore[union-attr]
    await repository.close()


async def test_collision_regenerates_before_any_navigation(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "collision.sqlite3")
    await repository.create(proxied_session("existing", "Ab12Cd34"))
    browser = _BrowserManager(repository)
    creator = creator_for(
        tmp_path, repository, browser, factory=iter(["Ab12Cd34", "Ab12Cd34", "Qw12Er34"])
    )

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="new"))

    assert outcome.kind is CreationOutcomeKind.SUCCESS
    assert (await repository.get("existing")).proxy_session_id == "Ab12Cd34"  # type: ignore[union-attr]
    assert (await repository.get("new")).proxy_session_id == "Qw12Er34"  # type: ignore[union-attr]
    assert len(browser.assignments_at_navigation) == 1
    await repository.close()


async def test_failed_creation_keeps_its_own_assignment(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "failed.sqlite3")
    browser = _BrowserManager(repository, fail=True)
    creator = creator_for(tmp_path, repository, browser, factory=iter(["Fa11Ed00"]))

    outcome = await creator.create(CreationWorkItem(sequence=1, session_id="failed"))

    assert outcome.kind is CreationOutcomeKind.PERMANENT_FAILURE
    failed = await repository.get("failed")
    assert failed is not None and failed.status is QueueStatus.FAILED
    assert failed.proxy_session_id == "Fa11Ed00"
    await repository.close()


class _Target:
    def adjust_target(self, delta: int) -> None:
        del delta


class _Monitor:
    async def check(self, session: QueueSession) -> object:
        return SimpleNamespace(success=True)


async def operator_manager(
    tmp_path: Path, repository: SQLiteSessionRepository, creator: QueueSessionCreator
) -> OperatorActionManager:
    manager = OperatorActionManager(
        repository=repository,
        creator=creator,
        monitor=_Monitor(),  # type: ignore[arg-type]
        state_store=FileSystemStateStore(tmp_path / "state"),
        target_adjustment=_Target(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
    )
    await manager.start()
    return manager


async def wait_for(manager: OperatorActionManager, session_id: str | None) -> OperatorActionStatus:
    for _ in range(200):
        action = manager.latest_add() if session_id is None else manager.for_session(session_id)
        if action is not None and action.status in {
            OperatorActionStatus.SUCCESS,
            OperatorActionStatus.FAILED,
        }:
            return action.status
        await asyncio.sleep(0.01)
    raise AssertionError("operator action did not finish")


async def test_restricted_attempt_is_replaced_with_a_fresh_context_and_assignment(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "restricted.sqlite3")
    browser = _BrowserManager(repository, restricted_navigations=2)
    creator = creator_for(tmp_path, repository, browser)
    controller = SessionCreationController(
        repository=repository,
        handler=creator,
        target_queue_ids=1,
        worker_count=1,
    )

    metrics = await controller.run()

    assert metrics.successful_unique_ids == 1
    assert metrics.access_restricted == 2
    # One context per work item; a restricted item is never retried on its own proxy.
    assert len(browser.context_proxies) == 3
    sessions = await repository.list()
    failed = [s for s in sessions if s.status is QueueStatus.FAILED]
    parked = [s for s in sessions if s.status is QueueStatus.PARKED]
    assert len(failed) == 2 and len(parked) == 1
    assert all(s.last_error == "access_restricted_before_queue" for s in failed)
    assignments = [s.proxy_session_id for s in sessions]
    assert all(is_valid_proxy_session_id(a) for a in assignments)
    assert len(set(assignments)) == 3
    # Each context was routed through its own work item's sticky session.
    passwords = [cast(dict[str, str], proxy)["password"] for proxy in browser.context_proxies]
    routed = {a for a in assignments if a and any(f"_session-{a}_" in p for p in passwords)}
    assert routed == set(assignments)
    assert len(set(passwords)) == 3
    await repository.close()


async def test_add_allocates_a_new_distinct_assignment(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "add.sqlite3")
    await repository.initialize()
    await repository.create(proxied_session("old", "Ab12Cd34"))
    browser = _BrowserManager(repository)
    manager = await operator_manager(tmp_path, repository, creator_for(tmp_path, repository, browser))

    await manager.request(OperatorActionKind.ADD)
    assert await wait_for(manager, None) is OperatorActionStatus.SUCCESS
    await manager.close()

    sessions = {session.session_id: session for session in await repository.list()}
    added = [session for session_id, session in sessions.items() if session_id != "old"]
    assert len(added) == 1
    assert is_valid_proxy_session_id(added[0].proxy_session_id)
    assert added[0].proxy_session_id != "Ab12Cd34"
    assert sessions["old"].proxy_session_id == "Ab12Cd34"
    assert browser.assignments_at_navigation[0][added[0].session_id] == added[0].proxy_session_id
    await repository.close()


async def test_replace_allocates_new_assignment_and_failed_replace_preserves_old(
    tmp_path: Path,
) -> None:
    repository = SQLiteSessionRepository(tmp_path / "replace.sqlite3")
    await repository.initialize()
    await repository.create(proxied_session("old", "Ab12Cd34"))

    failing = await operator_manager(
        tmp_path,
        repository,
        creator_for(tmp_path, repository, _BrowserManager(repository, fail=True)),
    )
    await failing.request(OperatorActionKind.REPLACE, "old")
    assert await wait_for(failing, "old") is OperatorActionStatus.FAILED
    await failing.close()
    remaining = await repository.list()
    assert [(item.session_id, item.queue_id, item.proxy_session_id) for item in remaining] == [
        ("old", "queue-old", "Ab12Cd34")
    ]

    manager = await operator_manager(
        tmp_path, repository, creator_for(tmp_path, repository, _BrowserManager(repository))
    )
    await manager.request(OperatorActionKind.REPLACE, "old")
    assert await wait_for(manager, "old") is OperatorActionStatus.SUCCESS
    await manager.close()

    replaced = await repository.list()
    assert len(replaced) == 1
    assert replaced[0].session_id != "old"
    assert is_valid_proxy_session_id(replaced[0].proxy_session_id)
    assert replaced[0].proxy_session_id != "Ab12Cd34"
    await repository.close()


async def test_delete_removes_only_that_assignment(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "delete.sqlite3")
    await repository.initialize()
    await repository.create(proxied_session("a", "Ab12Cd34"))
    await repository.create(proxied_session("b", "X9y8Z7w6"))
    manager = await operator_manager(
        tmp_path, repository, creator_for(tmp_path, repository, _BrowserManager(repository))
    )

    await manager.request(OperatorActionKind.DELETE, "a")
    assert await wait_for(manager, "a") is OperatorActionStatus.SUCCESS
    await manager.close()

    assert await repository.get("a") is None
    assert (await repository.get("b")).proxy_session_id == "X9y8Z7w6"  # type: ignore[union-attr]
    await repository.close()


# --- Consistency checker -------------------------------------------------------------


class _StubRepository:
    def __init__(self, sessions: list[QueueSession], run: RunConfig | None) -> None:
        self._sessions = sessions
        self._run = run

    async def list(self) -> list[QueueSession]:
        return list(self._sessions)

    async def get_active_run(self) -> RunConfig | None:
        return self._run


async def test_consistency_checker_reports_bad_assignments_without_repair(tmp_path: Path) -> None:
    sessions = [
        proxied_session("good", "Ab12Cd34"),
        proxied_session("missing", None),
        proxied_session("malformed", "bad-id"),
        proxied_session("dup-a", "X9y8Z7w6"),
        proxied_session("dup-b", "X9y8Z7w6"),
    ]
    snapshot = [(item.session_id, item.proxy_session_id) for item in sessions]
    store = FileSystemStateStore(tmp_path / "state")

    report = await StateConsistencyChecker(
        cast(Any, _StubRepository(sessions, iproyal_run())), store
    ).check()

    assert report.count("missing_proxy_session_id") == 1
    assert report.count("malformed_proxy_session_id") == 1
    assert report.count("duplicate_proxy_session_id") == 1
    assert [(item.session_id, item.proxy_session_id) for item in sessions] == snapshot

    disabled = await StateConsistencyChecker(
        cast(Any, _StubRepository([proxied_session("x", "Ab12Cd34")], iproyal_run(
            proxy_provider=ProxyProvider.NONE, proxy_country=None, proxy_lifetime=None
        ))),
        store,
    ).check()
    assert disabled.count("unexpected_proxy_session_id") == 1

    incomplete = await StateConsistencyChecker(
        cast(Any, _StubRepository([], iproyal_run(proxy_lifetime=None))), store
    ).check()
    assert incomplete.count("run_proxy_provenance_incomplete") == 1


async def test_consistency_checker_never_writes_assignments(tmp_path: Path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "check.sqlite3")
    await repository.create_run(iproyal_run())
    await repository.create(proxied_session("missing", None))

    report = await StateConsistencyChecker(
        repository, FileSystemStateStore(tmp_path / "state")
    ).check()

    assert report.count("missing_proxy_session_id") == 1
    assert (await repository.get("missing")).proxy_session_id is None  # type: ignore[union-attr]
    await repository.close()


# --- Startup, setup, and secrecy -----------------------------------------------------


async def test_restart_without_credentials_fails_closed_before_browser_work(
    tmp_path: Path,
) -> None:
    database = tmp_path / "fail-closed.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = ApplicationRunRuntime(
        settings=Settings(_env_file=None, DATABASE_URL=f"sqlite:///{database}"),
        repository=repository,
    )

    with pytest.raises(IPRoyalProxyConfigurationError):
        await runtime.start_run(iproyal_run())

    assert runtime._browser_manager is None
    assert runtime._task is None
    await repository.close()


async def test_persisted_iproyal_camoufox_run_is_refused(tmp_path: Path) -> None:
    database = tmp_path / "camoufox.sqlite3"
    repository = SQLiteSessionRepository(database)
    runtime = ApplicationRunRuntime(settings=proxy_settings(database), repository=repository)

    with pytest.raises(ValueError, match="Patchright or Chrome"):
        await runtime.start_run(iproyal_run(browser_backend=BrowserBackendName.CAMOUFOX))

    assert runtime._browser_manager is None
    await repository.close()


class _FakeRuntime:
    def __init__(self) -> None:
        self.started: list[RunConfig] = []

    async def start_run(self, run: RunConfig) -> None:
        self.started.append(run)

    def stop_accepting(self) -> None:
        return None

    async def capacity(self) -> RuntimeCapacity:
        return RuntimeCapacity(active_contexts=0, maximum_active_contexts=25, chrome_processes=0)

    def error(self) -> str | None:
        return None

    def latest_add_action(self) -> None:
        return None

    def session_action(self, session_id: str) -> None:
        del session_id

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def passing_preflights(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ready() -> Any:
        return SimpleNamespace(
            passed=True, error=None, remedy="", observed_browser_version="153.0.8010.54"
        )

    monkeypatch.setattr("queue_load_test.web.app.run_camoufox_preflight", ready)
    monkeypatch.setattr("queue_load_test.web.app.run_patchright_preflight", ready)


def test_setup_persists_safe_provenance_and_never_renders_credentials(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    database = tmp_path / "setup.sqlite3"
    runtime = _FakeRuntime()
    app = create_app(
        settings=proxy_settings(database),
        repository=SQLiteSessionRepository(database),
        runtime=cast(Any, runtime),
    )
    with TestClient(app) as client:
        setup_page = client.get("/setup").text
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )
        summary = client.get("/partials/summary").text
        dashboard = client.get("/dashboard").text

    assert "IPRoyal Residential" in setup_page
    assert "GB" in setup_page and "2h" in setup_page
    assert response.status_code == 303
    run = runtime.started[0]
    assert (run.proxy_provider, run.proxy_country, run.proxy_lifetime) == (
        ProxyProvider.IPROYAL,
        "gb",
        "2h",
    )
    assert "IPRoyal Residential" in summary and "GB" in summary
    for text in (setup_page, summary, dashboard, caplog.text, repr(run)):
        assert_no_secrets(text)
        assert FAKE_SERVER not in text
    with sqlite3.connect(database) as connection:
        dump = "\n".join(connection.iterdump())
    assert_no_secrets(dump)
    assert FAKE_SERVER not in dump
    assert_no_secrets(database.read_bytes().decode("latin-1"))


def test_setup_rejects_proxied_camoufox_without_persisting(tmp_path: Path) -> None:
    database = tmp_path / "camoufox-setup.sqlite3"
    runtime = _FakeRuntime()
    app = create_app(
        settings=proxy_settings(database, BROWSER_BACKEND="camoufox"),
        repository=SQLiteSessionRepository(database),
        runtime=cast(Any, runtime),
    )
    with TestClient(app) as client:
        response = client.post(
            "/setup",
            data={"target_url": "https://staging.example.test/", "requested_sessions": "2"},
            follow_redirects=False,
        )

    assert response.status_code == 422
    assert "Camoufox" in response.text
    assert runtime.started == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_config").fetchone() == (0,)


async def test_changed_environment_never_migrates_existing_run(tmp_path: Path) -> None:
    database = tmp_path / "no-migrate.sqlite3"
    repository = SQLiteSessionRepository(database)
    await repository.create_run(
        iproyal_run(
            proxy_provider=ProxyProvider.NONE,
            proxy_country=None,
            proxy_lifetime=None,
        )
    )
    await repository.close()

    runtime = _FakeRuntime()
    app = create_app(
        settings=proxy_settings(database, IPROYAL_PROXY_COUNTRY="us", IPROYAL_PROXY_LIFETIME="1h"),
        repository=SQLiteSessionRepository(database),
        runtime=cast(Any, runtime),
    )
    with TestClient(app):
        pass

    assert runtime.started[0].proxy_provider is ProxyProvider.NONE
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT proxy_provider, proxy_country, proxy_lifetime FROM run_config"
        ).fetchall() == [("none", None, None)]

    # An IPRoyal run likewise keeps its persisted country and lifetime.
    database_two = tmp_path / "no-migrate-iproyal.sqlite3"
    repository_two = SQLiteSessionRepository(database_two)
    await repository_two.create_run(iproyal_run())
    await repository_two.close()
    runtime_two = _FakeRuntime()
    app_two = create_app(
        settings=proxy_settings(
            database_two, IPROYAL_PROXY_COUNTRY="us", IPROYAL_PROXY_LIFETIME="1h"
        ),
        repository=SQLiteSessionRepository(database_two),
        runtime=cast(Any, runtime_two),
    )
    with TestClient(app_two):
        pass
    started = runtime_two.started[0]
    assert (started.proxy_country, started.proxy_lifetime) == ("gb", "2h")


def test_session_repr_contains_no_credentials() -> None:
    session = proxied_session("a", "Ab12Cd34")
    run = iproyal_run()

    assert_no_secrets(repr(session) + repr(run))
