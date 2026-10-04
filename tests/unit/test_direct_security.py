"""Phase 8 Prompt 6: Direct Monitoring secrecy, observability, and failure hardening."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_direct_monitor import (
    NOW,
    FakeRestorer,
    Harness,
    add_session,
    build,
    capability,
    recipe_for,
    scheduler_for,
    status_document,
    write_discovery_artifact,
)

from queue_load_test.browser import BrowserManager
from queue_load_test.browser.manager import BrowserCapacity
from queue_load_test.config import Settings
from queue_load_test.direct_monitor import (
    DirectCapability,
    DirectFallbackReason,
    DirectMonitorConsistencyChecker,
    DirectMonitorFindingKind,
    DirectMonitoringHandler,
    DirectMonitorStateStore,
    accepted_evidence_scopes,
    recipe_reference,
)
from queue_load_test.harness.state_consistency import run_consistency_check
from queue_load_test.metrics import PrometheusMetrics
from queue_load_test.metrics.logging import (
    REDACTED_TRANSPORT_DETAIL,
    JsonLogFormatter,
    configure_structured_logging,
)
from queue_load_test.metrics.status import StatusSummaryProvider
from queue_load_test.models import QueueSession, QueueStatus
from queue_load_test.repository import DirectMonitorStatus, SQLiteSessionRepository
from queue_load_test.web.actions import (
    OperatorActionKind,
    OperatorActionManager,
    OperatorActionStatus,
)

SECRETS = {
    "cookie": "SECRETCOOKIE4a1f9e",
    "set_cookie": "SECRETSETCOOKIE77d2",
    "authorization": "SECRETBEARER0c3b5a",
    "csrf": "SECRETCSRF61e0aa",
    "url": "SECRETURLTOKEN9bb1",
    "body": "SECRETBODY2e8c44",
    "response": "SECRETRESPONSE5d90",
    "storage": "SECRETSTORAGE13ab",
    "exception": "SECRETEXCEPTION8f27",
}


def _no_secret(text: str) -> list[str]:
    return sorted(name for name, value in SECRETS.items() if value in text)


def _formatted(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonLogFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


async def _seeded(harness: Harness, session_id: str = "s-1") -> QueueSession:
    session = await add_session(harness, session_id, capable=False)
    assert session.queue_id is not None
    await harness.state_store.save(
        session_id,
        {
            "cookies": [
                {
                    "name": "queue_id",
                    "value": session.queue_id,
                    "domain": "127.0.0.1",
                    "path": "/",
                },
                {
                    "name": "visitor",
                    "value": SECRETS["cookie"],
                    "domain": "127.0.0.1",
                    "path": "/",
                },
            ],
            "origins": [
                {
                    "origin": "http://127.0.0.1:9",
                    "localStorage": [{"name": "token", "value": SECRETS["storage"]}],
                }
            ],
        },
    )
    await harness.store.adopt_recipe(
        session_id=session_id,
        expected_queue_id=session.queue_id,
        recipe=recipe_for(
            session,
            method="POST",
            url=f"http://127.0.0.1:9/status?q={session.queue_id}&t={SECRETS['url']}",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {SECRETS['authorization']}",
                "x-csrf-token": SECRETS["csrf"],
            },
            body=json.dumps({"session": SECRETS["body"]}).encode(),
        ),
    )
    persisted = await harness.repository.get(session_id)
    assert persisted is not None
    return persisted


def _secret_responses() -> Callable[[httpx.Request], httpx.Response]:
    """Cycle through success, failures, and an exception that quotes a secret."""

    calls = {"count": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        queue_id = request.url.params["q"]
        step = calls["count"] % 4
        headers = {"set-cookie": f"rotation={SECRETS['set_cookie']}; Path=/"}
        if step == 1:
            return httpx.Response(
                200,
                json={**status_document(queue_id), "echo": SECRETS["response"]},
                headers=headers,
            )
        if step == 2:
            return httpx.Response(500, text=f"boom {SECRETS['response']}", headers=headers)
        if step == 3:
            return httpx.Response(
                200, json=status_document("queue-other") | {"leak": SECRETS["response"]}
            )
        raise httpx.ConnectError(
            f"cannot reach {request.url} with {SECRETS['exception']}", request=request
        )

    return respond


# ------------------------------------------------------------------ secret leaks


async def test_seeded_secrets_never_reach_logs_metrics_status_sqlite_or_reports(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    metrics = PrometheusMetrics()
    harness = await build(tmp_path, _secret_responses())
    handler = DirectMonitoringHandler(
        browser_monitor=harness.monitor,
        checker=harness.checker,
        store=harness.store,
        metadata=harness.repository,
        observability=metrics,
    )
    session = await _seeded(harness)
    for _ in range(8):
        current = await harness.repository.get(session.session_id)
        assert current is not None
        # Keep the session capable so every response variant is exercised.
        record = await harness.store.load(session.session_id)
        assert record is not None and record.recipe is not None
        await harness.store.adopt_recipe(
            session_id=session.session_id,
            expected_queue_id=str(session.queue_id),
            recipe=record.recipe,
        )
        await handler.check(current)
    metrics.set_direct_capability_counts(await harness.repository.direct_capability_counts())

    class _Browser:
        def report_navigation(self, context: object, *, responsive: bool) -> None:
            return None

        async def capacity(self) -> BrowserCapacity:
            return BrowserCapacity(1, 1, 0, 5, 5, ())

    status = await StatusSummaryProvider(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        repository=harness.repository,
        browser_manager=cast(BrowserManager, _Browser()),
        metrics=metrics,
    ).snapshot()
    report = await DirectMonitorConsistencyChecker(harness.repository, harness.store).check()
    await harness.repository.close()

    surfaces = {
        "logs": _formatted(caplog),
        "metrics": metrics.render().decode(),
        "status": status.render_text(),
        "sqlite": b"".join(
            path.read_bytes() for path in tmp_path.glob("direct.sqlite3*")
        ).decode("latin-1"),
        "consistency_report": json.dumps(report.to_dict()),
        "handler_metrics": repr(handler.metrics),
        "record_repr": repr(await harness.store.load(session.session_id)),
    }
    leaks = {name: _no_secret(text) for name, text in surfaces.items()}
    assert all(not found for found in leaks.values()), leaks
    assert handler.metrics.direct_successes >= 1
    assert handler.metrics.browser_fallbacks >= 3
    # The secret-bearing recipe really is persisted, but only in the protected store.
    protected = harness.store.path_for(session.session_id).read_text()
    assert SECRETS["authorization"] in protected and SECRETS["url"] in protected


def test_http_client_messages_are_replaced_by_the_structured_formatter() -> None:
    formatter = JsonLogFormatter()
    for name in ("httpx", "httpcore.http11", "h11"):
        record = logging.LogRecord(
            name,
            logging.WARNING,
            __file__,
            1,
            "headers [(b'Set-Cookie', b'%s')]",
            (SECRETS["set_cookie"],),
            None,
        )
        rendered = json.loads(formatter.format(record))
        assert rendered["message"] == REDACTED_TRANSPORT_DETAIL
        assert SECRETS["set_cookie"] not in json.dumps(rendered)


def test_structured_logging_floors_http_client_loggers_at_warning() -> None:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        configure_structured_logging(logging.DEBUG)
        assert logging.getLogger("httpcore.http11").getEffectiveLevel() == logging.WARNING
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


async def test_operator_ui_never_shows_exception_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    harness = await build(tmp_path)
    session = await add_session(harness)

    class _ExplodingMonitor:
        async def check(self, _: QueueSession) -> object:
            raise RuntimeError(f"direct failed at {SECRETS['url']} {SECRETS['exception']}")

    class _Target:
        def adjust_target(self, delta: int) -> None:
            return None

    manager = OperatorActionManager(
        repository=harness.repository,
        creator=cast(Any, object()),
        monitor=cast(Any, _ExplodingMonitor()),
        state_store=harness.state_store,
        target_adjustment=_Target(),
        worker_count=1,
        queue_capacity=2,
        lease_seconds=30,
        session_cleanup=harness.store.delete,
    )
    await manager.start()
    await manager.request(OperatorActionKind.REFRESH, session.session_id)
    for _ in range(200):
        action = manager.for_session(session.session_id)
        if action is not None and action.status is OperatorActionStatus.FAILED:
            break
        await asyncio.sleep(0.01)
    await manager.close(timeout_seconds=5)

    assert action is not None
    assert action.message == "Refresh failed"
    assert _no_secret(_formatted(caplog)) == []
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None and persisted.queue_id == session.queue_id
    await harness.repository.close()


# ---------------------------------------------------------- SQLite metadata only


async def test_sqlite_holds_only_non_secret_direct_metadata(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    handler = DirectMonitoringHandler(
        browser_monitor=harness.monitor,
        checker=harness.checker,
        store=harness.store,
        metadata=harness.repository,
        clock=lambda: NOW,
    )
    session = await add_session(harness)
    record = await harness.store.load(session.session_id)
    assert record is not None

    await handler.check(session)
    status = await harness.repository.get_direct_monitor_status(session.session_id)

    assert status is not None
    assert status.capability == "DIRECT_CAPABLE"
    assert status.last_reason is None
    assert status.recipe_reference == recipe_reference(record.recipe)
    assert status.recipe_reference is not None and status.recipe_reference.startswith("r1-")
    assert record.recipe is not None and record.recipe.fingerprint not in status.recipe_reference
    assert status.last_success_at == NOW and status.last_failure_at is None
    with sqlite3.connect(tmp_path / "direct.sqlite3") as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(direct_monitor_status)")
        }
    assert columns == {
        "session_id",
        "capability",
        "last_reason",
        "recipe_reference",
        "consecutive_failures",
        "last_success_at",
        "last_failure_at",
        "updated_at",
    }
    assert await harness.repository.direct_capability_counts() == {"DIRECT_CAPABLE": 1}
    await harness.repository.close()


async def test_metadata_records_failures_and_follows_session_lifecycle(tmp_path: Path) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/elsewhere"})

    harness = await build(tmp_path, fail, harvest=False)
    handler = DirectMonitoringHandler(
        browser_monitor=harness.monitor,
        checker=harness.checker,
        store=harness.store,
        metadata=harness.repository,
        clock=lambda: NOW,
    )
    session = await add_session(harness)
    await add_session(harness, "s-2", capable=False)

    await handler.check(session)

    status = await harness.repository.get_direct_monitor_status(session.session_id)
    assert status is not None
    assert status.capability == "DIRECT_UNAVAILABLE"
    assert status.last_reason == DirectFallbackReason.UNEXPECTED_REDIRECT.value
    assert status.last_failure_at == NOW and status.consecutive_failures == 1
    assert await harness.repository.direct_capability_counts() == {
        "DIRECT_UNAVAILABLE": 1,
        "DISCOVERY_REQUIRED": 1,
    }
    claimed = await harness.repository.acquire_operator_lease(
        session.session_id,
        worker_id="operator-request-1",
        now=NOW,
        lease_until=NOW + timedelta(minutes=1),
    )
    assert await harness.repository.delete_owned_session(
        claimed.session_id, worker_id="operator-request-1"
    )
    assert await harness.repository.get_direct_monitor_status(session.session_id) is None
    await harness.repository.reset_all()
    assert await harness.repository.direct_capability_counts() == {}
    await harness.repository.close()


async def test_metadata_database_failure_never_fails_a_valid_check(tmp_path: Path) -> None:
    class _Broken:
        async def record_direct_monitor_status(self, _: object) -> bool:
            raise sqlite3.OperationalError("database is locked")

        async def direct_capability_counts(self) -> dict[str, int]:
            return {}

    harness = await build(tmp_path)
    handler = DirectMonitoringHandler(
        browser_monitor=harness.monitor,
        checker=harness.checker,
        store=harness.store,
        metadata=_Broken(),
    )
    session = await add_session(harness)

    outcome = await handler.check(session)

    assert outcome.success is True and harness.restorer.calls == []
    assert handler.metrics.metadata_failures == 1
    await harness.repository.close()


# ------------------------------------------------------------------- metrics


async def test_direct_metrics_are_recorded_without_session_labels(tmp_path: Path) -> None:
    responses = iter(["ok", "unknown", "mismatch", "ok"])

    def respond(request: httpx.Request) -> httpx.Response:
        queue_id = request.url.params["q"]
        kind = next(responses)
        if kind == "unknown":
            # Direct evaluates CONNECTION_LOST; the browser says ACTIVE_QUEUE: a
            # disagreement. (A matching Queue ID alone would now be accepted.)
            return httpx.Response(
                200,
                json=status_document(
                    queue_id, activeQueue=False, usersAhead=None, connectionLost=True
                ),
            )
        if kind == "mismatch":
            return httpx.Response(200, json=status_document("queue-other"))
        return httpx.Response(200, json=status_document(queue_id))

    metrics = PrometheusMetrics()
    harness = await build(tmp_path, respond, harvest=False)
    handler = DirectMonitoringHandler(
        browser_monitor=harness.monitor,
        checker=harness.checker,
        store=harness.store,
        observability=metrics,
    )
    session = await add_session(harness)
    metrics.set_monitoring_strategy("direct")
    for _ in range(3):
        current = await harness.repository.get(session.session_id)
        assert current is not None
        await handler.check(current)

    sample = metrics.registry.get_sample_value
    assert sample("direct_monitoring_attempts_total") == 3
    assert sample("direct_monitoring_successes_total") == 1
    assert sample("direct_monitoring_fallbacks_total", {"reason": "unknown_lifecycle"}) == 1
    assert sample("direct_monitoring_fallbacks_total", {"reason": "identity_mismatch"}) == 1
    assert sample("direct_monitoring_disagreements_total") == 1
    assert sample("direct_monitoring_identity_mismatches_total") == 1
    assert sample("direct_monitoring_request_duration_seconds_count") == 3
    assert sample("direct_monitoring_fallback_duration_seconds_count") == 2
    assert sample("monitoring_strategy_info", {"strategy": "direct"}) == 1
    exposition = metrics.render().decode()
    assert str(session.queue_id) not in exposition and session.session_id not in exposition
    await harness.repository.close()


# ---------------------------------------------------------- failure hardening


async def test_expired_cookies_are_not_replayed_and_rejection_falls_back(
    tmp_path: Path,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "visitor=" not in request.headers.get("cookie", ""):
            return httpx.Response(403, text="no visitor")
        return httpx.Response(200, json=status_document(request.url.params["q"]))

    harness = await build(tmp_path, respond, harvest=False)
    session = await add_session(harness)
    assert session.queue_id is not None
    await harness.state_store.save(
        session.session_id,
        {
            "cookies": [
                {
                    "name": "visitor",
                    "value": "expired",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": 1_000,
                },
            ],
            "origins": [],
        },
    )

    await harness.handler.check(session)

    assert "visitor=" not in harness.requests[0].headers.get("cookie", "")
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.REJECTED_SESSION_STATE.value: 1
    }
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None and persisted.queue_id == session.queue_id
    await harness.repository.close()


async def test_corrupt_browser_state_is_missing_visitor_state_without_request(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path, harvest=False)
    session = await add_session(harness)
    harness.state_store.path_for(session.session_id).write_text("{corrupt", encoding="utf-8")

    await harness.handler.check(session)

    assert harness.requests == []
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.MISSING_VISITOR_STATE.value: 1
    }
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None and persisted.queue_id == session.queue_id
    await harness.repository.close()


async def test_corrupt_replay_cookies_fall_back_to_browser_state(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness)
    cookie_path = harness.store.cookie_path(session.session_id)
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text("{corrupt", encoding="utf-8")

    outcome = await harness.handler.check(session)

    assert outcome.success is True and harness.restorer.calls == []
    assert "visitor=" in harness.requests[0].headers.get("cookie", "")
    await harness.repository.close()


@pytest.mark.parametrize("code", [400, 404, 409, 429, 502, 503])
async def test_server_4xx_5xx_are_soft_http_failures(tmp_path: Path, code: int) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(code, text="nope")

    harness = await build(tmp_path, respond, harvest=False)
    session = await add_session(harness)

    await harness.handler.check(session)

    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.UNEXPECTED_HTTP_STATUS.value: 1
    }
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    await harness.repository.close()


async def test_redirect_loop_is_never_followed(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    harness = await build(tmp_path, respond, harvest=False)
    session = await add_session(harness)

    await harness.handler.check(session)

    assert len(harness.requests) == 1
    assert harness.handler.metrics.fallback_reasons == {
        DirectFallbackReason.UNEXPECTED_REDIRECT.value: 1
    }
    await harness.repository.close()


@pytest.mark.parametrize(
    "error", [httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError, httpx.PoolTimeout]
)
async def test_network_interruption_is_classified_and_falls_back(
    tmp_path: Path, error: type[httpx.TransportError]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error(f"interrupted {SECRETS['exception']}", request=request)

    harness = await build(tmp_path, respond, harvest=False)
    session = await add_session(harness)

    outcome = await harness.handler.check(session)

    reason = (
        DirectFallbackReason.TIMEOUT
        if issubclass(error, httpx.TimeoutException)
        else DirectFallbackReason.NETWORK
    )
    assert harness.handler.metrics.fallback_reasons == {reason.value: 1}
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    await harness.repository.close()


async def _hanging_harness(tmp_path: Path) -> tuple[Harness, asyncio.Event]:
    started = asyncio.Event()

    class _Hang(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    harness = await build(tmp_path)
    harness.checker._transport = _Hang()
    return harness, started


async def test_cancellation_during_direct_request_leaves_state_untouched(
    tmp_path: Path,
) -> None:
    harness, started = await _hanging_harness(tmp_path)
    session = await add_session(harness)
    before = harness.store.path_for(session.session_id).read_bytes()

    task = asyncio.create_task(harness.handler.check(session))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.store.path_for(session.session_id).read_bytes() == before
    assert harness.restorer.calls == []
    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None
    assert persisted.queue_id == session.queue_id and persisted.status is session.status
    await harness.repository.close()


async def test_shutdown_during_a_real_direct_request_releases_the_lease(tmp_path: Path) -> None:
    harness, started = await _hanging_harness(tmp_path)
    await add_session(harness)
    scheduler = scheduler_for(harness, worker_count=1, queue_capacity=1, claim_batch_size=1)

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await started.wait()
    await scheduler.shutdown(timeout_seconds=0.2)

    persisted = await harness.repository.get("s-1")
    assert persisted is not None
    assert persisted.queue_id == "queue-s-1" and persisted.worker_id is None
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    await harness.repository.close()


async def test_record_save_failure_after_success_keeps_the_observation(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness)
    record = await harness.store.load(session.session_id)
    assert record is not None
    await harness.store.record_failure(
        record, reason=DirectFallbackReason.TIMEOUT, make_unavailable=False
    )

    async def broken_save(_: object) -> Path:
        raise OSError("disk full")

    harness.store.save = broken_save  # type: ignore[method-assign, assignment]
    outcome = await harness.handler.check(session)

    assert outcome.success is True and harness.restorer.calls == []
    progress = await harness.repository.get_progress(session.session_id)
    assert progress is not None and progress.progress_percentage == 55
    await harness.repository.close()


async def test_cookie_persistence_failure_during_replay_falls_back(tmp_path: Path) -> None:
    harness = await build(tmp_path, harvest=False)
    session = await add_session(harness)

    async def broken(**_: object) -> Path:
        raise OSError("read-only filesystem")

    harness.store.cookies.save = broken  # type: ignore[method-assign, assignment]
    outcome = await harness.handler.check(session)

    assert harness.handler.metrics.fallback_reasons == {DirectFallbackReason.UNCERTAIN.value: 1}
    assert outcome.observed_status is QueueStatus.ACTIVE_QUEUE
    await harness.repository.close()


class _FlakyUpdateRepository(SQLiteSessionRepository):
    failures = 1

    async def update(self, session: QueueSession, progress: object = None) -> QueueSession:
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return await super().update(session, progress)  # type: ignore[arg-type]


async def test_database_failure_after_direct_success_reparks_and_keeps_identity(
    tmp_path: Path,
) -> None:
    harness = await build(tmp_path)
    await add_session(harness)
    await harness.repository.close()
    flaky = _FlakyUpdateRepository(tmp_path / "direct.sqlite3")
    harness.monitor._repository = flaky
    harness.repository = flaky
    scheduler = scheduler_for(harness, worker_count=1, queue_capacity=1, claim_batch_size=1)

    await scheduler.start()
    assert await scheduler.schedule_due() == 1
    await scheduler.wait_until_idle()
    await scheduler.shutdown()

    persisted = await flaky.get("s-1")
    assert persisted is not None
    assert persisted.queue_id == "queue-s-1"
    assert persisted.last_error == "monitor:worker_failure"
    assert persisted.worker_id is None
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    assert harness.restorer.calls == []  # a DB failure is not a direct failure
    await flaky.close()


async def test_fallback_browser_failure_after_direct_failure_keeps_identity(
    tmp_path: Path,
) -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    harness = await build(tmp_path, fail, restorer=FakeRestorer(raise_error=True))
    session = await add_session(harness)

    with pytest.raises(RuntimeError, match="synthetic browser crash"):
        await harness.handler.check(session)

    persisted = await harness.repository.get(session.session_id)
    assert persisted is not None and persisted.queue_id == session.queue_id
    record = await harness.store.load(session.session_id)
    assert record is not None and record.last_reason is DirectFallbackReason.UNEXPECTED_HTTP_STATUS
    await harness.repository.close()


# ------------------------------------------------ re-adoption and retention


async def test_readoption_cooldown_bounds_direct_fallback_churn(tmp_path: Path) -> None:
    now = {"value": datetime.now(UTC)}

    def fail(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=status_document("queue-other"))

    harness = await build(tmp_path, fail)
    handler = DirectMonitoringHandler(
        browser_monitor=harness.monitor,
        checker=harness.checker,
        store=harness.store,
        harvester=harness.handler._harvester,
        readopt_cooldown_seconds=300,
        clock=lambda: now["value"],
    )
    session = await add_session(harness)

    await handler.check(session)
    assert await capability(harness) is DirectCapability.DIRECT_UNAVAILABLE
    assert handler.metrics.recipe_refresh_deferred == 1

    current = await harness.repository.get(session.session_id)
    assert current is not None
    await handler.check(current)
    assert len(harness.requests) == 1  # still unavailable: no direct attempt
    assert await capability(harness) is DirectCapability.DIRECT_UNAVAILABLE

    now["value"] += timedelta(seconds=301)
    current = await harness.repository.get(session.session_id)
    assert current is not None
    await handler.check(current)
    assert await capability(harness) is DirectCapability.DIRECT_CAPABLE
    assert handler.metrics.recipes_adopted == 1
    await harness.repository.close()


async def test_discovery_retention_keeps_only_the_newest_artifacts(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    session = await add_session(harness, capable=False)
    for _ in range(6):
        write_discovery_artifact(
            harness.evidence,
            session,
            scope="local_simulator",
            response=status_document(str(session.queue_id)),
            url=f"http://127.0.0.1:9/status?q={session.queue_id}",
        )
        await asyncio.sleep(0.002)
    other = await add_session(harness, "s-2", capable=False)
    kept_other = write_discovery_artifact(
        harness.evidence,
        other,
        scope="local_simulator",
        response=status_document(str(other.queue_id)),
        url=f"http://127.0.0.1:9/status?q={other.queue_id}",
    )
    newest = sorted(harness.evidence.glob(f"*-{session.session_id}.json"))[-2:]
    harvester = harness.handler._harvester
    assert harvester is not None

    removed = await harvester.prune(session_id=session.session_id, keep=2)

    assert removed == 4
    assert sorted(harness.evidence.glob(f"*-{session.session_id}.json")) == newest
    assert kept_other.exists()
    await harness.repository.close()


# ------------------------------------------------ report-only consistency


async def test_consistency_report_finds_problems_and_changes_nothing(tmp_path: Path) -> None:
    harness = await build(tmp_path)
    good = await add_session(harness, "good")
    corrupt = await add_session(harness, "corrupt")
    mismatch = await add_session(harness, "mismatch")
    await harness.repository.record_direct_monitor_status(
        DirectMonitorStatus(
            session_id=good.session_id, capability="DIRECT_UNAVAILABLE", updated_at=NOW
        )
    )
    path = harness.store.path_for(corrupt.session_id)
    path.write_text(path.read_text().replace("DIRECT_CAPABLE", "DISCOVERY_REQUIRED"))
    other = QueueSession(
        session_id=mismatch.session_id,
        queue_id="queue-previous",
        transfer_url=mismatch.transfer_url,
        mode=mismatch.mode,
        state_path=mismatch.state_path,
    )
    await harness.store.adopt_recipe(
        session_id=mismatch.session_id,
        expected_queue_id="queue-previous",
        recipe=recipe_for(other),
    )
    orphan = DirectMonitorStateStore(harness.store.directory)
    ghost = QueueSession(
        session_id="ghost",
        queue_id="queue-ghost",
        transfer_url="http://127.0.0.1:9/queue?q=queue-ghost",
        mode=good.mode,
        state_path=good.state_path,
    )
    await orphan.adopt_recipe(
        session_id="ghost", expected_queue_id="queue-ghost", recipe=recipe_for(ghost)
    )
    await harness.store.cookies.save(session_id="nobody", recipe_fingerprint="x", cookies=())
    loose = harness.store.path_for(good.session_id)
    loose.chmod(0o644)
    snapshot = {
        path: path.read_bytes() for path in harness.store.directory.rglob("*") if path.is_file()
    }

    report = await DirectMonitorConsistencyChecker(harness.repository, harness.store).check()
    result = report.to_dict()
    await harness.repository.close()
    cli = await run_consistency_check(
        tmp_path / "direct.sqlite3", tmp_path / "state", harness.store.directory
    )

    kinds = {(finding.kind, finding.session_id) for finding in report.findings}
    assert (DirectMonitorFindingKind.METADATA_MISMATCH, "good") in kinds
    assert (DirectMonitorFindingKind.CORRUPT_RECORD, "corrupt") in kinds
    assert (DirectMonitorFindingKind.IDENTITY_MISMATCH, "mismatch") in kinds
    assert (DirectMonitorFindingKind.ORPHAN_RECORD, "ghost") in kinds
    assert (DirectMonitorFindingKind.ORPHAN_COOKIES, "nobody") in kinds
    assert (DirectMonitorFindingKind.UNSAFE_PERMISSIONS, "good") in kinds
    assert result["repairs_performed"] == 0 and result["consistent"] is False
    assert cli["direct_monitor"] == result
    after = {path: path.read_bytes() for path in harness.store.directory.rglob("*") if path.is_file()}
    assert after == snapshot  # report-only
    assert stat.S_IMODE(loose.stat().st_mode) == 0o644
    assert "queue-previous" not in json.dumps(result)


def test_version_one_records_remain_readable(tmp_path: Path) -> None:
    import hashlib

    store = DirectMonitorStateStore(tmp_path)
    document: dict[str, object] = {
        "format": "queue-load-test.phase8-direct-monitor-record",
        "version": 1,
        "session_id": "s-1",
        "expected_queue_id": "queue-s-1",
        "capability": "DISCOVERY_REQUIRED",
        "updated_at": NOW.isoformat(),
        "last_reason": None,
        "consecutive_failures": 0,
        "recipe": None,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    document["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    path = store.path_for("s-1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document))

    record = asyncio.run(store.load("s-1"))

    assert record is not None and record.unavailable_since is None


def test_accepted_scopes_are_unchanged_for_real_targets() -> None:
    assert accepted_evidence_scopes("https://queue.example.test/") == frozenset(
        {"authorized_queue_it_staging"}
    )

