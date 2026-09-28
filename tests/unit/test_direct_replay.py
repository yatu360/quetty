from __future__ import annotations

import asyncio
import json
import logging
import stat
from pathlib import Path

import httpx
import pytest

from queue_load_test.direct_replay import (
    DirectStatusReplayClient,
    HeaderProfile,
    ProtectedReplayStateStore,
    ReplayCookie,
    ReplayEvidenceError,
    ReplayFailure,
    ReplayRecipe,
    ReplayStateError,
    ValueStability,
    classify_artifact_values,
    cookies_from_browser_state,
    load_replay_recipe,
)
from queue_load_test.metrics.logging import JsonLogFormatter

QUEUE_ID = "queue-secret-identity"
SESSION_ID = "session-one"
URL = f"https://fixture.invalid/visitor-status?queueId={QUEUE_ID}&poll=one"


def _artifact(*, bodies: tuple[str, ...] = ('{"poll":"one"}',)) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sensitivity": "SENSITIVE_VISITOR_CREDENTIAL_EVIDENCE",
        "scope": "authorized_queue_it_staging",
        "session_id": SESSION_ID,
        "expected_queue_id": QUEUE_ID,
        "exchanges": [
            {
                "sequence": index,
                "url": URL.replace("poll=one", f"poll={index}"),
                "method": "POST",
                "classification": "dom_correlated_status_candidate",
                "request_headers": {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Cookie": "browser-secret=do-not-copy",
                    "Authorization": "Bearer protected-token",
                    "Sec-Fetch-Mode": "cors",
                    "X-Visitor-Proof": "protected-proof",
                },
                "request_body": body,
                "request_body_truncated": False,
                "observed_identifiers": {"queue_id": [QUEUE_ID]},
            }
            for index, body in enumerate(bodies, start=1)
        ],
    }


def _write_artifact(tmp_path: Path, artifact: dict[str, object] | None = None) -> Path:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(artifact or _artifact()), encoding="utf-8")
    return path


def _recipe(tmp_path: Path, *, bodies: tuple[str, ...] = ('{"poll":"one"}',), sequence: int = 1) -> ReplayRecipe:
    return load_replay_recipe(
        _write_artifact(tmp_path, _artifact(bodies=bodies)),
        session_id=SESSION_ID,
        expected_queue_id=QUEUE_ID,
        exchange_sequence=sequence,
    )


def _cookies(value: str = "browser-cookie") -> tuple[ReplayCookie, ...]:
    return (ReplayCookie("visitor", value, "fixture.invalid", "/", True),)


async def _replay(
    tmp_path: Path,
    handler: object,
    *,
    profile: HeaderProfile = HeaderProfile.FULL_DERIVED,
    recipe: ReplayRecipe | None = None,
    cookies: tuple[ReplayCookie, ...] | None = None,
    timeout: float = 1.0,
) -> tuple[object, ProtectedReplayStateStore]:
    store = ProtectedReplayStateStore(tmp_path / "replay-state")
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    async with DirectStatusReplayClient(
        recipe=recipe or _recipe(tmp_path),
        cookies=cookies or _cookies(),
        state_store=store,
        timeout_seconds=timeout,
        transport=transport,
    ) as client:
        return await client.replay(profile), store


def test_recipe_uses_exact_selected_capture_and_rejects_identity_or_scope(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path, bodies=('{"poll":"one"}', '{"poll":"two"}'), sequence=2)
    assert recipe.url.endswith("poll=2")
    assert recipe.body == b'{"poll":"two"}'

    with pytest.raises(ReplayEvidenceError, match="another session"):
        load_replay_recipe(
            _write_artifact(tmp_path),
            session_id="different",
            expected_queue_id=QUEUE_ID,
            exchange_sequence=1,
        )
    local = _artifact()
    local["scope"] = "local_simulator"
    with pytest.raises(ReplayEvidenceError, match="authorized"):
        load_replay_recipe(
            _write_artifact(tmp_path, local),
            session_id=SESSION_ID,
            expected_queue_id=QUEUE_ID,
            exchange_sequence=1,
        )


def test_storage_state_cookie_validation() -> None:
    cookies = cookies_from_browser_state(
        {
            "cookies": [
                {
                    "name": "visitor",
                    "value": "secret",
                    "domain": "fixture.invalid",
                    "path": "/",
                    "secure": True,
                    "expires": 12345,
                }
            ],
            "origins": [],
        }
    )
    assert len(cookies) == 1
    with pytest.raises(ReplayEvidenceError, match="cookie list"):
        cookies_from_browser_state({"origins": []})


def test_state_classification_requires_repeated_evidence(tmp_path: Path) -> None:
    artifact = _artifact(bodies=('{"poll":"one"}', '{"poll":"two"}', '{"poll":"three"}'))
    exchanges = artifact["exchanges"]
    assert isinstance(exchanges, list)
    for index, exchange in enumerate(exchanges, start=1):
        assert isinstance(exchange, dict)
        exchange["query"] = [["poll", str(index)]]
        exchange["request_json"] = {"poll": index}
    exchanges[-1]["set_cookie"] = ["visitor=refreshed-secret"]
    path = _write_artifact(tmp_path, artifact)
    findings = {item.value: item.classification for item in classify_artifact_values(path)}
    assert findings["host/path"] is ValueStability.UNKNOWN
    assert findings["Queue ID"] is ValueStability.SESSION_STABLE
    assert findings["request body fields"] is ValueStability.REQUEST_TRANSIENT
    assert findings["query parameters"] is ValueStability.REQUEST_TRANSIENT
    assert findings["response cookies"] is ValueStability.RESPONSE_REFRESHED


