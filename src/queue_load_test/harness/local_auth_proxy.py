"""A tiny local authenticating HTTP forward proxy for per-session routing tests.

It is **not** IPRoyal. It stands in for the provider in local tests: every request
must carry HTTP Basic proxy credentials, and the proxy records which sticky-session
identifier (``_session-XXXXXXXX_``) each forwarded request authenticated with. It
forwards only to loopback origins (the local queue simulator) and refuses CONNECT
tunnels, so a test run never reaches the internet. Test credentials are fake values.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_SESSION_PATTERN = re.compile(r"_session-([A-Za-z0-9]{8})_")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

MODE_FORWARD = "forward"
MODE_REJECT_AUTH = "reject_auth"
MODE_UPSTREAM_DOWN = "upstream_down"


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
            if method == "CONNECT" or parsed.hostname not in _LOOPBACK_HOSTS:
                # No tunnels and no internet: only the local simulator is reachable.
                self.refused += 1
                await self._reply(writer, "403 Forbidden")
                return
            if self.mode == MODE_UPSTREAM_DOWN:
                self.refused += 1
                await self._reply(writer, "502 Bad Gateway")
                return
            match = _SESSION_PATTERN.search(password)
            self.requests.append(
                ProxiedRequest(
                    method=method,
                    target=target,
                    username=username,
                    provider_session_id=match.group(1) if match else None,
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
