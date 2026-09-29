"""Phase 8 Prompt 5: Direct Monitoring Strategy with browser fallback (deterministic)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from queue_load_test.direct_monitor import (
    HARD_FAILURES,
    SOFT_FAILURES,
    DirectCapability,
    DirectFallbackReason,
    DirectMonitoringHandler,
    DirectMonitorRecord,
    DirectMonitorStateError,
    DirectMonitorStateStore,
    DirectStatusChecker,
    DiscoveryRecipeHarvester,
    accepted_evidence_scopes,
)
from queue_load_test.direct_replay import ReplayRecipe
from queue_load_test.harness.local_queue_simulator import LocalQueueSimulator
from queue_load_test.metrics.logging import JsonLogFormatter
from queue_load_test.models import QueueProgress, QueueSession, QueueStatus, SessionMode
from queue_load_test.observation_equivalence import (
    DirectField,
    DirectResponseParser,
    DirectResponseSchema,
)
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import (
    MonitoringRetryPolicy,
    ParkedSessionScheduler,
    PollingPolicy,
    QueueSessionMonitor,
)
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import RestoreFailure, RestoreMethod, SessionRestoreResult

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TARGET = "http://127.0.0.1:9/entry"
SECRET_COOKIE = "cookie-secret-value-7f3a"
SECRET_HEADER = "header-secret-token-91bc"


def schema() -> DirectResponseSchema:
    fields = LocalQueueSimulator.status_schema()["fields"]
    assert isinstance(fields, dict)
    return DirectResponseSchema(
        {DirectField(name): tuple(path) for name, path in fields.items()},
        "local_simulator",
    )


def status_document(queue_id: str, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "queueId": queue_id,
        "preQueue": False,
        "activeQueue": True,
        "servicedSoon": False,
        "progress": 55,
        "usersAhead": 7,
    }
    document.update(overrides)
    return document


def recipe_for(session: QueueSession, *, fingerprint: str = "fp-1", **kw: object) -> ReplayRecipe:
    assert session.queue_id is not None
    values: dict[str, object] = {
        "session_id": session.session_id,
        "expected_queue_id": session.queue_id,
        "source_scope": "local_simulator",
        "exchange_sequence": 3,
        "url": f"http://127.0.0.1:9/status?q={session.queue_id}",
        "method": "GET",
        "headers": {"accept": "application/json", "x-visitor-token": SECRET_HEADER},
        "body": None,
        "observed_identifiers": {"queue_id": (session.queue_id,)},
        "fingerprint": fingerprint,
    }
    values.update(kw)
    return ReplayRecipe(**values)  # type: ignore[arg-type]


@dataclass
class FakeRestorer:
    """Stands in for the browser restore; optionally records Prompt 2 evidence."""

    evidence_directory: Path | None = None
    evidence_scope: str = "local_simulator"
    evidence_response: Callable[[str], object] | None = None
    evidence_url: Callable[[str], str] | None = None
    result: Callable[[QueueSession], SessionRestoreResult] | None = None
    calls: list[str] = field(default_factory=list)
    raise_error: bool = False

    async def restore(self, session: QueueSession) -> SessionRestoreResult:
        self.calls.append(session.session_id)
        if self.raise_error:
            raise RuntimeError("synthetic browser crash")
        if self.evidence_directory is not None and session.queue_id is not None:
            write_discovery_artifact(
                self.evidence_directory,
                session,
                scope=self.evidence_scope,
                response=(
                    self.evidence_response(session.queue_id)
                    if self.evidence_response is not None
                    else status_document(session.queue_id)
                ),
                url=(
                    self.evidence_url(session.queue_id)
                    if self.evidence_url is not None
                    else f"http://127.0.0.1:9/status?q={session.queue_id}"
                ),
            )
        if self.result is not None:
            return self.result(session)
        return SessionRestoreResult(
            method=RestoreMethod.STORAGE_STATE,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(
                session_id=session.session_id,
                active_queue=True,
                progress_percentage=50,
                users_ahead=9,
            ),
        )


def write_discovery_artifact(
    directory: Path,
    session: QueueSession,
    *,
    scope: str,
    response: object,
    url: str,
    queue_id: str | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    expected = queue_id or session.queue_id
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"{stamp}-{session.session_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sensitivity": "SENSITIVE_VISITOR_CREDENTIAL_EVIDENCE",
                "scope": scope,
                "session_id": session.session_id,
                "expected_queue_id": expected,
                "exchanges": [
                    {
                        "sequence": 1,
                        "classification": "observed_navigation",
                        "url": f"http://127.0.0.1:9/queue?q={expected}",
                        "method": "GET",
                        "request_headers": {},
                        "response_status": 200,
                        "response_json": None,
                    },
                    {
                        "sequence": 4,
                        "classification": "periodic_status_candidate",
                        "url": url,
                        "method": "GET",
                        "request_headers": {"accept": "application/json"},
                        "request_body": None,
                        "request_body_truncated": False,
                        "response_status": 200,
                        "response_json": response,
                        "response_body_truncated": False,
                        "observed_identifiers": {"queue_id": [expected]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@dataclass
class Harness:
    repository: SQLiteSessionRepository
    state_store: FileSystemStateStore
    store: DirectMonitorStateStore
    restorer: FakeRestorer
    monitor: QueueSessionMonitor
    checker: DirectStatusChecker
    handler: DirectMonitoringHandler
    requests: list[httpx.Request]
    evidence: Path


async def build(
    tmp_path: Path,
    respond: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    with_schema: bool = True,
    harvest: bool = True,
    restorer: FakeRestorer | None = None,
    failure_threshold: int = 3,
    target_url: str = TARGET,
    timeout_seconds: float = 5.0,
) -> Harness:
    repository = SQLiteSessionRepository(tmp_path / "direct.sqlite3")
    state_store = FileSystemStateStore(tmp_path / "state")
    store = DirectMonitorStateStore(tmp_path / "direct-monitor")
    evidence = tmp_path / "evidence"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if respond is not None:
            return respond(request)
        queue_id = request.url.params.get("q", "")
        return httpx.Response(200, json=status_document(queue_id))

    parser = DirectResponseParser(schema()) if with_schema else None
    restorer = restorer or FakeRestorer(evidence_directory=evidence)
    monitor = QueueSessionMonitor(
        repository=repository,
        restorer=restorer,
        polling_policy=PollingPolicy(jitter_seconds=0),
        retry_policy=MonitoringRetryPolicy(max_attempts=1),
        clock=lambda: NOW,
    )
    scopes = accepted_evidence_scopes(target_url)
    checker = DirectStatusChecker(
        store=store,
        browser_state=state_store,
        parser=parser,
        accepted_scopes=scopes,
        failure_threshold=failure_threshold,
        timeout_seconds=timeout_seconds,
        transport=httpx.MockTransport(handle),
        clock=lambda: NOW,
    )
    harvester = (
        DiscoveryRecipeHarvester(evidence_directory=evidence, parser=parser, accepted_scopes=scopes)
        if harvest and parser is not None
        else None
    )
    handler = DirectMonitoringHandler(
        browser_monitor=monitor, checker=checker, store=store, harvester=harvester
    )
    return Harness(
        repository, state_store, store, restorer, monitor, checker, handler, requests, evidence
    )


async def add_session(
    harness: Harness,
    session_id: str = "s-1",
    *,
    status: QueueStatus = QueueStatus.ACTIVE_QUEUE,
    capable: bool = True,
    browser_state: bool = True,
) -> QueueSession:
    queue_id = f"queue-{session_id}"
    session = QueueSession(
        session_id=session_id,
        queue_id=queue_id,
        transfer_url=f"http://127.0.0.1:9/queue?q={queue_id}",
        mode=SessionMode.HYBRID,
        status=status,
        state_path=harness.state_store.path_for(session_id),
        next_check_at=NOW,
    )
    await harness.repository.create(session)
    if browser_state:
        await harness.state_store.save(
            session_id,
            {
                "cookies": [
                    {"name": "queue_id", "value": queue_id, "domain": "127.0.0.1", "path": "/"},
                    {"name": "visitor", "value": SECRET_COOKIE, "domain": "127.0.0.1", "path": "/"},
                ],
                "origins": [],
            },
        )
    if capable:
        await harness.store.adopt_recipe(
            session_id=session_id, expected_queue_id=queue_id, recipe=recipe_for(session)
        )
    persisted = await harness.repository.get(session_id)
    assert persisted is not None
    return persisted


async def capability(harness: Harness, session_id: str = "s-1") -> DirectCapability | None:
    record = await harness.store.load(session_id)
    return record.capability if record is not None else None


# --------------------------------------------------------------------------- success


async def test_direct_success_persists_without_opening_a_browser(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness)

    outcome = await harness.handler.check(session)

    assert outcome.success is True
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    assert harness.restorer.calls == []
    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert str(request.url) == f"http://127.0.0.1:9/status?q={session.queue_id}"
    assert request.method == "GET"
    assert request.headers["x-visitor-token"] == SECRET_HEADER
    assert f"visitor={SECRET_COOKIE}" in request.headers.get("cookie", "")
    persisted = await harness.repository.get(session.session_id)
    progress = await harness.repository.get_progress(session.session_id)
    assert persisted is not None and progress is not None
    assert persisted.status is QueueStatus.ACTIVE_QUEUE
    assert persisted.queue_id == session.queue_id
    assert persisted.next_check_at is not None and persisted.next_check_at > NOW
    assert progress.progress_percentage == 55 and progress.users_ahead == 7
    assert persisted.last_checked_at == NOW  # a direct check is a real observation
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    assert harness.handler.metrics.direct_successes == 1
    assert harness.handler.metrics.browser_fallbacks == 0
    await harness.repository.close()


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"preQueue": True, "activeQueue": False, "progress": None}, QueueStatus.PRE_QUEUE),
        ({"servicedSoon": True}, QueueStatus.SERVICED_SOON),
    ],
)
async def test_direct_success_uses_the_one_lifecycle_evaluator(
    tmp_path: Path, document: dict[str, object], expected: QueueStatus
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=status_document(request.url.params["q"], **document))

    harness = await build(tmp_path, respond)
    session = await add_session(harness, status=QueueStatus.PARKED)

    outcome = await harness.handler.check(session)

    assert outcome.observed_status is expected
    assert harness.restorer.calls == []
    await harness.repository.close()


# --------------------------------------------------------------------------- fallbacks


def _json(status: int = 200, **overrides: object) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=status_document(request.url.params["q"], **overrides))

    return respond


def _raise(error: type[Exception]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error("synthetic", request=request)  # type: ignore[call-arg]

    return respond


def _raw(status: int, body: bytes, content_type: str, **headers: str) -> Callable[
    [httpx.Request], httpx.Response
]:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=body, headers={"content-type": content_type, **headers}
        )

    return respond


def _document(document: dict[str, object]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    return respond


FALLBACKS: list[tuple[str, Callable[[httpx.Request], httpx.Response], DirectFallbackReason]] = [
    ("network", _raise(httpx.ConnectError), DirectFallbackReason.NETWORK),
    ("timeout", _raise(httpx.ReadTimeout), DirectFallbackReason.TIMEOUT),
    ("http_500", _raw(500, b"no", "text/plain"), DirectFallbackReason.UNEXPECTED_HTTP_STATUS),
    (
        "redirect",
        _raw(302, b"", "text/plain", location="/elsewhere"),
        DirectFallbackReason.UNEXPECTED_REDIRECT,
    ),
    (
        "content_type",
        _raw(200, b"<h1>x</h1>", "text/html"),
        DirectFallbackReason.UNEXPECTED_CONTENT_TYPE,
    ),
    (
        "malformed",
        _raw(200, b"{not json", "application/json"),
        DirectFallbackReason.MALFORMED_RESPONSE,
    ),
    (
        "oversize",
        _raw(200, b'{"pad":"' + b"x" * 70_000 + b'"}', "application/json"),
        DirectFallbackReason.MALFORMED_RESPONSE,
    ),
    ("wrong_type", _json(usersAhead="seven"), DirectFallbackReason.SCHEMA_INCOMPATIBLE),
    ("not_object", _document([1, 2]), DirectFallbackReason.SCHEMA_INCOMPATIBLE),  # type: ignore[arg-type]
    ("missing_id", _document({"activeQueue": True}), DirectFallbackReason.IDENTITY_AMBIGUITY),
    ("mismatch", _json(queueId="queue-someone-else"), DirectFallbackReason.IDENTITY_MISMATCH),
    ("rejected", _raw(403, b"no", "text/plain"), DirectFallbackReason.REJECTED_SESSION_STATE),
    (
        "unknown_lifecycle",
        _json(activeQueue=False, usersAhead=None),
        DirectFallbackReason.UNKNOWN_LIFECYCLE,
    ),
    (
        "contradictory",
        _json(preQueue=True, activeQueue=True),
        DirectFallbackReason.CONTRADICTORY_LIFECYCLE,
    ),
    (
        "admission",
        _json(redirectUrl="http://127.0.0.1:9/protected"),
        DirectFallbackReason.UNSUPPORTED_ADMISSION,
    ),
]


@pytest.mark.parametrize(
    ("name", "respond", "reason"), FALLBACKS, ids=[item[0] for item in FALLBACKS]
)
async def test_every_direct_failure_class_falls_back_to_the_browser_monitor(
    tmp_path: Path,
    name: str,
    respond: Callable[[httpx.Request], httpx.Response],
    reason: DirectFallbackReason,
) -> None:
    harness = await build(tmp_path, respond, harvest=False)
    session = await add_session(harness)

    outcome = await harness.handler.check(session)

    assert harness.restorer.calls == [session.session_id], name
    assert outcome.success is True
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    assert harness.handler.metrics.fallback_reasons == {reason.value: 1}
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None
    assert persisted.queue_id == session.queue_id
    assert persisted.transfer_url == session.transfer_url
    record = await harness.store.load(session.session_id)
    assert record is not None and record.last_reason is reason
    expected = (
        DirectCapability.DIRECT_UNAVAILABLE
        if reason in HARD_FAILURES
        else DirectCapability.DIRECT_CAPABLE
    )
    assert record.capability is expected
    assert reason in HARD_FAILURES | SOFT_FAILURES
    await harness.repository.close()


async def test_direct_timeout_is_bounded_by_the_configured_request_timeout(
    tmp_path: Path,
) -> None:
    simulator = LocalQueueSimulator(status_enabled=True, slow_seconds=5.0)
    await simulator.start()
    try:
        repository = SQLiteSessionRepository(tmp_path / "slow.sqlite3")
        state_store = FileSystemStateStore(tmp_path / "state")
        store = DirectMonitorStateStore(tmp_path / "direct-monitor")
        restorer = FakeRestorer()
        monitor = QueueSessionMonitor(
            repository=repository,
            restorer=restorer,
            polling_policy=PollingPolicy(jitter_seconds=0),
            retry_policy=MonitoringRetryPolicy(max_attempts=1),
        )
        handler = DirectMonitoringHandler(
            browser_monitor=monitor,
            checker=DirectStatusChecker(
                store=store,
                browser_state=state_store,
                parser=DirectResponseParser(schema()),
                accepted_scopes=accepted_evidence_scopes(simulator.entry_url),
                timeout_seconds=0.3,
            ),
            store=store,
        )
        harness = Harness(
            repository, state_store, store, restorer, monitor, handler._checker, handler, [],
            tmp_path,
        )
        session = await add_session(harness, capable=False)
        assert session.queue_id is not None
        simulator.status_faults[session.queue_id] = "slow"
        await store.adopt_recipe(
            session_id=session.session_id,
            expected_queue_id=session.queue_id,
            recipe=recipe_for(session, url=f"{simulator.status_url}?q={session.queue_id}"),
        )

        started = asyncio.get_running_loop().time()
        outcome = await handler.check(session)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 3.0
        assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
        assert handler.metrics.fallback_reasons == {DirectFallbackReason.TIMEOUT.value: 1}
        assert restorer.calls == [session.session_id]
        await repository.close()
    finally:
        await simulator.close()


async def test_missing_browser_state_is_a_fallback_not_an_acquisition(tmp_path: Path) -> None:
    harness = await build(tmp_path, harvest=False)
    session = await add_session(harness, browser_state=False)

    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.MISSING_VISITOR_STATE.value: 1
    }
    assert await capability(harness) is DirectCapability.DIRECT_UNAVAILABLE
    await harness.repository.close()


async def test_backward_lifecycle_is_contradictory_and_never_written(tmp_path: Path) -> None:
    def serviced(session: QueueSession) -> SessionRestoreResult:
        return SessionRestoreResult(
            method=RestoreMethod.STORAGE_STATE,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            progress=QueueProgress(session_id=session.session_id, serviced_soon=True),
        )

    harness = await build(
        tmp_path,
        _json(preQueue=True, activeQueue=False),
        harvest=False,
        restorer=FakeRestorer(result=serviced),
    )
    session = await add_session(harness, status=QueueStatus.SERVICED_SOON)

    outcome = await harness.handler.check(session)

    assert outcome.observed_status is QueueStatus.SERVICED_SOON

    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.CONTRADICTORY_LIFECYCLE.value: 1
    }
    assert harness.restorer.calls == [session.session_id]
    await harness.repository.close()


@pytest.mark.parametrize(
    ("scope", "url", "target"),
    [
        ("diagnostic_unverified_target", "http://127.0.0.1:9/status", TARGET),
        ("local_simulator", "https://queue.example.test/status", TARGET),
        ("local_simulator", "http://127.0.0.1:9/status", "https://staging.example.test/"),
    ],
)
async def test_recipe_provenance_uncertainty_falls_back(
    tmp_path: Path, scope: str, url: str, target: str
) -> None:
    harness = await build(tmp_path, harvest=False, target_url=target)
    session = await add_session(harness, capable=False)
    assert session.queue_id is not None
    await harness.store.adopt_recipe(
        session_id=session.session_id,
        expected_queue_id=session.queue_id,
        recipe=recipe_for(session, source_scope=scope, url=url),
    )

    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.RECIPE_UNCERTAIN.value: 1
    }
    assert await capability(harness) is DirectCapability.DIRECT_UNAVAILABLE
    await harness.repository.close()


async def test_record_for_another_identity_is_fenced_not_reused(tmp_path: Path) -> None:
    harness = await build(tmp_path, harvest=False)
    session = await add_session(harness, capable=False)
    other = QueueSession(
        session_id=session.session_id,
        queue_id="queue-previous-identity",
        transfer_url=session.transfer_url,
        mode=session.mode,
        state_path=session.state_path,
    )
    await harness.store.adopt_recipe(
        session_id=session.session_id,
        expected_queue_id="queue-previous-identity",
        recipe=recipe_for(other),
    )

    before = harness.store.path_for(session.session_id).read_bytes()
    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.RECIPE_UNCERTAIN.value: 1
    }
    # Report-only: the other identity's record is neither reused nor rewritten.
    assert harness.store.path_for(session.session_id).read_bytes() == before
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None and persisted.queue_id == session.queue_id
    await harness.repository.close()


async def test_corrupt_record_falls_back_and_is_kept_for_the_report(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path, harvest=False)
    session = await add_session(harness)
    path = harness.store.path_for(session.session_id)
    path.write_text(path.read_text().replace("DIRECT_CAPABLE", "DIRECT_UNAVAILABLE"))
    tampered = path.read_bytes()

    with pytest.raises(DirectMonitorStateError):
        await harness.store.load(session.session_id)
    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.RECIPE_UNCERTAIN.value: 1
    }
    assert path.read_bytes() == tampered  # no automatic destructive repair
    await harness.repository.close()


async def test_unexpected_checker_error_is_uncertainty_and_uses_the_browser(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path, harvest=False)
    session = await add_session(harness)

    async def explode(_: QueueSession) -> object:
        raise OSError("disk unavailable")

    harness.checker.attempt = explode  # type: ignore[method-assign, assignment]
    outcome = await harness.handler.check(session)

    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    assert harness.handler.metrics.fallback_reasons == {DirectFallbackReason.UNCERTAIN.value: 1}
    await harness.repository.close()


async def test_identity_mismatch_preserves_identity_and_disables_direct_until_refresh(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path, _json(queueId="queue-intruder"), harvest=False)
    session = await add_session(harness)

    await harness.handler.check(session)
    second = await harness.repository.get(session.session_id)
    assert second is not None
    await harness.handler.check(second)

    assert len(harness.requests) == 1  # the unavailable session is not polled directly again
    assert harness.restorer.calls == [session.session_id, session.session_id]
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.IDENTITY_MISMATCH.value: 1,
        DirectFallbackReason.DIRECT_UNAVAILABLE.value: 1,
    }
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None
    assert persisted.queue_id == session.queue_id
    assert await harness.repository.count_successful_queue_ids() == 1
    await harness.repository.close()


async def test_soft_failures_disable_direct_only_after_the_threshold(tmp_path: Path) -> None:
    outcomes = iter([500, 200, 500, 500])

    def respond(request: httpx.Request) -> httpx.Response:
        code = next(outcomes)
        if code == 200:
            return httpx.Response(200, json=status_document(request.url.params["q"]))
        return httpx.Response(code, text="unavailable")

    harness = await build(tmp_path, respond, harvest=False, failure_threshold=2)
    session = await add_session(harness)

    for expected in (
        DirectCapability.DIRECT_CAPABLE,
        DirectCapability.DIRECT_CAPABLE,  # success resets the consecutive count
        DirectCapability.DIRECT_CAPABLE,
        DirectCapability.DIRECT_UNAVAILABLE,
    ):
        current = await harness.repository.get(session.session_id)
        assert current is not None
        await harness.handler.check(current)
        assert await capability(harness) is expected
    assert len(harness.requests) == 4
    await harness.repository.close()


# ------------------------------------------------------------------ capability states


async def test_schema_unavailable_means_every_check_is_browser_only(tmp_path: Path) -> None:
    harness = await build(tmp_path, with_schema=False)
    session = await add_session(harness)

    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.restorer.calls == [session.session_id]
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.SCHEMA_UNAVAILABLE.value: 1
    }
    # Capability metadata is untouched: it is an implementation state only.
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    await harness.repository.close()


async def test_discovery_required_uses_browser_then_adopts_the_observed_recipe(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness, capable=False)

    first = await harness.handler.check(session)
    assert first.observed_status is QueueStatus.ACTIVE_QUEUE
    assert harness.requests == []
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.DISCOVERY_REQUIRED.value: 1
    }
    record = await harness.store.load(session.session_id)
    assert record is not None
    assert record.capability is DirectCapability.DIRECT_CAPABLE
    assert record.recipe is not None
    assert record.recipe.exchange_sequence == 4  # the browser's own status exchange
    assert harness.handler.metrics.recipes_adopted == 1

    current = await harness.repository.get(session.session_id)
    assert current is not None
    await harness.handler.check(current)
    assert len(harness.requests) == 1
    assert harness.restorer.calls == [session.session_id]
    await harness.repository.close()


async def test_unavailable_session_recovers_only_through_a_new_browser_observation(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness)
    record = await harness.store.load(session.session_id)
    assert record is not None
    await harness.store.record_failure(
        record, reason=DirectFallbackReason.IDENTITY_MISMATCH, make_unavailable=True
    )
    await harness.store.cookies.save(
        session_id=session.session_id, recipe_fingerprint="fp-1", cookies=()
    )

    await harness.handler.check(session)

    refreshed = await harness.store.load(session.session_id)
    assert refreshed is not None
    assert refreshed.capability is DirectCapability.DIRECT_CAPABLE
    assert refreshed.recipe is not None and refreshed.recipe.fingerprint != "fp-1"
    assert refreshed.consecutive_failures == 0
    # Cookies retained for the old recipe are discarded on adoption.
    assert not harness.store.cookie_path(session.session_id).exists()
    await harness.repository.close()


@pytest.mark.parametrize(
    "case",
    ["stale_artifact", "other_identity", "schema_rejects", "wrong_scope", "remote_url"],
)
async def test_recipe_refresh_never_derives_a_recipe_from_unproven_evidence(
    tmp_path: Path, case: str
) -> None:
    evidence = tmp_path / "evidence"
    restorer = FakeRestorer(
        evidence_directory=None if case in {"stale_artifact", "other_identity"} else evidence,
        evidence_scope="diagnostic_unverified_target" if case == "wrong_scope" else "local_simulator",
        evidence_response=(
            (lambda queue_id: {"activeQueue": True}) if case == "schema_rejects" else None
        ),
        evidence_url=(
            (lambda queue_id: f"https://queue.example.test/status?q={queue_id}")
            if case == "remote_url"
            else None
        ),
    )
    harness = await build(tmp_path, restorer=restorer)
    session = await add_session(harness, capable=False)
    if case == "stale_artifact":
        path = write_discovery_artifact(
            evidence,
            session,
            scope="local_simulator",
            response=status_document(str(session.queue_id)),
            url=f"http://127.0.0.1:9/status?q={session.queue_id}",
        )
        old = datetime.now(UTC).timestamp() - 3_600
        os.utime(path, (old, old))
    if case == "other_identity":
        write_discovery_artifact(
            evidence,
            session,
            scope="local_simulator",
            response=status_document("queue-other"),
            url="http://127.0.0.1:9/status?q=queue-other",
            queue_id="queue-other",
        )

    await harness.handler.check(session)

    assert await capability(harness) is None
    assert harness.handler.metrics.recipes_adopted == 0
    await harness.repository.close()


async def test_no_recipe_refresh_after_a_terminal_browser_outcome(tmp_path: Path) -> None:
    def admitted(session: QueueSession) -> SessionRestoreResult:
        return SessionRestoreResult(
            method=RestoreMethod.STORAGE_STATE,
            success=True,
            expected_queue_id=session.queue_id,
            observed_queue_id=session.queue_id,
            identity_match=True,
            admitted=True,
        )

    restorer = FakeRestorer(evidence_directory=tmp_path / "evidence", result=admitted)
    harness = await build(tmp_path, restorer=restorer)
    session = await add_session(harness, capable=False)

    outcome = await harness.handler.check(session)

    assert outcome.observed_status is QueueStatus.ADMITTED
    assert await capability(harness) is None
    await harness.repository.close()


def test_real_targets_accept_only_authorised_staging_evidence() -> None:
    assert accepted_evidence_scopes("https://staging.example.test/") == frozenset(
        {"authorized_queue_it_staging"}
    )
    assert "local_simulator" in accepted_evidence_scopes("http://127.0.0.1:8123/entry")
    assert "local_simulator" in accepted_evidence_scopes("http://localhost:8123/entry")


# ------------------------------------------------------------- scheduler invariants


def scheduler_for(harness: Harness, **overrides: object) -> ParkedSessionScheduler:
    values: dict[str, object] = {
        "repository": harness.repository,
        "handler": harness.handler,
        "worker_count": 2,
        "queue_capacity": 4,
        "claim_batch_size": 4,
        "lease_seconds": 60,
        "failure_delay_seconds": 30,
        "clock": lambda: NOW,
        "scheduler_id": "direct-scheduler",
    }
    values.update(overrides)
    return ParkedSessionScheduler(**values)  # type: ignore[arg-type]


async def test_scheduler_runs_direct_checks_with_leases_released(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    for index in range(4):
        await add_session(harness, f"s-{index}")
    scheduler = scheduler_for(harness)

    await scheduler.start()
    assert await scheduler.schedule_due() == 4
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    assert len(harness.requests) == 4
    assert harness.restorer.calls == []
    assert scheduler.metrics.completed == 4 and scheduler.metrics.failed == 0
    assert scheduler.metrics.maximum_concurrent_checks <= 2
    for index in range(4):
        persisted = await harness.repository.get(f"s-{index}")
        assert persisted is not None
        assert persisted.worker_id is None and persisted.lease_until is None
        assert persisted.next_check_at is not None and persisted.next_check_at > NOW
    await harness.repository.close()


async def test_pause_stops_direct_and_browser_checks_and_resume_continues(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path)
    await add_session(harness, "direct")
    await add_session(harness, "needs-browser", capable=False)
    scheduler = scheduler_for(harness)
    await scheduler.start()

    await scheduler.pause_monitoring()
    assert await scheduler.schedule_due() == 0
    assert harness.requests == [] and harness.restorer.calls == []
    assert (await harness.repository.due_session_summary(now=NOW)).count == 2

    # A fresh process sees the same persisted pause.
    restarted = scheduler_for(harness, scheduler_id="restarted")
    await restarted.start()
    assert await restarted.monitoring_paused()
    assert await restarted.schedule_due() == 0
    await restarted.shutdown()

    await scheduler.resume_monitoring()
    assert await scheduler.schedule_due() == 2
    await scheduler.wait_until_idle()
    await scheduler.shutdown()
    assert len(harness.requests) == 1
    assert harness.restorer.calls == ["needs-browser"]
    await harness.repository.close()


async def test_in_flight_direct_check_finishes_when_paused(tmp_path: Path) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    harness = await build(tmp_path)
    for index in range(3):
        await add_session(harness, f"s-{index}")
    original = harness.checker.attempt

    async def slow_attempt(session: QueueSession) -> object:
        started.set()
        await release.wait()
        return await original(session)

    harness.checker.attempt = slow_attempt  # type: ignore[method-assign, assignment]
    scheduler = scheduler_for(harness, worker_count=1, queue_capacity=3, claim_batch_size=3)
    await scheduler.start()
    assert await scheduler.schedule_due() == 3
    await started.wait()
    await scheduler.pause_monitoring()
    release.set()
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    assert len(harness.requests) == 1
    assert scheduler.metrics.skipped_while_paused == 2
    assert (await harness.repository.due_session_summary(now=NOW)).count == 2
    await harness.repository.close()


async def test_manual_open_ownership_fences_automatic_direct_polling(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    await add_session(harness, "open")
    await add_session(harness, "parked")
    await harness.repository.acquire_manual_ownership(
        "open", owner_id="manual-1", now=NOW, lease_until=NOW + timedelta(minutes=5), capacity=2
    )
    scheduler = scheduler_for(harness)

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    assert [request.url.params["q"] for request in harness.requests] == ["queue-parked"]
    opened = await harness.repository.get("open")
    assert opened is not None and opened.manual_owner_id == "manual-1"

    await harness.repository.release_manual_ownership("open", owner_id="manual-1")
    again = scheduler_for(harness, scheduler_id="after-close")
    await again.start()
    assert await again.schedule_due() == 1
    await again.wait_until_idle()
    await again.shutdown()
    assert [request.url.params["q"] for request in harness.requests][-1] == "queue-open"
    await harness.repository.close()


async def test_stale_worker_direct_write_is_fenced(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    await add_session(harness)
    claimed = await harness.repository.claim_due_sessions(
        worker_id="stale-worker", now=NOW, lease_until=NOW + timedelta(seconds=1), limit=1
    )
    stale = next(iter(claimed))
    # The lease expires and another worker takes the session over.
    later = NOW + timedelta(seconds=5)
    takeover = await harness.repository.claim_due_sessions(
        worker_id="new-owner", now=later, lease_until=later + timedelta(minutes=1), limit=1
    )
    assert len(list(takeover)) == 1

    with pytest.raises(Exception, match="ownership"):
        await harness.handler.check(stale)

    persisted = await harness.repository.get(stale.session_id)
    assert persisted is not None
    assert persisted.worker_id == "new-owner"
    assert persisted.status is QueueStatus.ACTIVE_QUEUE
    assert await harness.repository.get_progress(stale.session_id) is None
    await harness.repository.close()


async def test_shutdown_cancels_a_hung_direct_check_and_releases_its_lease(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    harness = await build(tmp_path)
    await add_session(harness)

    async def hang(_: QueueSession) -> object:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    harness.checker.attempt = hang  # type: ignore[method-assign, assignment]
    scheduler = scheduler_for(harness, worker_count=1, queue_capacity=1, claim_batch_size=1)
    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await started.wait()
    await scheduler.shutdown(timeout_seconds=0.2)

    persisted = await harness.repository.get("s-1")
    assert persisted is not None
    assert persisted.queue_id == "queue-s-1"
    assert persisted.worker_id is None
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    await harness.repository.close()


async def test_browser_crash_during_fallback_reparks_and_keeps_identity(tmp_path: Path) -> None:
    restorer = FakeRestorer(raise_error=True)
    harness = await build(tmp_path, _raw(500, b"no", "text/plain"), restorer=restorer)
    await add_session(harness)
    scheduler = scheduler_for(harness, worker_count=1, queue_capacity=1, claim_batch_size=1)

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    persisted = await harness.repository.get("s-1")
    assert persisted is not None
    assert persisted.queue_id == "queue-s-1"
    assert persisted.last_error == "monitor:worker_failure"
    assert persisted.worker_id is None
    assert persisted.next_check_at == NOW + timedelta(seconds=30)
    assert scheduler.metrics.failed == 1
    assert await harness.repository.count_successful_queue_ids() == 1
    await harness.repository.close()


async def test_browser_restore_failure_during_fallback_is_connection_lost_not_reacquired(
    tmp_path: Path,
) -> None:
    def lost(session: QueueSession) -> SessionRestoreResult:
        return SessionRestoreResult(
            method=RestoreMethod.STORAGE_STATE,
            success=False,
            expected_queue_id=session.queue_id,
            failure=RestoreFailure.NAVIGATION_FAILED,
        )

    harness = await build(
        tmp_path, _raise(httpx.ConnectError), restorer=FakeRestorer(result=lost)
    )
    session = await add_session(harness)

    outcome = await harness.handler.check(session)

    assert outcome.observed_status is QueueStatus.CONNECTION_LOST
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None and persisted.queue_id == session.queue_id
    assert await harness.repository.count_successful_queue_ids() == 1
    await harness.repository.close()


# ---------------------------------------------------------------- restart and storage


async def test_capability_and_refreshed_cookies_survive_restart(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=status_document(request.url.params["q"]),
            headers={"set-cookie": "rotated=after-response; Path=/"},
        )

    harness = await build(tmp_path, respond)
    session = await add_session(harness)
    await harness.handler.check(session)
    await harness.repository.close()

    restarted = await build(tmp_path)
    current = await restarted.repository.get(session.session_id)
    assert current is not None
    await restarted.handler.check(current)

    assert restarted.restorer.calls == []
    assert "rotated=after-response" in restarted.requests[0].headers.get("cookie", "")
    await restarted.repository.close()


async def test_protected_store_permissions_integrity_and_cleanup(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness)
    path = harness.store.path_for(session.session_id)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert (harness.store.directory / ".gitignore").read_text() == "*\n"
    assert (path.parent / ".gitignore").read_text() == "*\n"

    await harness.store.cookies.save(
        session_id=session.session_id, recipe_fingerprint="fp-1", cookies=()
    )
    assert await harness.store.delete(session.session_id) is True
    assert await harness.store.load(session.session_id) is None
    assert not harness.store.cookie_path(session.session_id).exists()

    await add_session(harness, "s-2")
    await add_session(harness, "s-3")
    assert await harness.store.clear() == 2
    assert await harness.store.load("s-2") is None
    await harness.repository.close()


async def test_replayable_values_never_reach_sqlite_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    harness = await build(tmp_path, _json(queueId="queue-intruder"))
    session = await add_session(harness)
    await harness.handler.check(session)
    current = await harness.repository.get(session.session_id)
    assert current is not None
    await harness.handler.check(current)
    await harness.repository.close()

    database = b"".join(
        path.read_bytes() for path in tmp_path.glob("direct.sqlite3*") if path.is_file()
    )
    for secret in (SECRET_COOKIE.encode(), SECRET_HEADER.encode(), b"/status?q="):
        assert secret not in database
    # What the application's structured logging actually emits (third-party
    # messages such as httpx request lines included) never carries the recipe.
    formatter = JsonLogFormatter()
    text = "\n".join(formatter.format(record) for record in caplog.records)
    assert SECRET_COOKIE not in text and SECRET_HEADER not in text
    assert "status?q=" not in text
    assert "direct_monitor_fallback" in text


def test_record_rejects_inconsistent_metadata(tmp_path: Path) -> None:
    session = QueueSession(
        session_id="s-1",
        queue_id="queue-s-1",
        transfer_url="http://127.0.0.1:9/queue?q=queue-s-1",
        mode=SessionMode.HYBRID,
        state_path=tmp_path / "s-1.json",
    )
    with pytest.raises(ValueError, match="needs a browser-observed recipe"):
        DirectMonitorRecord(
            session_id="s-1",
            expected_queue_id="queue-s-1",
            capability=DirectCapability.DIRECT_CAPABLE,
            updated_at=NOW,
        )
    with pytest.raises(ValueError, match="another session or identity"):
        DirectMonitorRecord(
            session_id="s-1",
            expected_queue_id="queue-other",
            capability=DirectCapability.DIRECT_CAPABLE,
            updated_at=NOW,
            recipe=recipe_for(session),
        )


def test_direct_monitor_settings_validate_bounds() -> None:
    from pydantic import ValidationError

    from queue_load_test.config import Settings

    settings = Settings(_env_file=None, DIRECT_MONITOR_SCHEMA_PATH="  ")  # type: ignore[call-arg]
    assert settings.direct_monitor_schema_path is None
    assert settings.direct_monitor_directory == Path(".direct-monitor")
    with pytest.raises(ValidationError, match="less than MONITOR_LEASE_SECONDS"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, DIRECT_MONITOR_TIMEOUT_SECONDS=30, MONITOR_LEASE_SECONDS=20
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, DIRECT_MONITOR_FAILURE_THRESHOLD=0)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("scope", "target", "accepted"),
    [
        ("authorized_queue_it_staging", "https://staging.example.test/", True),
        ("local_simulator", "https://staging.example.test/", False),
        ("local_simulator", "http://127.0.0.1:8000/entry", True),
        ("diagnostic_unverified_target", "http://127.0.0.1:8000/entry", False),
    ],
)
def test_runtime_accepts_only_schemas_valid_for_the_run_target(
    tmp_path: Path, scope: str, target: str, accepted: bool
) -> None:
    from queue_load_test.config import Settings
    from queue_load_test.web.service import _direct_response_schema

    path = tmp_path / "schema.json"
    path.write_text(json.dumps(LocalQueueSimulator.status_schema(scope)), encoding="utf-8")
    settings = Settings(_env_file=None, DIRECT_MONITOR_SCHEMA_PATH=str(path))  # type: ignore[call-arg]

    assert (_direct_response_schema(settings, target) is not None) is accepted


def test_runtime_schema_absent_or_invalid_disables_direct_requests(tmp_path: Path) -> None:
    from queue_load_test.config import Settings
    from queue_load_test.web.service import _direct_response_schema

    unset = Settings(_env_file=None)  # type: ignore[call-arg]
    assert _direct_response_schema(unset, TARGET) is None
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    invalid = Settings(_env_file=None, DIRECT_MONITOR_SCHEMA_PATH=str(broken))  # type: ignore[call-arg]
    assert _direct_response_schema(invalid, TARGET) is None


def test_runtime_assembles_direct_handler_only_for_direct_runs(tmp_path: Path) -> None:
    from queue_load_test.config import Settings
    from queue_load_test.models import MonitoringStrategy, RunConfig
    from queue_load_test.web.service import _direct_handler_for_run

    path = tmp_path / "schema.json"
    path.write_text(json.dumps(LocalQueueSimulator.status_schema()), encoding="utf-8")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        DIRECT_MONITOR_SCHEMA_PATH=str(path),
        DIRECT_MONITOR_DIRECTORY=str(tmp_path / "dm"),
    )
    monitor = QueueSessionMonitor(
        repository=SQLiteSessionRepository(tmp_path / "wiring.sqlite3"),
        restorer=FakeRestorer(),
        polling_policy=PollingPolicy(),
    )

    def run(strategy: MonitoringStrategy) -> RunConfig:
        return RunConfig(
            run_id=strategy.value,
            target_url=TARGET,
            requested_sessions=1,
            created_at=NOW,
            monitoring_strategy=strategy,
        )

    state = FileSystemStateStore(tmp_path / "state")
    assert (
        _direct_handler_for_run(
            run(MonitoringStrategy.HEADED_WINDOW),
            settings,
            browser_monitor=monitor,
            state_store=state,
            target_url=TARGET,
        )
        is None
    )
    handler = _direct_handler_for_run(
        run(MonitoringStrategy.DIRECT),
        settings,
        browser_monitor=monitor,
        state_store=state,
        target_url=TARGET,
    )
    assert handler is not None
    assert handler._checker.enabled is True
    assert handler._harvester is None  # discovery disabled: capability cannot be established
    with_discovery = _direct_handler_for_run(
        run(MonitoringStrategy.DIRECT),
        settings.model_copy(update={"status_discovery_enabled": True}),
        browser_monitor=monitor,
        state_store=state,
        target_url=TARGET,
    )
    assert with_discovery is not None and with_discovery._harvester is not None
