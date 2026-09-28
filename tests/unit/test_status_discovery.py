from __future__ import annotations

import asyncio
import io
import json
import logging
import stat
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from queue_load_test.metrics.logging import JsonLogFormatter
from queue_load_test.status_discovery import DiscoveryDomSnapshot, StatusDiscoveryFactory


class FakePage:
    def __init__(self) -> None:
        self.listeners: dict[str, list[Any]] = defaultdict(list)

    def on(self, event: str, callback: Any) -> None:
        self.listeners[event].append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        self.listeners[event].remove(callback)

    def emit(self, event: str, value: object) -> None:
        for callback in tuple(self.listeners[event]):
            callback(value)


class FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        resource_type: str = "fetch",
        headers: dict[str, str] | None = None,
        post_data: str | None = None,
        redirected_from: FakeRequest | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {}
        self.post_data = post_data
        self.redirected_from = redirected_from
        self.redirected_to: FakeRequest | None = None
        self.failure: str | None = None
        if redirected_from is not None:
            redirected_from.redirected_to = self

    async def all_headers(self) -> dict[str, str]:
        return self.headers


class FakeResponse:
    def __init__(
        self,
        request: FakeRequest,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        set_cookie: list[str] | None = None,
        block: asyncio.Event | None = None,
    ) -> None:
        self.request = request
        self.status = status
        self._headers = headers or {}
        self._body = body
        self._headers.setdefault("content-length", str(len(body)))
        self._set_cookie = set_cookie or []
        self._block = block

    async def all_headers(self) -> dict[str, str]:
        return self._headers

    async def header_values(self, name: str) -> list[str]:
        return self._set_cookie if name.casefold() == "set-cookie" else []

    async def body(self) -> bytes:
        if self._block is not None:
            await self._block.wait()
        return self._body


class FakeContext:
    def __init__(self) -> None:
        self.cookie_version = 0

    async def cookies(self, _: str) -> list[dict[str, object]]:
        self.cookie_version += 1
        return [
            {
                "name": "visitor-secret",
                "value": f"credential-{self.cookie_version}",
                "domain": "queue.example.test",
                "path": "/",
            }
        ]


async def snapshot() -> DiscoveryDomSnapshot:
    return DiscoveryDomSnapshot(
        observed_at="2026-09-29T12:00:00+00:00",
        page_url="https://queue.example.test/wait",
        lifecycle_status="ACTIVE_QUEUE",
        progress_percentage=42.0,
        users_ahead=10,
    )