@pytest.mark.asyncio
async def test_replay_uses_storage_cookies_exact_body_query_and_filtered_headers(
    tmp_path: Path,
) -> None:
    observed: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["cookie"] = request.headers.get("cookie", "")
        observed["body"] = (await request.aread()).decode()
        observed["query"] = request.url.query.decode()
        observed["authorization"] = request.headers.get("authorization", "")
        observed["visitor_proof"] = request.headers.get("x-visitor-proof", "")
        observed["sec_fetch"] = request.headers.get("sec-fetch-mode", "")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"queueId": QUEUE_ID, "progress": 42},
        )

    result, _ = await _replay(tmp_path, handler)
    assert result.succeeded and result.identity_confirmed
    assert observed == {
        "cookie": "visitor=browser-cookie",
        "body": '{"poll":"one"}',
        "query": f"queueId={QUEUE_ID}&poll=1",
        "authorization": "Bearer protected-token",
        "visitor_proof": "protected-proof",
        "sec_fetch": "",
    }


@pytest.mark.asyncio
async def test_minimal_profile_discards_nonminimal_headers_only_after_explicit_selection(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert "x-visitor-proof" not in request.headers
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, headers={"content-type": "application/json"}, json={})

    result, _ = await _replay(tmp_path, handler, profile=HeaderProfile.MINIMAL)
    assert result.succeeded
    assert not result.identity_confirmed


@pytest.mark.asyncio
async def test_cookie_updates_are_protected_and_survive_client_restart(tmp_path: Path) -> None:
    recipe = _recipe(tmp_path)
    calls = 0

    async def first_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "visitor=browser-cookie" in request.headers["cookie"]
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "set-cookie": "visitor=refreshed-secret; Path=/; Secure",
            },
            json={"queueId": QUEUE_ID},
        )

    result, store = await _replay(tmp_path, first_handler, recipe=recipe)
    assert result.succeeded and result.cookies_changed
    path = store.path_for(SESSION_ID)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert (path.parent / ".gitignore").read_text() == "*\n"
    persisted = await store.load(session_id=SESSION_ID, recipe_fingerprint=recipe.fingerprint)
    assert persisted is not None

    async def restarted_handler(request: httpx.Request) -> httpx.Response:
        assert "visitor=refreshed-secret" in request.headers["cookie"]
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json={"queueId": QUEUE_ID}
        )

    restarted, _ = await _replay(
        tmp_path,
        restarted_handler,
        recipe=recipe,
        cookies=persisted,
    )
    assert restarted.succeeded
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(302, headers={"location": "https://fixture.invalid/next"}), ReplayFailure.REDIRECT),
        (httpx.Response(403, json={"error": "expired"}), ReplayFailure.STATE),
        (httpx.Response(500, json={"error": "failed"}), ReplayFailure.HTTP),
        (httpx.Response(200, headers={"content-type": "text/plain"}, text="no"), ReplayFailure.SCHEMA),
        (httpx.Response(200, headers={"content-type": "application/json"}, text="{"), ReplayFailure.SCHEMA),
        (
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"queueId": "different-identity"},
            ),
            ReplayFailure.IDENTITY,
        ),
    ],
)
async def test_failure_classification(
    tmp_path: Path, response: httpx.Response, expected: ReplayFailure
) -> None:
    result, _ = await _replay(tmp_path, lambda _request: response)
    assert not result.succeeded
    assert result.failure is expected


@pytest.mark.asyncio
async def test_response_body_is_bounded(tmp_path: Path) -> None:
    result, _ = await _replay(
        tmp_path,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'"' + (b"x" * 70_000) + b'"',
        ),
    )
    assert result.failure is ReplayFailure.SCHEMA


@pytest.mark.asyncio
async def test_timeout_and_network_failures(tmp_path: Path) -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret detail", request=request)

    timeout, _ = await _replay(tmp_path, timeout_handler)
    assert timeout.failure is ReplayFailure.TIMEOUT

    def network_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret detail", request=request)

    network, _ = await _replay(tmp_path, network_handler)
    assert network.failure is ReplayFailure.NETWORK


@pytest.mark.asyncio
async def test_replay_cancellation_closes_client(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    store = ProtectedReplayStateStore(tmp_path / "state")
    client = DirectStatusReplayClient(
        recipe=_recipe(tmp_path),
        cookies=_cookies(),
        state_store=store,
        transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(client.replay())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()


@pytest.mark.asyncio
async def test_protected_state_rejects_recipe_mismatch(tmp_path: Path) -> None:
    store = ProtectedReplayStateStore(tmp_path / "state")
    await store.save(session_id=SESSION_ID, recipe_fingerprint="one", cookies=_cookies())
    with pytest.raises(ReplayStateError, match="does not match"):
        await store.load(session_id=SESSION_ID, recipe_fingerprint="two")


@pytest.mark.asyncio
async def test_normal_logging_suppresses_sensitive_replay_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="queue_load_test.direct_replay.client")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    replay_logger = logging.getLogger("queue_load_test.direct_replay.client")
    replay_logger.addHandler(handler)
    try:
        result, _ = await _replay(
            tmp_path,
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"queueId": QUEUE_ID},
            ),
        )
    finally:
        replay_logger.removeHandler(handler)
    assert result.succeeded
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "direct_replay_attempt_completed" in log_text
    for secret in (QUEUE_ID, SESSION_ID, URL, "protected-token", "browser-cookie"):
        assert secret not in log_text
