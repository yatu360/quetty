"""The one authoritative QueueSession -> IPRoyal sticky-session proxy resolver.

Every outbound target operation that belongs to a QueueSession (acquisition, restore,
monitoring, Refresh Now, Manual Open, and Direct status requests) resolves its proxy
here. Nothing else reconstructs IPRoyal authentication. A session that cannot be
resolved raises :class:`ProxyResolutionError`; callers must then fail closed and never
fall back to an unproxied request.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, TypedDict

from queue_load_test.models.browser import BrowserBackendName
from queue_load_test.models.run import ProxyProvider, RunConfig
from queue_load_test.models.session import QueueSession
from queue_load_test.proxy.iproyal import (
    IPRoyalCredentials,
    IPRoyalProxyConfigurationError,
    construct_effective_password,
    is_valid_proxy_country,
    is_valid_proxy_lifetime,
    is_valid_proxy_session_id,
)

if TYPE_CHECKING:
    from queue_load_test.config import Settings

SUPPORTED_PROXY_BACKENDS = frozenset({BrowserBackendName.PATCHRIGHT, BrowserBackendName.CHROME})


class ProxyFailure(StrEnum):
    """Low-cardinality, sanitized transport failures. Never a Queue lifecycle status."""

    PROXY_CONFIG_MISSING = "PROXY_CONFIG_MISSING"
    PROXY_ASSIGNMENT_MISSING = "PROXY_ASSIGNMENT_MISSING"
    PROXY_ASSIGNMENT_INVALID = "PROXY_ASSIGNMENT_INVALID"
    PROXY_AUTH_FAILED = "PROXY_AUTH_FAILED"
    PROXY_CONNECT_FAILED = "PROXY_CONNECT_FAILED"
    PROXY_UNSUPPORTED_BACKEND = "PROXY_UNSUPPORTED_BACKEND"


class ProxyPurpose(StrEnum):
    BROWSER = "browser"
    DIRECT = "direct"


class ProxyResolutionError(RuntimeError):
    """A session's proxy cannot be resolved; its message is only the classification."""

    def __init__(self, failure: ProxyFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class SessionProxySettings(TypedDict):
    """Structured per-context proxy values, structurally the browser API's shape."""

    server: str
    username: str
    password: str


@dataclass(frozen=True, slots=True)
class ProxyDiagnostics:
    """Safe, non-secret metadata describing which sticky session was configured."""

    provider: ProxyProvider
    provider_session_id: str
    country: str
    lifetime: str
    purpose: ProxyPurpose


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedSessionProxy:
    """One session's in-memory proxy; credential material never enters repr."""

    diagnostics: ProxyDiagnostics
    server: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)

    def browser_proxy(self) -> SessionProxySettings:
        return {"server": self.server, "username": self.username, "password": self.password}

    def __repr__(self) -> str:
        diagnostics = self.diagnostics
        return (
            "ResolvedSessionProxy("
            f"provider={diagnostics.provider.value!r}, "
            f"provider_session_id={diagnostics.provider_session_id!r}, "
            f"country={diagnostics.country!r}, lifetime={diagnostics.lifetime!r}, "
            "credentials='<redacted>')"
        )


def resolve_proxy_for_session(
    run: RunConfig,
    session: QueueSession,
    credentials: IPRoyalCredentials | None,
    *,
    purpose: ProxyPurpose = ProxyPurpose.BROWSER,
) -> ResolvedSessionProxy | None:
    """Return the session's persisted sticky-session proxy, ``None`` when unproxied."""

    if run.proxy_provider is ProxyProvider.NONE:
        if session.proxy_session_id is not None:
            # Provenance contradiction: never guess which way to route it.
            raise ProxyResolutionError(ProxyFailure.PROXY_ASSIGNMENT_INVALID)
        return None
    if run.browser_backend not in SUPPORTED_PROXY_BACKENDS:
        raise ProxyResolutionError(ProxyFailure.PROXY_UNSUPPORTED_BACKEND)
    country = run.proxy_country
    lifetime = run.proxy_lifetime
    if (
        credentials is None
        or not is_valid_proxy_country(country)
        or not is_valid_proxy_lifetime(lifetime)
    ):
        raise ProxyResolutionError(ProxyFailure.PROXY_CONFIG_MISSING)
    assert country is not None and lifetime is not None
    session_id = session.proxy_session_id
    if session_id is None:
        raise ProxyResolutionError(ProxyFailure.PROXY_ASSIGNMENT_MISSING)
    if not is_valid_proxy_session_id(session_id):
        raise ProxyResolutionError(ProxyFailure.PROXY_ASSIGNMENT_INVALID)
    try:
        password = construct_effective_password(
            credentials.base_password,
            country=country,
            session_id=session_id,
            lifetime=lifetime,
        )
    except IPRoyalProxyConfigurationError:
        # Suppress the cause: nothing derived from credentials may reach a traceback.
        raise ProxyResolutionError(ProxyFailure.PROXY_CONFIG_MISSING) from None
    return ResolvedSessionProxy(
        diagnostics=ProxyDiagnostics(
            provider=run.proxy_provider,
            provider_session_id=session_id,
            country=country,
            lifetime=lifetime,
            purpose=purpose,
        ),
        server=credentials.server,
        username=credentials.username,
        password=password,
    )