async def test_captures_requests_responses_redirects_cookies_bodies_and_cadence(
    tmp_path: Path,
) -> None:
    page = FakePage()
    context = FakeContext()
    factory = StatusDiscoveryFactory(
        evidence_directory=tmp_path / "protected",
        scope="local_simulator",
        max_exchanges=10,
        max_body_bytes=1024,
        event_queue_capacity=10,
    )
    observation = factory.create(
        page=page,
        context=context,
        session_id="session-1",
        expected_queue_id="queue-secret",
        dom_snapshot_provider=snapshot,
    )

    async with observation:
        first = FakeRequest(
            "https://queue.example.test/runtime/refresh?nonce=one&queueId=queue-secret",
            method="POST",
            headers={"content-type": "application/json", "authorization": "secret-token"},
            post_data='{"customerId":"customer-a","eventId":"event-a","nonce":"one"}',
        )
        redirect = FakeRequest(
            "https://queue.example.test/runtime/refresh?nonce=two&queueId=queue-secret",
            method="POST",
            headers={"content-type": "application/json"},
            post_data='{"customerId":"customer-a","eventId":"event-a","nonce":"two"}',
            redirected_from=first,
        )
        for request, nonce in ((first, "one"), (redirect, "two")):
            page.emit("request", request)
            page.emit(
                "response",
                FakeResponse(
                    request,
                    status=307 if request is first else 200,
                    headers={"content-type": "application/json", "x-evidence": nonce},
                    body=(
                        b'{"status":"active","progressPercentage":42.0,'
                        b'"usersAhead":10,"tokenIdentifier":"rotated-token"}'
                    ),
                    set_cookie=[f"visitor-cookie={nonce}; Path=/; Secure"],
                ),
            )
            page.emit("requestfinished", request)
        await asyncio.sleep(0)

    artifacts = list((tmp_path / "protected").glob("*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text())
    assert stat.S_IMODE(artifacts[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(artifacts[0].parent.stat().st_mode) == 0o700
    assert (artifacts[0].parent / ".gitignore").read_text() == "*\n"
    assert artifact["scope"] == "local_simulator"
    assert len(artifact["exchanges"]) == 2
    first_capture, second_capture = artifact["exchanges"]
    assert first_capture["url"] == first.url
    assert first_capture["scheme"] == "https"
    assert first_capture["host"] == "queue.example.test"
    assert first_capture["path"] == "/runtime/refresh"
    assert first_capture["query"] == [["nonce", "one"], ["queueId", "queue-secret"]]
    assert first_capture["method"] == "POST"
    assert first_capture["request_content_type"] == "application/json"
    assert first_capture["request_json"]["customerId"] == "customer-a"
    assert first_capture["response_status"] == 307
    assert first_capture["set_cookie"] == ["visitor-cookie=one; Path=/; Secure"]
    assert first_capture["cookies_after_response"][0]["value"] == "credential-1"
    assert first_capture["redirected_to_url"] == redirect.url
    assert second_capture["redirected_from_url"] == first.url
    assert second_capture["response_json"]["progressPercentage"] == 42.0
    assert second_capture["classification"] == "dom_correlated_status_candidate"
    assert second_capture["correlation_fields"] == ["progress_percentage", "users_ahead"]
    assert second_capture["observed_identifiers"] == {
        "customer_id": ["customer-a"],
        "event_id": ["event-a"],
        "queue_id": ["queue-secret"],
        "token_identifier": ["rotated-token"],
    }
    assert artifact["cadence"][0]["request_count"] == 2
    assert {item["result"] for item in artifact["findings"]} == {"UNKNOWN"}
    assert all(not listeners for listeners in page.listeners.values())


async def test_capture_is_bounded_and_body_is_truncated(tmp_path: Path) -> None:
    page = FakePage()
    factory = StatusDiscoveryFactory(
        evidence_directory=tmp_path,
        max_exchanges=2,
        max_body_bytes=8,
        event_queue_capacity=1,
    )
    observation = factory.create(
        page=page,
        context=FakeContext(),
        session_id="bounded",
        expected_queue_id=None,
        dom_snapshot_provider=snapshot,
    )
    async with observation:
        for index in range(5):
            request = FakeRequest(f"https://example.test/poll?n={index}")
            page.emit("request", request)
            page.emit(
                "response",
                FakeResponse(
                    request,
                    headers={"content-type": "application/json"},
                    body=b'{"long":"sensitive-value"}',
                ),
            )
            page.emit("requestfinished", request)

    artifact = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert len(artifact["exchanges"]) == 2
    assert artifact["dropped"]["exchanges"] == 3
    assert artifact["limits"] == {
        "event_queue_capacity": 1,
        "max_body_bytes": 8,
        "max_exchanges": 2,
    }
    captured_bodies = [item["response_body"] for item in artifact["exchanges"]]
    assert all(body is None or len(body.encode()) <= 8 for body in captured_bodies)


async def test_opt_in_observation_dwell_allows_periodic_requests_before_detach(
    tmp_path: Path,
) -> None:
    page = FakePage()
    observation = StatusDiscoveryFactory(
        evidence_directory=tmp_path,
        observe_seconds=0.02,
    ).create(
        page=page,
        context=FakeContext(),
        session_id="dwell",
        expected_queue_id=None,
        dom_snapshot_provider=snapshot,
    )
    started = asyncio.get_running_loop().time()

    async with observation:
        pass

    assert asyncio.get_running_loop().time() - started >= 0.018


async def test_cancellation_detaches_listeners_and_bounds_worker_cleanup(
    tmp_path: Path,
) -> None:
    page = FakePage()
    blocked = asyncio.Event()
    factory = StatusDiscoveryFactory(
        evidence_directory=tmp_path,
        cleanup_timeout_seconds=0.01,
        event_queue_capacity=1,
    )
    observation = factory.create(
        page=page,
        context=FakeContext(),
        session_id="cancelled",
        expected_queue_id=None,
        dom_snapshot_provider=snapshot,
    )

    with pytest.raises(asyncio.CancelledError):
        async with observation:
            request = FakeRequest("https://example.test/poll")
            page.emit("request", request)
            page.emit(
                "response",
                FakeResponse(
                    request,
                    headers={"content-type": "application/json"},
                    body=b"{}",
                    block=blocked,
                ),
            )
            await asyncio.sleep(0)
            raise asyncio.CancelledError

    artifact = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert artifact["cancelled"] is True
    assert all(not listeners for listeners in page.listeners.values())


async def test_normal_structured_log_suppresses_sensitive_evidence_values(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    capture_logger = logging.getLogger("queue_load_test.status_discovery.observer")
    original_handlers = capture_logger.handlers[:]
    original_propagate = capture_logger.propagate
    capture_logger.handlers = [handler]
    capture_logger.propagate = False
    capture_logger.setLevel(logging.INFO)
    try:
        page = FakePage()
        observation = StatusDiscoveryFactory(evidence_directory=tmp_path).create(
            page=page,
            context=FakeContext(),
            session_id="session-sensitive",
            expected_queue_id="queue-secret",
            dom_snapshot_provider=snapshot,
        )
        async with observation:
            request = FakeRequest(
                "https://secret.example.test/private?queueId=queue-secret",
                headers={"authorization": "bearer-secret"},
            )
            page.emit("request", request)
            page.emit(
                "response",
                FakeResponse(
                    request,
                    headers={"content-type": "application/json"},
                    body=b'{"credential":"sensitive-response"}',
                ),
            )
            page.emit("requestfinished", request)
    finally:
        capture_logger.handlers = original_handlers
        capture_logger.propagate = original_propagate

    normal_log = stream.getvalue()
    assert "status_discovery_capture_completed" in normal_log
    for secret in (
        "secret.example.test",
        "queue-secret",
        "bearer-secret",
        "sensitive-response",
        "session-sensitive",
        str(tmp_path),
    ):
        assert secret not in normal_log
