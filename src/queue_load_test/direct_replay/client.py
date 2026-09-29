"""Bounded out-of-browser replay of one exact browser-observed request."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from http.cookiejar import Cookie
from typing import Any, Self

import httpx

from queue_load_test.direct_replay.models import (
    HeaderProfile,
    ReplayCookie,
    ReplayFailure,
    ReplayRecipe,
    ReplayResult,
)
from queue_load_test.direct_replay.store import ProtectedReplayStateStore
from queue_load_test.metrics.logging import log_event, quiet_http_client_loggers

logger = logging.getLogger(__name__)
# Replayed requests carry visitor cookies and headers; keep the HTTP client's own
# INFO/DEBUG request and header traces out of every log handler.
quiet_http_client_loggers()

_BLOCKED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-connection",
        "accept-encoding",
        "cookie",
        "keep-alive",
        "priority",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)
_BLOCKED_PREFIXES = ("sec-ch-", "sec-fetch-")
_MINIMAL_HEADERS = frozenset({"accept", "content-type"})

# Fixed, non-sensitive failure details. Production monitoring classifies SCHEMA
# failures by these exact values; they never carry response material.
DETAIL_CONTENT_TYPE = "unexpected response content type"
DETAIL_OVERSIZE = "response exceeded bound"
DETAIL_MALFORMED = "response JSON was malformed"


class DirectStatusReplayClient:
    """One-session client with fixed limits and response-cookie retention."""

    def __init__(
        self,
        *,
        recipe: ReplayRecipe,
        cookies: tuple[ReplayCookie, ...],
        state_store: ProtectedReplayStateStore,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 65_536,
        max_connections: int = 2,
        max_concurrency: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("replay timeout and response bound must be positive")
        if max_connections < 1 or max_concurrency < 1:
            raise ValueError("replay connection and concurrency bounds must be positive")
        self._recipe = recipe
        self._state_store = state_store
        self._max_response_bytes = max_response_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        jar = httpx.Cookies()
        for cookie in cookies:
            if cookie.expires is not None and cookie.expires <= time.time():
                continue
            jar.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
        self._client = httpx.AsyncClient(
            cookies=jar,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=min(max_connections, 1),
            ),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def replay(self, profile: HeaderProfile = HeaderProfile.FULL_DERIVED) -> ReplayResult:
        """Replay only the captured URL/method/body, never a constructed request."""

        before = _cookie_signature(self._client.cookies.jar)
        try:
            async with self._semaphore, self._client.stream(
                self._recipe.method,
                self._recipe.url,
                headers=_headers_for(self._recipe, profile),
                content=self._recipe.body,
            ) as response:
                result = await self._consume_response(response, profile, before)
        except httpx.TimeoutException:
            result = _failure(profile, ReplayFailure.TIMEOUT, "request timed out")
        except httpx.RequestError:
            result = _failure(profile, ReplayFailure.NETWORK, "network request failed")
        except asyncio.CancelledError:
            await self.aclose()
            raise
        log_event(
            logger,
            logging.INFO,
            "direct_replay_attempt_completed",
            operation=profile.value,
            status=result.status_code,
            error_type=result.failure.value if result.failure is not None else None,
            count=1,
        )
        return result

    async def _consume_response(
        self,
        response: httpx.Response,
        profile: HeaderProfile,
        before: tuple[tuple[str, str, str, str], ...],
    ) -> ReplayResult:
        status = response.status_code
        if 300 <= status < 400:
            return await self._finish_failure(
                profile, ReplayFailure.REDIRECT, status, "redirect response", before
            )
        if status in {401, 403, 410}:
            return await self._finish_failure(
                profile, ReplayFailure.STATE, status, "visitor state rejected", before
            )
        if status < 200 or status >= 300:
            return await self._finish_failure(
                profile, ReplayFailure.HTTP, status, "non-success HTTP response", before
            )
        content_type = response.headers.get("content-type", "").casefold()
        if "json" not in content_type:
            return await self._finish_failure(
                profile, ReplayFailure.SCHEMA, status, DETAIL_CONTENT_TYPE, before
            )
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_response_bytes:
                return await self._finish_failure(
                    profile, ReplayFailure.SCHEMA, status, DETAIL_OVERSIZE, before
                )
        try:
            parsed: object = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return await self._finish_failure(
                profile, ReplayFailure.SCHEMA, status, DETAIL_MALFORMED, before
            )
        observed_queue_ids = _named_queue_ids(parsed)
        if observed_queue_ids and observed_queue_ids != {self._recipe.expected_queue_id}:
            return await self._finish_failure(
                profile,
                ReplayFailure.IDENTITY,
                status,
                "response Queue ID contradicted persisted identity",
                before,
            )
        cookies_changed = await self._persist_cookies(before)
        return ReplayResult(
            succeeded=True,
            failure=None,
            status_code=status,
            header_profile=profile,
            identity_confirmed=observed_queue_ids == {self._recipe.expected_queue_id},
            cookies_changed=cookies_changed,
            response_json=parsed,
        )

    async def _finish_failure(
        self,
        profile: HeaderProfile,
        failure: ReplayFailure,
        status: int,
        detail: str,
        before: tuple[tuple[str, str, str, str], ...],
    ) -> ReplayResult:
        changed = await self._persist_cookies(before)
        return ReplayResult(False, failure, status, profile, False, changed, detail=detail)

    async def _persist_cookies(
        self, before: tuple[tuple[str, str, str, str], ...]
    ) -> bool:
        cookies = _cookies_from_jar(self._client.cookies.jar)
        await self._state_store.save(
            session_id=self._recipe.session_id,
            recipe_fingerprint=self._recipe.fingerprint,
            cookies=cookies,
        )
        return _cookie_signature(self._client.cookies.jar) != before


def _headers_for(recipe: ReplayRecipe, profile: HeaderProfile) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in recipe.headers.items():
        lowered = name.casefold()
        if (
            lowered in _BLOCKED_HEADERS
            or lowered.startswith(_BLOCKED_PREFIXES)
            or lowered.startswith(":")
        ):
            continue
        if profile is HeaderProfile.MINIMAL and lowered not in _MINIMAL_HEADERS:
            continue
        headers[name] = value
    return headers


def _named_queue_ids(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() == "queueid" and isinstance(child, str):
                found.add(child)
            found.update(_named_queue_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_named_queue_ids(child))
    return found


def _cookies_from_jar(jar: Any) -> tuple[ReplayCookie, ...]:
    return tuple(
        ReplayCookie(
            name=cookie.name,
            value=cookie.value,
            domain=cookie.domain,
            path=cookie.path,
            secure=cookie.secure,
            expires=cookie.expires,
        )
        for cookie in jar
        if isinstance(cookie, Cookie) and isinstance(cookie.value, str)
    )


def _cookie_signature(jar: Any) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        sorted(
            (cookie.name, cookie.value, cookie.domain, cookie.path)
            for cookie in jar
            if isinstance(cookie, Cookie) and isinstance(cookie.value, str)
        )
    )


def _failure(profile: HeaderProfile, failure: ReplayFailure, detail: str) -> ReplayResult:
    return ReplayResult(False, failure, None, profile, False, False, detail=detail)
