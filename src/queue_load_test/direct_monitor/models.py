"""Per-session direct-monitor capability and classified fallback reasons."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from queue_load_test.models import MonitoringObservation


class DirectCapability(StrEnum):
    """Internal implementation state; never an operator-selected strategy."""

    DISCOVERY_REQUIRED = "DISCOVERY_REQUIRED"
    DIRECT_CAPABLE = "DIRECT_CAPABLE"
    DIRECT_UNAVAILABLE = "DIRECT_UNAVAILABLE"


class DirectFallbackReason(StrEnum):
    """Why one Direct Monitoring Strategy check used the browser monitor."""

    # Capability/configuration: no direct attempt was made.
    NO_QUEUE_ID = "no_queue_id"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    DISCOVERY_REQUIRED = "discovery_required"
    DIRECT_UNAVAILABLE = "direct_unavailable"
    # Transport.
    NETWORK = "network"
    TIMEOUT = "timeout"
    UNEXPECTED_HTTP_STATUS = "unexpected_http_status"
    UNEXPECTED_REDIRECT = "unexpected_redirect"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"
    MALFORMED_RESPONSE = "malformed_response"
    # Visitor state and identity.
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    MISSING_VISITOR_STATE = "missing_visitor_state"
    REJECTED_SESSION_STATE = "rejected_session_state"
    IDENTITY_AMBIGUITY = "identity_ambiguity"
    IDENTITY_MISMATCH = "identity_mismatch"
    # Lifecycle.
    UNKNOWN_LIFECYCLE = "unknown_lifecycle"
    CONTRADICTORY_LIFECYCLE = "contradictory_lifecycle"
    UNSUPPORTED_ADMISSION = "unsupported_admission"
    # Recipe provenance and anything else that makes preservation uncertain.
    RECIPE_UNCERTAIN = "recipe_uncertain"
    UNCERTAIN = "uncertain"


# A browser fallback is needed and the stored recipe/state is no longer trusted:
# only a newly observed legitimate browser request may make the session capable.
HARD_FAILURES = frozenset(
    {
        DirectFallbackReason.UNEXPECTED_REDIRECT,
        DirectFallbackReason.UNEXPECTED_CONTENT_TYPE,
        DirectFallbackReason.MALFORMED_RESPONSE,
        DirectFallbackReason.SCHEMA_INCOMPATIBLE,
        DirectFallbackReason.MISSING_VISITOR_STATE,
        DirectFallbackReason.REJECTED_SESSION_STATE,
        DirectFallbackReason.IDENTITY_AMBIGUITY,
        DirectFallbackReason.IDENTITY_MISMATCH,
        DirectFallbackReason.CONTRADICTORY_LIFECYCLE,
        DirectFallbackReason.RECIPE_UNCERTAIN,
    }
)
# Plausibly transient; the recipe is kept until a bounded number of consecutive
# failures, after which the session also becomes DIRECT_UNAVAILABLE.
SOFT_FAILURES = frozenset(
    {
        DirectFallbackReason.NETWORK,
        DirectFallbackReason.TIMEOUT,
        DirectFallbackReason.UNEXPECTED_HTTP_STATUS,
        DirectFallbackReason.UNKNOWN_LIFECYCLE,
        DirectFallbackReason.UNSUPPORTED_ADMISSION,
        DirectFallbackReason.UNCERTAIN,
    }
)


@dataclass(frozen=True, slots=True)
class DirectAttempt:
    """A validated direct observation, or the classified reason to fall back."""

    observation: MonitoringObservation | None = None
    fallback_reason: DirectFallbackReason | None = None

    def __post_init__(self) -> None:
        if (self.observation is None) == (self.fallback_reason is None):
            raise ValueError("a direct attempt has exactly one of observation or reason")

    @property
    def attempted_request(self) -> bool:
        """Whether network I/O happened (capability/config reasons make none)."""

        return self.fallback_reason not in {
            DirectFallbackReason.NO_QUEUE_ID,
            DirectFallbackReason.SCHEMA_UNAVAILABLE,
            DirectFallbackReason.DISCOVERY_REQUIRED,
            DirectFallbackReason.DIRECT_UNAVAILABLE,
        }
