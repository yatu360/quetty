"""Conservative state-stability classification from protected discovery evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from queue_load_test.direct_replay.models import StabilityFinding, ValueStability

_CANDIDATES = frozenset(
    {"json_status_candidate", "periodic_status_candidate", "dom_correlated_status_candidate"}
)


def classify_artifact_values(path: Path) -> tuple[StabilityFinding, ...]:
    """Classify only patterns supported within one captured session."""

    value = json.loads(path.read_text(encoding="utf-8"))
    exchanges = value.get("exchanges", []) if isinstance(value, Mapping) else []
    candidates = [
        item
        for item in exchanges
        if isinstance(item, Mapping) and item.get("classification") in _CANDIDATES
    ]
    queue_id = value.get("expected_queue_id") if isinstance(value, Mapping) else None
    repeated = len(candidates) >= 3
    queue_seen = bool(queue_id) and repeated and all(
        str(queue_id) in str(item.get("url", ""))
        or str(queue_id) in str(item.get("request_body", ""))
        or str(queue_id) in str(item.get("observed_identifiers", {}))
        for item in candidates
    )
    query_variants = {_canonical(item.get("query")) for item in candidates}
    body_variants = {_canonical(item.get("request_json")) for item in candidates}
    refreshed_cookies = any(bool(item.get("set_cookie")) for item in candidates)
    return (
        _unknown("host/path", "Cross-session same-event evidence is required."),
        _unknown("customerId", "Cross-session same-event evidence is required."),
        _unknown("eventId", "Cross-session same-event evidence is required."),
        StabilityFinding(
            "Queue ID",
            ValueStability.SESSION_STABLE if queue_seen else ValueStability.UNKNOWN,
            (
                "The authoritative value was present across at least three captured requests."
                if queue_seen
                else "Fewer than three consistent captured requests prove this classification."
            ),
        ),
        StabilityFinding(
            "request body fields",
            ValueStability.REQUEST_TRANSIENT
            if repeated and len(body_variants) > 1
            else ValueStability.UNKNOWN,
            (
                "Browser-observed request JSON changed across at least three requests."
                if repeated and len(body_variants) > 1
                else "No repeated browser evidence proves stable or rotating body values."
            ),
        ),
        StabilityFinding(
            "query parameters",
            ValueStability.REQUEST_TRANSIENT
            if repeated and len(query_variants) > 1
            else ValueStability.UNKNOWN,
            (
                "Browser-observed query values changed across at least three requests."
                if repeated and len(query_variants) > 1
                else "No repeated browser evidence proves stable or rotating query values."
            ),
        ),
        _unknown("request cookies", "Per-cookie repeated ablation evidence is required."),
        _unknown("CSRF/session tokens", "No token semantics are assumed from names or shape."),
        StabilityFinding(
            "response cookies",
            ValueStability.RESPONSE_REFRESHED
            if refreshed_cookies
            else ValueStability.UNKNOWN,
            (
                "At least one candidate response browser capture contained Set-Cookie."
                if refreshed_cookies
                else "No response refresh was observed; absence does not establish stability."
            ),
        ),
        _unknown("polling/version fields", "Field semantics require authorised repeated evidence."),
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _unknown(value: str, basis: str) -> StabilityFinding:
    return StabilityFinding(value, ValueStability.UNKNOWN, basis)
