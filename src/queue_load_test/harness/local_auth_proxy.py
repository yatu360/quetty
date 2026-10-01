"""A tiny local authenticating HTTP forward proxy for per-session routing tests.

It is **not** IPRoyal. It stands in for the provider in local tests: every request
must carry HTTP Basic proxy credentials, and the proxy records which sticky-session
identifier (``_session-XXXXXXXX_``) each forwarded request authenticated with. It
forwards only to loopback origins (the local queue simulator, optionally reached via a
non-loopback ``aliases`` hostname) and refuses CONNECT tunnels, so a test run never
reaches the internet. Test credentials are fake values.

It can also answer for a fake ipify host (``ipify_host``): like a sticky residential
provider, it returns one stable synthetic exit IP per sticky session (documentation
ranges only), which a test may change to simulate a provider exit change.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_SESSION_PATTERN = re.compile(r"_session-([A-Za-z0-9]{8})_")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

MODE_FORWARD = "forward"
MODE_REJECT_AUTH = "reject_auth"
MODE_UPSTREAM_DOWN = "upstream_down"

IPIFY_OK = "ok"
IPIFY_TIMEOUT = "timeout"
IPIFY_MALFORMED = "malformed"
IPIFY_INVALID_IP = "invalid_ip"
IPIFY_HTTP_500 = "http_500"
DEFAULT_IPIFY_HOST = "ipify.quetty.test"


@dataclass(frozen=True, slots=True)
class ProxiedRequest:
    """One forwarded request and the sticky session it authenticated with."""

    method: str
    target: str
    username: str
    provider_session_id: str | None


@dataclass(slots=True)
class LocalAuthProxy:
    """Require Basic proxy auth, record the sticky session, forward to loopback only."""

    mode: str = MODE_FORWARD
    # Non-loopback hostname -> loopback port, resolved here and never by the browser.
    aliases: dict[str, int] = field(default_factory=dict)
    ipify_host: str = DEFAULT_IPIFY_HOST
    ipify_mode: str = IPIFY_OK
    ipify_timeout_seconds: float = 30.0
    # Explicit synthetic exit IP per sticky session; otherwise a stable derived one.
    exit_ips: dict[str, str] = field(default_factory=dict)
    ip_lookups: list[str | None] = field(default_factory=list)
    requests: list[ProxiedRequest] = field(default_factory=list)
    auth_challenges: int = 0
    rejected_auth: int = 0
    refused: int = 0
    _server: asyncio.Server | None = None
    port: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def session_ids(self) -> list[str | None]:
        return [request.provider_session_id for request in self.requests]

    def exit_ip(self, provider_session_id: str | None) -> str:
        """The synthetic exit IP this sticky session currently maps to (TEST-NET-2)."""

        if provider_session_id is None:
            return "203.0.113.1"
        explicit = self.exit_ips.get(provider_session_id)
        if explicit is not None:
            return explicit
        digest = sum((index + 1) * ord(char) for index, char in enumerate(provider_session_id))
        return f"198.51.100.{digest % 250 + 1}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        if not self._server.sockets:
            raise RuntimeError("local auth proxy did not bind a socket")
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=2)
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) != 3:
                return
            method, target, version = parts
            headers = [line for line in lines[1:] if line]
            authorization = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in headers
                    if line.casefold().startswith("proxy-authorization:")
                ),
                None,
            )
            if authorization is None:
                self.auth_challenges += 1
                await self._reply(
                    writer,
                    "407 Proxy Authentication Required",
                    'Proxy-Authenticate: Basic realm="local-test"\r\n',
                )
                return
            credentials = _basic_credentials(authorization)
            if credentials is None or self.mode == MODE_REJECT_AUTH:
                self.rejected_auth += 1
                await self._reply(
                    writer,
                    "407 Proxy Authentication Required",
                    'Proxy-Authenticate: Basic realm="local-test"\r\n',
                )
                return
            username, password = credentials
            parsed = urlsplit(target)
            match = _SESSION_PATTERN.search(password)
            provider_session_id = match.group(1) if match else None
            if method != "CONNECT" and parsed.hostname == self.ipify_host:
                await self._ipify(writer, provider_session_id)
                return
            if method != "CONNECT" and parsed.hostname in self.aliases:
                target = parsed._replace(
                    netloc=f"127.0.0.1:{self.aliases[parsed.hostname]}"
                ).geturl()
                parsed = urlsplit(target)
            if method == "CONNECT" or parsed.hostname not in _LOOPBACK_HOSTS:
                # No tunnels and no internet: only the local simulator is reachable.
                self.refused += 1
                await self._reply(writer, "403 Forbidden")
                return
            if self.mode == MODE_UPSTREAM_DOWN:
                self.refused += 1
                await self._reply(writer, "502 Bad Gateway")
                return
            self.requests.append(
                ProxiedRequest(
                    method=method,
                    target=target,
                    username=username,
                    provider_session_id=provider_session_id,
                )
            )
            await self._forward(reader, writer, method, target, version, headers)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _forward(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        version: str,
        headers: list[str],
    ) -> None:
        url = urlsplit(target)
        upstream_reader, upstream_writer = await asyncio.open_connection(
            url.hostname, url.port or 80
        )
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        forwarded = [
            line
            for line in headers
            if not line.casefold().startswith(("proxy-", "connection:", "keep-alive:"))
        ]
        request = f"{method} {path} {version}\r\n" + "\r\n".join(
            [*forwarded, "Connection: close"]
        )
        upstream_writer.write((request + "\r\n\r\n").encode("latin-1"))
        await upstream_writer.drain()
        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
        )

    async def _ipify(
        self, writer: asyncio.StreamWriter, provider_session_id: str | None
    ) -> None:
        self.ip_lookups.append(provider_session_id)
        if self.ipify_mode == IPIFY_TIMEOUT:
            await asyncio.sleep(self.ipify_timeout_seconds)
            return
        if self.ipify_mode == IPIFY_HTTP_500:
            await self._reply(writer, "500 Internal Server Error")
            return
        if self.ipify_mode == IPIFY_MALFORMED:
            body = b"{not json"
        elif self.ipify_mode == IPIFY_INVALID_IP:
            body = b'{"ip": "999.1.2.3"}'
        else:
            body = json.dumps({"ip": self.exit_ip(provider_session_id)}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: str, extra: str = "") -> None:
        writer.write(
            f"HTTP/1.1 {status}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode(
                "latin-1"
            )
        )
        await writer.drain()


def _basic_credentials(value: str) -> tuple[str, str] | None:
    scheme, _, encoded = value.partition(" ")
    if scheme.casefold() != "basic":
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    return (username, password) if separator else None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65_536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
