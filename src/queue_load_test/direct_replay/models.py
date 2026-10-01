"""Sensitive recipe and sanitized result types for the Phase 8 replay experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class HeaderProfile(StrEnum):
    """Controlled browser-derived header sets; neither includes transport headers."""

    FULL_DERIVED = "full_derived"
    MINIMAL = "minimal"


class ReplayFailure(StrEnum):
    """Stable failure boundaries for experimental replay evidence."""

    NETWORK = "network"
    TIMEOUT = "timeout"
    HTTP = "http"
    REDIRECT = "redirect"
    SCHEMA = "schema"
    STATE = "state"
    IDENTITY = "identity"
    PROXY = "proxy"


class ValueStability(StrEnum):
    EVENT_STABLE = "event-stable"
    SESSION_STABLE = "session-stable"
    REQUEST_TRANSIENT = "request-transient"
    RESPONSE_REFRESHED = "response-refreshed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReplayRecipe:
    """One exact, browser-observed request; every raw field is protected."""

    session_id: str
    expected_queue_id: str = field(repr=False)
    source_scope: str
    exchange_sequence: int
    url: str = field(repr=False)
    method: str
    headers: dict[str, str] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    observed_identifiers: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False
    )
    fingerprint: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class ReplayCookie:
    name: str = field(repr=False)
    value: str = field(repr=False)
    domain: str = field(repr=False)
    path: str = field(default="/", repr=False)
    secure: bool = False
    expires: int | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Result safe for normal reports/logs; response material is intentionally absent."""

    succeeded: bool
    failure: ReplayFailure | None
    status_code: int | None
    header_profile: HeaderProfile
    identity_confirmed: bool
    cookies_changed: bool
    response_json: object | None = field(default=None, repr=False, compare=False)
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StabilityFinding:
    value: str
    classification: ValueStability
    basis: str
