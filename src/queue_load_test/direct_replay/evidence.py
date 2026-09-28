"""Strict consumers for protected Prompt 2 artifacts and browser state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from queue_load_test.direct_replay.models import ReplayCookie, ReplayRecipe
from queue_load_test.state import BrowserState

_REPLAYABLE_CLASSIFICATIONS = frozenset(
    {
        "json_status_candidate",
        "periodic_status_candidate",
        "dom_correlated_status_candidate",
    }
)


class ReplayEvidenceError(ValueError):
    """The protected inputs do not prove a same-session captured request."""


def load_replay_recipe(
    path: Path,
    *,
    session_id: str,
    expected_queue_id: str,
    exchange_sequence: int,
    require_authorized_staging: bool = True,
) -> ReplayRecipe:
    """Load one exact exchange without deriving any endpoint component."""

    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayEvidenceError("discovery evidence is unreadable") from exc
    if not isinstance(artifact, dict):
        raise ReplayEvidenceError("discovery evidence is not an object")
    if artifact.get("schema_version") != 1 or artifact.get("sensitivity") != (
        "SENSITIVE_VISITOR_CREDENTIAL_EVIDENCE"
    ):
        raise ReplayEvidenceError("discovery evidence has an unsupported format")
    scope = artifact.get("scope")
    if not isinstance(scope, str):
        raise ReplayEvidenceError("discovery evidence has no scope")
    if require_authorized_staging and scope != "authorized_queue_it_staging":
        raise ReplayEvidenceError("replay requires authorized Queue-it staging evidence")
    if artifact.get("session_id") != session_id:
        raise ReplayEvidenceError("discovery evidence belongs to another session")
    if artifact.get("expected_queue_id") != expected_queue_id:
        raise ReplayEvidenceError("discovery evidence Queue ID does not match persisted identity")
    raw_exchanges = artifact.get("exchanges")
    if not isinstance(raw_exchanges, list):
        raise ReplayEvidenceError("discovery evidence has no exchanges")
    exchange = next(
        (
            item
            for item in raw_exchanges
            if isinstance(item, dict) and item.get("sequence") == exchange_sequence
        ),
        None,
    )
    if exchange is None:
        raise ReplayEvidenceError("selected browser exchange was not captured")
    if exchange.get("classification") not in _REPLAYABLE_CLASSIFICATIONS:
        raise ReplayEvidenceError("selected exchange is not a status-request candidate")
    url = exchange.get("url")
    method = exchange.get("method")
    if not isinstance(url, str) or not isinstance(method, str):
        raise ReplayEvidenceError("selected exchange has no exact URL or method")
    split = urlsplit(url)
    if split.scheme not in {"http", "https"} or not split.netloc:
        raise ReplayEvidenceError("selected exchange URL is not an absolute HTTP URL")
    headers = _string_mapping(exchange.get("request_headers"))
    body_value = exchange.get("request_body")
    if body_value is not None and not isinstance(body_value, str):
        raise ReplayEvidenceError("selected exchange body is not textual")
    if exchange.get("request_body_truncated") is True:
        raise ReplayEvidenceError("a truncated request body cannot be replayed")
    identifiers = _identifier_mapping(exchange.get("observed_identifiers"))
    queue_ids = identifiers.get("queue_id", ())
    request_material = f"{url}\n{body_value or ''}"
    if expected_queue_id not in queue_ids and expected_queue_id not in request_material:
        raise ReplayEvidenceError("selected request does not carry the authoritative Queue ID")
    digest_material = json.dumps(
        {
            "url": url,
            "method": method,
            "headers": headers,
            "body": body_value,
            "sequence": exchange_sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return ReplayRecipe(
        session_id=session_id,
        expected_queue_id=expected_queue_id,
        source_scope=scope,
        exchange_sequence=exchange_sequence,
        url=url,
        method=method.upper(),
        headers=headers,
        body=body_value.encode("utf-8") if body_value is not None else None,
        observed_identifiers=identifiers,
        fingerprint=hashlib.sha256(digest_material.encode()).hexdigest(),
    )


def cookies_from_browser_state(state: BrowserState) -> tuple[ReplayCookie, ...]:
    """Extract only Playwright storage_state cookies, rejecting malformed state."""

    raw_cookies = state.get("cookies")
    if not isinstance(raw_cookies, list):
        raise ReplayEvidenceError("browser storage_state has no cookie list")
    cookies: list[ReplayCookie] = []
    for raw in raw_cookies:
        if not isinstance(raw, Mapping):
            raise ReplayEvidenceError("browser storage_state contains an invalid cookie")
        name, value, domain = raw.get("name"), raw.get("value"), raw.get("domain")
        if not all(isinstance(item, str) and item for item in (name, value, domain)):
            raise ReplayEvidenceError("browser storage_state contains an incomplete cookie")
        raw_path = raw.get("path", "/")
        path = raw_path if isinstance(raw_path, str) else "/"
        raw_expires = raw.get("expires")
        expires = int(raw_expires) if isinstance(raw_expires, int | float) else None
        cookies.append(
            ReplayCookie(
                name=cast(str, name),
                value=cast(str, value),
                domain=cast(str, domain),
                path=path,
                secure=raw.get("secure") is True,
                expires=expires if expires is not None and expires > 0 else None,
            )
        )
    return tuple(cookies)


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ReplayEvidenceError("selected exchange headers are invalid")
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ReplayEvidenceError("selected exchange headers are invalid")
    return {cast(str, key): cast(str, item) for key, item in value.items()}


def _identifier_mapping(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for key, items in value.items():
        if isinstance(key, str) and isinstance(items, list) and all(
            isinstance(item, str) for item in items
        ):
            result[key] = tuple(cast(list[str], items))
    return result
