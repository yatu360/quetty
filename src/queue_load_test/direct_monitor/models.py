"""Per-session direct-monitor capability and classified fallback reasons."""

from __future__ import annotations

from dataclasses import dataclass, field

from queue_load_test.models import (
    DirectCapability,
    DirectFallbackReason,
    MonitoringObservation,
)

__all__ = [
    "HARD_FAILURES",
    "PROXY_FAILURES",
    "SOFT_FAILURES",
    "DirectAttempt",
    "DirectCapability",
    "DirectFallbackReason",
]


# Proxy faults are transport faults of the session's sticky IPRoyal session. They
# never count against the recipe; the proxied browser monitor is the only fallback.
PROXY_FAILURES = frozenset(
    {DirectFallbackReason.PROXY_UNAVAILABLE, DirectFallbackReason.PROXY_FAILED}
)
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
    # A parsed, identity-matching observation that validation refused (for example
    # an unknown or contradictory lifecycle). It is never persisted; it only lets
    # the handler count direct/browser disagreement after the browser fallback.
    rejected_observation: MonitoringObservation | None = field(
        default=None, repr=False, compare=False
    )
    request_seconds: float | None = None

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
