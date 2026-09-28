"""Bounded capture of network traffic produced by an already-active visitor page.

Raw artifacts are deliberately separate from normal persistence and observability.
They may contain credentials and are written only when the operator opts in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, cast
from urllib.parse import parse_qsl, urlsplit

from queue_load_test.metrics.logging import log_event

logger = logging.getLogger(__name__)

_CAPTURED_RESOURCE_TYPES = frozenset({"document", "xhr", "fetch"})
_IDENTIFIER_KEYS = {
    "customerid": "customer_id",
    "eventid": "event_id",
    "queueid": "queue_id",
    "tokenidentifier": "token_identifier",
}
_QUEUE_RESPONSE_KEYS = frozenset(
    {
        "progress",
        "progresspercentage",
        "queuenumber",
        "usersahead",
        "usersinlineaheadofyou",
        "status",
        "queuestatus",
        "expectedservicetime",
        "estimatedwait",
    }
)
_REDIRECT_RESPONSE_KEYS = frozenset(
    {"redirect", "redirecturl", "targeturl", "admitted", "turnstarted"}
)

type DomSnapshotProvider = Callable[[], Awaitable[DiscoveryDomSnapshot]]


@dataclass(frozen=True, slots=True)
class DiscoveryDomSnapshot:
    """Visible Queue-it state used only to correlate protected network evidence."""

    observed_at: str
    page_url: str = field(repr=False)
    lifecycle_status: str | None = None
    queue_number: str | None = None
    users_ahead: int | None = None
    progress_percentage: float | None = None
    estimated_wait_text: str | None = None
    expected_service_time: str | None = None
    queue_paused: bool | None = None
    serviced_soon: bool | None = None
    turn_started: bool | None = None
    pre_queue: bool | None = None
    active_queue: bool | None = None


class StatusDiscoveryFactoryProtocol(Protocol):
    def create(
        self,
        *,
        page: Any,
        context: Any,
        session_id: str,
        expected_queue_id: str | None,
        dom_snapshot_provider: DomSnapshotProvider,
    ) -> BrowserNetworkObservation: ...


@dataclass(slots=True)
class _Exchange:
    sequence: int
    request_started_at: float
    request_started_wall: str
    url: str = field(repr=False)
    scheme: str
    host: str = field(repr=False)
    path: str = field(repr=False)
    query: list[tuple[str, str]] = field(default_factory=list, repr=False)
    method: str = ""
    resource_type: str = ""
    request_headers: dict[str, str] = field(default_factory=dict, repr=False)
    request_content_type: str | None = None
    request_body: str | None = field(default=None, repr=False)
    request_body_truncated: bool = False
    request_json: object | None = field(default=None, repr=False)
    redirected_from_url: str | None = field(default=None, repr=False)
    redirected_to_url: str | None = field(default=None, repr=False)
    response_received_at: float | None = None
    completed_at: float | None = None
    duration_seconds: float | None = None
    response_status: int | None = None
    response_content_type: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict, repr=False)
    set_cookie: list[str] = field(default_factory=list, repr=False)
    cookies_after_response: list[dict[str, object]] = field(default_factory=list, repr=False)
    response_body: str | None = field(default=None, repr=False)
    response_json: object | None = field(default=None, repr=False)
    response_body_truncated: bool = False
    failure: str | None = None
    classification: str = "observed_navigation"
    correlation_fields: list[str] = field(default_factory=list)
    observed_identifiers: dict[str, list[str]] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class _ResponseWork:
    request_key: int
    response: Any
    received_at: float


class BrowserNetworkObservation:
    """One bounded network observation attached to a visitor page."""

    def __init__(
        self,
        *,
        page: Any,
        context: Any,
        session_id: str,
        expected_queue_id: str | None,
        evidence_path: Path,
        scope: str,
        dom_snapshot_provider: DomSnapshotProvider,
        max_exchanges: int,
        max_body_bytes: int,
        event_queue_capacity: int,
        cleanup_timeout_seconds: float,
        observe_seconds: float,
    ) -> None:
        self._page = page
        self._context = context
        self._session_id = session_id
        self._expected_queue_id = expected_queue_id
        self._evidence_path = evidence_path
        self._scope = scope
        self._dom_snapshot_provider = dom_snapshot_provider
        self._max_exchanges = max_exchanges
        self._max_body_bytes = max_body_bytes
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._observe_seconds = observe_seconds
        self._response_queue: asyncio.Queue[_ResponseWork | None] = asyncio.Queue(
            maxsize=event_queue_capacity
        )
        self._exchanges: list[_Exchange] = []
        self._request_indexes: dict[int, int] = {}
        self._worker: asyncio.Task[None] | None = None
        self._started_monotonic = time.monotonic()
        self._dropped_exchanges = 0
        self._dropped_response_events = 0
        self._finished = False

    async def __aenter__(self) -> Self:
        try:
            self._worker = asyncio.create_task(
                self._response_worker(),
                name="status-discovery-response-worker",
            )
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
            self._page.on("requestfinished", self._on_request_finished)
            self._page.on("requestfailed", self._on_request_failed)
        except Exception as exc:  # noqa: BLE001 - diagnostics cannot fail monitoring
            self._detach()
            if self._worker is not None:
                self._worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker
            log_event(
                logger,
                logging.WARNING,
                "status_discovery_capture_failed",
                operation="attach_browser_observer",
                error_type=type(exc).__name__,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.finish(cancelled=exc_type is asyncio.CancelledError)
        except asyncio.CancelledError:
            if exc_type is asyncio.CancelledError:
                return
            raise
        except Exception as finish_error:  # noqa: BLE001 - diagnostics cannot fail monitoring
            log_event(
                logger,
                logging.WARNING,
                "status_discovery_capture_failed",
                operation="finish_browser_observer",
                error_type=type(finish_error).__name__,
                count=len(self._exchanges),
            )

    def _on_request(self, request: Any) -> None:
        try:
            resource_type = str(request.resource_type)
            if resource_type not in _CAPTURED_RESOURCE_TYPES:
                return
            if len(self._exchanges) >= self._max_exchanges:
                self._dropped_exchanges += 1
                return
            url = str(request.url)
            parsed = urlsplit(url)
            headers = _string_mapping(request.headers)
            raw_body = request.post_data
            body = _bounded_text(raw_body, self._max_body_bytes)
            request_json = _parse_json(body)
            redirected_from = request.redirected_from
            exchange = _Exchange(
                sequence=len(self._exchanges) + 1,
                request_started_at=time.monotonic(),
                request_started_wall=datetime.now(UTC).isoformat(),
                url=url,
                scheme=parsed.scheme,
                host=parsed.netloc,
                path=parsed.path,
                query=list(parse_qsl(parsed.query, keep_blank_values=True)),
                method=str(request.method),
                resource_type=resource_type,
                request_headers=headers,
                request_content_type=_content_type(headers),
                request_body=body,
                request_body_truncated=(
                    raw_body is not None
                    and len(raw_body.encode("utf-8")) > self._max_body_bytes
                ),
                request_json=request_json,
                redirected_from_url=(
                    str(redirected_from.url) if redirected_from is not None else None
                ),
            )
            self._request_indexes[id(request)] = len(self._exchanges)
            self._exchanges.append(exchange)
        except Exception:  # noqa: BLE001 - diagnostics must never affect browser work
            self._dropped_exchanges += 1

    def _on_response(self, response: Any) -> None:
        request_key = id(response.request)
        if request_key not in self._request_indexes:
            return
        try:
            self._response_queue.put_nowait(
                _ResponseWork(request_key, response, time.monotonic())
            )
        except asyncio.QueueFull:
            self._dropped_response_events += 1

    def _on_request_finished(self, request: Any) -> None:
        exchange = self._exchange_for(request)
        if exchange is None:
            return
        completed = time.monotonic()
        exchange.completed_at = completed
        exchange.duration_seconds = completed - exchange.request_started_at
        redirected_to = request.redirected_to
        if redirected_to is not None:
            exchange.redirected_to_url = str(redirected_to.url)

    def _on_request_failed(self, request: Any) -> None:
        exchange = self._exchange_for(request)
        if exchange is None:
            return
        completed = time.monotonic()
        exchange.completed_at = completed
        exchange.duration_seconds = completed - exchange.request_started_at
        exchange.failure = str(request.failure or "request_failed")

    def _exchange_for(self, request: Any) -> _Exchange | None:
        index = self._request_indexes.get(id(request))
        return self._exchanges[index] if index is not None else None

    async def _response_worker(self) -> None:
        while True:
            work = await self._response_queue.get()
            try:
                if work is None:
                    return
                await self._capture_response(work)
            finally:
                self._response_queue.task_done()

    async def _capture_response(self, work: _ResponseWork) -> None:
        index = self._request_indexes.get(work.request_key)
        if index is None:
            return
        exchange = self._exchanges[index]
        response = work.response
        try:
            with contextlib.suppress(Exception):
                request_headers = _string_mapping(await response.request.all_headers())
                exchange.request_headers = request_headers
                exchange.request_content_type = _content_type(request_headers)
            headers = _string_mapping(await response.all_headers())
            exchange.response_received_at = work.received_at
            exchange.response_status = int(response.status)
            exchange.response_headers = headers
            exchange.response_content_type = _content_type(headers)
            with contextlib.suppress(Exception):
                exchange.set_cookie = [str(value) for value in await response.header_values(
                    "set-cookie"
                )]
            with contextlib.suppress(Exception):
                cookies = await self._context.cookies(exchange.url)
                exchange.cookies_after_response = [
                    _json_mapping(cookie) for cookie in cookies[:100]
                ]
            if _is_textual_content(exchange.response_content_type):
                content_length = _content_length(headers)
                if content_length is None or content_length > self._max_body_bytes:
                    # Playwright buffers response.body() in full. Skip unknown/oversize
                    # bodies rather than defeating the capture bound before truncation.
                    exchange.response_body_truncated = True
                else:
                    body_bytes = await response.body()
                    exchange.response_body_truncated = len(body_bytes) > self._max_body_bytes
                    bounded = body_bytes[: self._max_body_bytes]
                    exchange.response_body = bounded.decode("utf-8", errors="replace")
                    exchange.response_json = _parse_json(exchange.response_body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - evidence remains useful if body access fails
            exchange.failure = type(exc).__name__

    async def finish(self, *, cancelled: bool = False) -> Path | None:
        if self._finished:
            return self._evidence_path if self._evidence_path.exists() else None
        self._finished = True
        if not cancelled:
            remaining = self._observe_seconds - (time.monotonic() - self._started_monotonic)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._detach()
        await self._stop_worker()
        dom_snapshot: DiscoveryDomSnapshot | None = None
        with contextlib.suppress(Exception):
            dom_snapshot = await asyncio.wait_for(
                self._dom_snapshot_provider(),
                timeout=self._cleanup_timeout_seconds,
            )
        self._classify(dom_snapshot)
        artifact = self._artifact(dom_snapshot, cancelled=cancelled)
        try:
            await asyncio.to_thread(_write_protected_json, self._evidence_path, artifact)
        except Exception as exc:  # noqa: BLE001 - discovery cannot fail monitoring
            log_event(
                logger,
                logging.WARNING,
                "status_discovery_capture_failed",
                operation="write_protected_evidence",
                error_type=type(exc).__name__,
                count=len(self._exchanges),
            )
            return None
        classifications = Counter(exchange.classification for exchange in self._exchanges)
        classification_summary = ",".join(
            f"{name}:{classifications[name]}" for name in sorted(classifications)
        )
        log_event(
            logger,
            logging.INFO,
            "status_discovery_capture_completed",
            operation=classification_summary or "no_captured_requests",
            count=sum(classifications.values()),
        )
        return self._evidence_path

    def _detach(self) -> None:
        for event, callback in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfinished", self._on_request_finished),
            ("requestfailed", self._on_request_failed),
        ):
            with contextlib.suppress(Exception):
                self._page.remove_listener(event, callback)

    async def _stop_worker(self) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            await asyncio.wait_for(
                self._response_queue.join(),
                timeout=self._cleanup_timeout_seconds,
            )
        except TimeoutError:
            pass
        try:
            self._response_queue.put_nowait(None)
        except asyncio.QueueFull:
            worker.cancel()
        try:
            await asyncio.wait_for(worker, timeout=self._cleanup_timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    def _classify(self, dom_snapshot: DiscoveryDomSnapshot | None) -> None:
        route_counts = Counter(_route_signature(item) for item in self._exchanges)
        dom_values = _dom_values(dom_snapshot)
        for exchange in self._exchanges:
            if exchange.resource_type in {"xhr", "fetch"}:
                exchange.classification = "visitor_background_request"
                if exchange.response_json is not None:
                    exchange.classification = "json_status_candidate"
                if route_counts[_route_signature(exchange)] > 1:
                    exchange.classification = "periodic_status_candidate"
            response_values = _flatten_values(exchange.response_json)
            exchange.correlation_fields = sorted(
                key for key, value in dom_values.items() if value in response_values
            )
            if exchange.correlation_fields and exchange.resource_type in {"xhr", "fetch"}:
                exchange.classification = "dom_correlated_status_candidate"
            exchange.observed_identifiers = _observed_identifiers(
                exchange,
                expected_queue_id=self._expected_queue_id,
            )

    def _artifact(
        self,
        dom_snapshot: DiscoveryDomSnapshot | None,
        *,
        cancelled: bool,
    ) -> dict[str, object]:
        routes: dict[str, list[_Exchange]] = defaultdict(list)
        for exchange in self._exchanges:
            routes[_route_signature(exchange)].append(exchange)
        cadence = [
            {
                "route_signature": signature,
                "request_count": len(items),
                "interval_seconds": [
                    round(items[index].request_started_at - items[index - 1].request_started_at, 6)
                    for index in range(1, len(items))
                ],
            }
            for signature, items in routes.items()
        ]
        findings = _derive_findings(
            scope=self._scope,
            exchanges=self._exchanges,
            expected_queue_id=self._expected_queue_id,
        )
        return {
            "schema_version": 1,
            "sensitivity": "SENSITIVE_VISITOR_CREDENTIAL_EVIDENCE",
            "scope": self._scope,
            "generated_at": datetime.now(UTC).isoformat(),
            "session_id": self._session_id,
            "expected_queue_id": self._expected_queue_id,
            "cancelled": cancelled,
            "capture_duration_seconds": round(time.monotonic() - self._started_monotonic, 6),
            "limits": {
                "max_exchanges": self._max_exchanges,
                "max_body_bytes": self._max_body_bytes,
                "event_queue_capacity": self._response_queue.maxsize,
            },
            "dropped": {
                "exchanges": self._dropped_exchanges,
                "response_events": self._dropped_response_events,
            },
            "dom_observation": asdict(dom_snapshot) if dom_snapshot is not None else None,
            "cadence": cadence,
            "findings": findings,
            "exchanges": [_exchange_dict(exchange) for exchange in self._exchanges],
        }


class StatusDiscoveryFactory:
    """Create per-page observers with shared immutable safety limits."""

    def __init__(
        self,
        *,
        evidence_directory: Path,
        scope: str = "diagnostic_unverified_target",
        max_exchanges: int = 100,
        max_body_bytes: int = 65_536,
        event_queue_capacity: int = 100,
        cleanup_timeout_seconds: float = 5.0,
        observe_seconds: float = 0.0,
    ) -> None:
        if max_exchanges < 1 or max_body_bytes < 1 or event_queue_capacity < 1:
            raise ValueError("status discovery limits must be positive")
        if cleanup_timeout_seconds <= 0:
            raise ValueError("status discovery cleanup timeout must be positive")
        if observe_seconds < 0:
            raise ValueError("status discovery observation duration cannot be negative")
        self._directory = evidence_directory
        self._scope = scope
        self._max_exchanges = max_exchanges
        self._max_body_bytes = max_body_bytes
        self._event_queue_capacity = event_queue_capacity
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._observe_seconds = observe_seconds

    def create(
        self,
        *,
        page: Any,
        context: Any,
        session_id: str,
        expected_queue_id: str | None,
        dom_snapshot_provider: DomSnapshotProvider,
    ) -> BrowserNetworkObservation:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_session_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in session_id
        )[:100]
        evidence_path = self._directory / f"{timestamp}-{safe_session_id}.json"
        return BrowserNetworkObservation(
            page=page,
            context=context,
            session_id=session_id,
            expected_queue_id=expected_queue_id,
            evidence_path=evidence_path,
            scope=self._scope,
            dom_snapshot_provider=dom_snapshot_provider,
            max_exchanges=self._max_exchanges,
            max_body_bytes=self._max_body_bytes,
            event_queue_capacity=self._event_queue_capacity,
            cleanup_timeout_seconds=self._cleanup_timeout_seconds,
            observe_seconds=self._observe_seconds,
        )


def _exchange_dict(exchange: _Exchange) -> dict[str, object]:
    value = asdict(exchange)
    for monotonic_field in ("request_started_at", "response_received_at", "completed_at"):
        value.pop(monotonic_field, None)
    return value


def _route_signature(exchange: _Exchange) -> str:
    query_keys = ",".join(sorted(key for key, _ in exchange.query))
    body_keys = ",".join(sorted(_top_level_keys(exchange.request_json)))
    return (
        f"{exchange.method}|{exchange.scheme}|{exchange.host}|{exchange.path}|"
        f"{query_keys}|{body_keys}"
    )


def _derive_findings(
    *,
    scope: str,
    exchanges: list[_Exchange],
    expected_queue_id: str | None,
) -> list[dict[str, str]]:
    questions = (
        "periodic_status_like_request_observed",
        "clearly_associated_with_queue_it_visitor_page",
        "request_contains_queue_id",
        "customer_id_observable",
        "event_id_observable",
        "event_stable_url_components",
        "session_specific_values",
        "per_request_changing_values",
        "cookies_changed_by_responses",
        "request_body_or_query_values_rotated",
        "response_contains_queue_progress_or_status",
        "response_contains_redirect_or_admission_information",
        "safe_to_investigate_replay",
    )
    if scope != "authorized_queue_it_staging":
        return [
            {
                "question": question,
                "result": "UNKNOWN",
                "basis": "No authorised Queue-it staging evidence in this artifact.",
            }
            for question in questions
        ]
    periodic = [
        item
        for item in exchanges
        if item.classification
        in {"periodic_status_candidate", "dom_correlated_status_candidate"}
    ]
    identifier_keys = {
        key
        for item in exchanges
        for key in item.observed_identifiers
    }
    response_keys = {
        key.casefold()
        for item in exchanges
        for key in _all_keys(item.response_json)
    }
    queue_id_seen = expected_queue_id is not None and any(
        _request_contains_value(item, expected_queue_id) for item in exchanges
    )
    rotated_request_values = _request_values_changed(periodic)
    set_cookie_values = {value for item in exchanges for value in item.set_cookie}
    cookies_changed: bool | None = (
        len(set_cookie_values) > 1 if set_cookie_values else False
    )
    results: dict[str, bool | None] = {
        "periodic_status_like_request_observed": bool(periodic),
        "clearly_associated_with_queue_it_visitor_page": any(
            item.correlation_fields for item in periodic
        ),
        "request_contains_queue_id": queue_id_seen,
        "customer_id_observable": "customer_id" in identifier_keys,
        "event_id_observable": "event_id" in identifier_keys,
        "event_stable_url_components": None,
        "session_specific_values": True if queue_id_seen else None,
        "per_request_changing_values": rotated_request_values,
        "cookies_changed_by_responses": cookies_changed,
        "request_body_or_query_values_rotated": rotated_request_values,
        "response_contains_queue_progress_or_status": bool(response_keys & _QUEUE_RESPONSE_KEYS),
        "response_contains_redirect_or_admission_information": bool(
            response_keys & _REDIRECT_RESPONSE_KEYS
        ) or any(item.redirected_to_url is not None for item in exchanges),
        "safe_to_investigate_replay": None,
    }
    findings: list[dict[str, str]] = []
    for question in questions:
        if question in results and results[question] is not None:
            findings.append(
                {
                    "question": question,
                    "result": "PASS" if results[question] is True else "FAIL",
                    "basis": "Derived from this browser-observed artifact.",
                }
            )
        else:
            findings.append(
                {
                    "question": question,
                    "result": "UNKNOWN",
                    "basis": "Requires comparison across requests/sessions or a replay experiment.",
                }
            )
    return findings


def _observed_identifiers(
    exchange: _Exchange,
    *,
    expected_queue_id: str | None,
) -> dict[str, list[str]]:
    observed: dict[str, set[str]] = defaultdict(set)
    for key, value in exchange.query:
        canonical = _IDENTIFIER_KEYS.get(key.casefold())
        if canonical is not None:
            observed[canonical].add(value)
    for source in (exchange.request_json, exchange.response_json):
        for key, identifier_value in _key_values(source):
            canonical = _IDENTIFIER_KEYS.get(key.casefold())
            if canonical is not None:
                observed[canonical].add(str(identifier_value))
    searchable = (exchange.url, exchange.request_body or "", exchange.response_body or "")
    if expected_queue_id and any(expected_queue_id in value for value in searchable):
        observed["queue_id"].add(expected_queue_id)
    return {key: sorted(values) for key, values in observed.items()}


def _request_contains_value(exchange: _Exchange, expected: str) -> bool:
    if expected in exchange.url or expected in (exchange.request_body or ""):
        return True
    return any(str(value) == expected for _, value in _key_values(exchange.request_json))


def _request_values_changed(exchanges: list[_Exchange]) -> bool | None:
    if len(exchanges) < 2:
        return None
    observed: dict[str, set[str]] = defaultdict(set)
    for exchange in exchanges:
        for key, value in exchange.query:
            observed[f"query:{key}"].add(value)
        for key, body_value in _key_values(exchange.request_json):
            observed[f"body:{key}"].add(str(body_value))
    return any(len(values) > 1 for values in observed.values())


def _dom_values(snapshot: DiscoveryDomSnapshot | None) -> dict[str, object]:
    if snapshot is None:
        return {}
    values = asdict(snapshot)
    return {
        key: value
        for key, value in values.items()
        if key not in {"observed_at", "page_url"} and value is not None
    }


def _flatten_values(value: object) -> set[object]:
    flattened: set[object] = set()
    if isinstance(value, Mapping):
        for child in value.values():
            flattened.update(_flatten_values(child))
    elif isinstance(value, list):
        for child in value:
            flattened.update(_flatten_values(child))
    elif isinstance(value, str | int | float | bool):
        flattened.add(value)
    return flattened


def _key_values(value: object) -> list[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(child, str | int | float | bool):
                pairs.append((str(key), child))
            pairs.extend(_key_values(child))
    elif isinstance(value, list):
        for child in value:
            pairs.extend(_key_values(child))
    return pairs


def _all_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key) for key in value} | {
            nested for child in value.values() for nested in _all_keys(child)
        }
    if isinstance(value, list):
        return {nested for child in value for nested in _all_keys(child)}
    return set()


def _top_level_keys(value: object) -> set[str]:
    return {str(key) for key in value} if isinstance(value, Mapping) else set()


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _bounded_text(str(item), 8_192) or ""
        for key, item in list(value.items())[:100]
    }


def _json_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in list(value.items())[:100]}


def _content_type(headers: Mapping[str, str]) -> str | None:
    return next(
        (value for key, value in headers.items() if key.casefold() == "content-type"),
        None,
    )


def _is_textual_content(content_type: str | None) -> bool:
    if content_type is None:
        return False
    lowered = content_type.casefold()
    return "json" in lowered or lowered.startswith("text/") or "javascript" in lowered


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = next(
        (value for key, value in headers.items() if key.casefold() == "content-length"),
        None,
    )
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _bounded_text(value: str | None, maximum_bytes: int) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    return encoded[:maximum_bytes].decode("utf-8", errors="replace")


def _parse_json(value: str | None) -> object | None:
    if value is None or not value.strip():
        return None
    try:
        return cast(object, json.loads(value))
    except (TypeError, ValueError):
        return None


def _write_protected_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    ignore_marker = path.parent / ".gitignore"
    if not ignore_marker.exists():
        try:
            marker_descriptor = os.open(
                ignore_marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(marker_descriptor, "w", encoding="utf-8") as marker:
                marker.write("*\n")
                marker.flush()
                os.fsync(marker.fileno())
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
