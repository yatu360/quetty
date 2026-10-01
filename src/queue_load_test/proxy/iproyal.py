"""Minimal IPRoyal sticky-session identity and authentication helpers.

This module deliberately does not route browser traffic.  It owns only the durable,
non-secret provider session identifier and the in-memory authentication construction
needed by the Phase 9 runtime-routing follow-up.
"""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass, field

PROXY_SESSION_ID_LENGTH = 8
PROXY_SESSION_ALPHABET = string.ascii_letters + string.digits
_PROXY_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
# Separator-free components keep the underscore-delimited password unambiguous.
_PROXY_COUNTRY_PATTERN = re.compile(r"^[A-Za-z]{2}$")
_PROXY_LIFETIME_PATTERN = re.compile(r"^[1-9][0-9]*[smhd]$")


class IPRoyalProxyConfigurationError(ValueError):
    """Sanitized production proxy configuration error."""


@dataclass(frozen=True, slots=True, repr=False)
class IPRoyalCredentials:
    """Environment-only account credentials; repr never exposes their values."""

    server: str = field(repr=False)
    username: str = field(repr=False)
    base_password: str = field(repr=False)

    def __repr__(self) -> str:
        return "IPRoyalCredentials(provider='iproyal', credentials='<redacted>')"


def is_valid_proxy_session_id(value: str | None) -> bool:
    return value is not None and _PROXY_SESSION_PATTERN.fullmatch(value) is not None


def is_valid_proxy_country(value: str | None) -> bool:
    return value is not None and _PROXY_COUNTRY_PATTERN.fullmatch(value) is not None


def is_valid_proxy_lifetime(value: str | None) -> bool:
    return value is not None and _PROXY_LIFETIME_PATTERN.fullmatch(value) is not None


def validate_proxy_session_id(value: str) -> str:
    if not is_valid_proxy_session_id(value):
        raise IPRoyalProxyConfigurationError(
            "IPRoyal session ID must contain exactly 8 alphanumeric characters"
        )
    return value


def generate_proxy_session_id() -> str:
    """Return a cryptographically generated provider sticky-session identifier."""

    return "".join(secrets.choice(PROXY_SESSION_ALPHABET) for _ in range(PROXY_SESSION_ID_LENGTH))


def construct_effective_password(
    base_password: str,
    *,
    country: str,
    session_id: str,
    lifetime: str,
) -> str:
    """Construct the provider password in memory without retaining or logging it."""

    if (
        not base_password
        or not is_valid_proxy_country(country)
        or not is_valid_proxy_lifetime(lifetime)
    ):
        raise IPRoyalProxyConfigurationError("IPRoyal authentication components are incomplete")
    validate_proxy_session_id(session_id)
    return (
        f"{base_password}_country-{country}_session-{session_id}_lifetime-{lifetime}"
    )
