"""Direct Monitoring capability and sanitized failure-class enums (no sensitive data)."""

from enum import StrEnum


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
