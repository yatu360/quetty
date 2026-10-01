"""One bounded proxy-exit IP lookup through a QueueSession's own sticky session.

The observer only answers "which exit IP does this session's existing IPRoyal sticky
session currently use?". It takes an already-resolved session proxy, so there is no
code path that can send the lookup through the machine's direct connection. It never
assigns or rotates proxy identities, never touches Queue lifecycle, and never opens a
browser context.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from queue_load_test.proxy.resolver import ResolvedSessionProxy

DEFAULT_PROXY_IP_ENDPOINT = "https://api.ipify.org/?format=json"

type ProxiedTransportFactory = Callable[[httpx.Proxy], httpx.AsyncBaseTransport]


class ProxyIpFailure(StrEnum):
    """Sanitized, low-cardinality reasons an IP observation produced no value."""

    PROXY_IP_UNRESOLVED = "PROXY_IP_UNRESOLVED"
    PROXY_IP_TIMEOUT = "PROXY_IP_TIMEOUT"
    PROXY_IP_PROXY_FAILED = "PROXY_IP_PROXY_FAILED"
    PROXY_IP_NETWORK = "PROXY_IP_NETWORK"
    PROXY_IP_HTTP_STATUS = "PROXY_IP_HTTP_STATUS"
    PROXY_IP_TOO_LARGE = "PROXY_IP_TOO_LARGE"
    PROXY_IP_MALFORMED = "PROXY_IP_MALFORMED"
    PROXY_IP_INVALID = "PROXY_IP_INVALID"
    PROXY_IP_STORE_FAILED = "PROXY_IP_STORE_FAILED"


class ProxyIpParseError(ValueError):
    def __init__(self, failure: ProxyIpFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class ProxyIpObservation:
    """A validated exit IP, or the sanitized reason there is none."""

    ip: str | None = field(default=None, repr=False)
    failure: ProxyIpFailure | None = None

    @property
    def succeeded(self) -> bool:
        return self.ip is not None


def parse_proxy_ip_response(body: bytes) -> str:
    """Accept only ``{"ip": "<IPv4 or IPv6>"}`` as Python's ``ipaddress`` parses it."""

    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProxyIpParseError(ProxyIpFailure.PROXY_IP_MALFORMED) from None
    if not isinstance(document, dict):
        raise ProxyIpParseError(ProxyIpFailure.PROXY_IP_MALFORMED)
    value = document.get("ip")
    if not isinstance(value, str):
        raise ProxyIpParseError(ProxyIpFailure.PROXY_IP_MALFORMED)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ProxyIpParseError(ProxyIpFailure.PROXY_IP_INVALID) from None


class ProxyIpObserver:
    """Make one deadline-bounded ipify request through a resolved session proxy."""

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_PROXY_IP_ENDPOINT,
        timeout_seconds: float = 5.0,
        connect_timeout_seconds: float = 3.0,
        max_response_bytes: int = 1_024,
        transport_factory: ProxiedTransportFactory | None = None,
    ) -> None:
        """``transport_factory`` is a local-test seam: it receives the session's
        structured proxy and must route through it."""

        if timeout_seconds <= 0 or connect_timeout_seconds <= 0 or max_response_bytes < 1:
            raise ValueError("proxy IP observation bounds must be positive")
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds
        self._connect_timeout_seconds = min(connect_timeout_seconds, timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._transport_factory = transport_factory

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def observe(self, proxy: ResolvedSessionProxy) -> ProxyIpObservation:
        try:
            # The whole lookup (connect, proxy auth, response) shares one deadline so
            # a slow provider or ipify can never stall a monitoring worker.
            return await asyncio.wait_for(self._observe(proxy), timeout=self._timeout_seconds)
        except TimeoutError:
            return ProxyIpObservation(failure=ProxyIpFailure.PROXY_IP_TIMEOUT)

    async def _observe(self, resolved: ResolvedSessionProxy) -> ProxyIpObservation:
        proxy = httpx.Proxy(resolved.server, auth=(resolved.username, resolved.password))
        transport: httpx.AsyncBaseTransport | None = None
        client_proxy: httpx.Proxy | None = proxy
        if self._transport_factory is not None:
            transport, client_proxy = self._transport_factory(proxy), None
        timeout = httpx.Timeout(self._timeout_seconds, connect=self._connect_timeout_seconds)
        try:
            async with httpx.AsyncClient(
                proxy=client_proxy,
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                # Only the session's explicit proxy may route this request.
                trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                headers={"accept": "application/json"},
            ) as client, client.stream("GET", self._endpoint) as response:
                if response.status_code != 200:
                    return ProxyIpObservation(failure=ProxyIpFailure.PROXY_IP_HTTP_STATUS)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        return ProxyIpObservation(failure=ProxyIpFailure.PROXY_IP_TOO_LARGE)
        except httpx.ProxyError:
            return ProxyIpObservation(failure=ProxyIpFailure.PROXY_IP_PROXY_FAILED)
        except httpx.TimeoutException:
            return ProxyIpObservation(failure=ProxyIpFailure.PROXY_IP_TIMEOUT)
        except httpx.HTTPError:
            # Only the classification is kept: HTTP-library messages can carry URLs.
            return ProxyIpObservation(failure=ProxyIpFailure.PROXY_IP_NETWORK)
        try:
            return ProxyIpObservation(ip=parse_proxy_ip_response(bytes(body)))
        except ProxyIpParseError as exc:
            return ProxyIpObservation(failure=exc.failure)