class ProxyObserver(Protocol):
    """Low-cardinality counters; implemented by PrometheusMetrics."""

    def record_proxy_attempt(self, purpose: ProxyPurpose) -> None: ...

    def record_proxy_failure(self, purpose: ProxyPurpose, failure: ProxyFailure) -> None: ...


class SessionProxyResolver:
    """Bind the resolver to one immutable run and the runtime's environment credentials."""

    def __init__(
        self,
        run: RunConfig,
        credentials: IPRoyalCredentials | None,
        *,
        observer: ProxyObserver | None = None,
        on_resolved: Callable[[ProxyDiagnostics], None] | None = None,
    ) -> None:
        self._run = run
        self._credentials = credentials
        self._observer = observer
        self._on_resolved = on_resolved

    @classmethod
    def for_run(
        cls,
        run: RunConfig,
        settings: Settings,
        *,
        observer: ProxyObserver | None = None,
        on_resolved: Callable[[ProxyDiagnostics], None] | None = None,
    ) -> SessionProxyResolver:
        credentials: IPRoyalCredentials | None = None
        if run.proxy_provider is ProxyProvider.IPROYAL:
            try:
                credentials = settings.require_iproyal_credentials()
            except IPRoyalProxyConfigurationError:
                credentials = None  # every resolution then fails closed
        return cls(run, credentials, observer=observer, on_resolved=on_resolved)

    @property
    def provider(self) -> ProxyProvider:
        return self._run.proxy_provider

    @property
    def enabled(self) -> bool:
        return self._run.proxy_provider is not ProxyProvider.NONE

    def resolve(
        self,
        session: QueueSession,
        *,
        purpose: ProxyPurpose = ProxyPurpose.BROWSER,
    ) -> ResolvedSessionProxy | None:
        try:
            resolved = resolve_proxy_for_session(
                self._run, session, self._credentials, purpose=purpose
            )
        except ProxyResolutionError as exc:
            self.record_failure(exc.failure, purpose=purpose)
            raise
        if resolved is not None and self._on_resolved is not None:
            self._on_resolved(resolved.diagnostics)
        return resolved

    def record_attempt(self, *, purpose: ProxyPurpose = ProxyPurpose.BROWSER) -> None:
        """Count one proxied context (or Direct request) actually being opened."""

        if self._observer is not None:
            self._observer.record_proxy_attempt(purpose)

    def record_failure(
        self, failure: ProxyFailure, *, purpose: ProxyPurpose = ProxyPurpose.BROWSER
    ) -> None:
        if self._observer is not None:
            self._observer.record_proxy_failure(purpose, failure)

    def __repr__(self) -> str:
        return f"SessionProxyResolver(provider={self._run.proxy_provider.value!r})"


def resolve_session_proxy(
    resolver: SessionProxyResolver | None,
    session: QueueSession,
    *,
    purpose: ProxyPurpose = ProxyPurpose.BROWSER,
) -> ResolvedSessionProxy | None:
    """Resolve through an optional resolver without ever unproxying an assigned session.

    Callers without a resolver (legacy paths and gated harnesses) stay unproxied, but a
    session that carries a persisted assignment can never be sent without its proxy.
    """

    if resolver is None:
        if session.proxy_session_id is not None:
            raise ProxyResolutionError(ProxyFailure.PROXY_CONFIG_MISSING)
        return None
    return resolver.resolve(session, purpose=purpose)


_AUTH_MARKERS = (
    "ERR_INVALID_AUTH_CREDENTIALS",
    "ERR_PROXY_AUTH",
    "ERR_NO_SUPPORTED_PROXIES",
    "407",
)
_CONNECT_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_PROXY",
)


class ProxyAuthWatch:
    """Notice the proxy rejecting a page's sticky-session credentials (HTTP 407).

    Chromium reports a rejected proxy login as a generic response-code failure, so
    the 407 itself is observed on the page. Only the status is kept.
    """

    def __init__(self) -> None:
        self.rejected = False

    @classmethod
    def attach(cls, page: object) -> ProxyAuthWatch:
        watch = cls()

        def on_response(response: object) -> None:
            if getattr(response, "status", None) == 407:
                watch.rejected = True

        on = getattr(page, "on", None)
        if callable(on):
            # Diagnostics only: a listener problem must never affect navigation.
            with contextlib.suppress(Exception):
                on("response", on_response)
        return watch


def classify_proxy_error(
    exc: BaseException, watch: ProxyAuthWatch | None = None
) -> ProxyFailure | None:
    """Classify a browser/HTTP error raised through a proxy; the text is never kept."""

    if watch is not None and watch.rejected:
        return ProxyFailure.PROXY_AUTH_FAILED
    text = str(exc)
    if any(marker in text for marker in _AUTH_MARKERS):
        return ProxyFailure.PROXY_AUTH_FAILED
    if any(marker in text for marker in _CONNECT_MARKERS):
        return ProxyFailure.PROXY_CONNECT_FAILED
    return None
