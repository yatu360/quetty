"""Evidence-defined direct-response schema; no Queue-it field name is assumed."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

type JSONPathPart = str | int
type JSONPath = tuple[JSONPathPart, ...]


class DirectField(StrEnum):
    QUEUE_ID = "queue_id"
    PROGRESS_PERCENTAGE = "progress_percentage"
    QUEUE_NUMBER = "queue_number"
    USERS_AHEAD = "users_ahead"
    LAST_UPDATED_AT = "last_updated_at"
    QUEUE_PAUSED = "queue_paused"
    EXPECTED_SERVICE_TIME = "expected_service_time"
    ESTIMATED_WAIT_TEXT = "estimated_wait_text"
    FIRST_IN_LINE = "first_in_line"
    SERVICED_SOON = "serviced_soon"
    TURN_STARTED = "turn_started"
    CONNECTION_LOST = "connection_lost"
    PRE_QUEUE = "pre_queue"
    ACTIVE_QUEUE = "active_queue"
    EXPIRED = "expired"
    REDIRECT_URL = "redirect_url"
    POLL_AFTER_SECONDS = "poll_after_seconds"


@dataclass(frozen=True, slots=True)
class DirectResponseSchema:
    """Reviewed JSON paths learned from genuine browser-observed responses."""

    fields: Mapping[DirectField, JSONPath]
    source_scope: str

    def __post_init__(self) -> None:
        normalized = dict(self.fields)
        if DirectField.QUEUE_ID not in normalized:
            raise ValueError("direct response schema must map the authoritative Queue ID")
        if any(not path for path in normalized.values()):
            raise ValueError("direct response schema paths cannot be empty")
        object.__setattr__(self, "fields", MappingProxyType(normalized))


class DirectSchemaError(ValueError):
    """A schema document is invalid or lacks authorised evidence provenance."""


def load_direct_response_schema(
    path: Path, *, require_authorized_staging: bool = True
) -> DirectResponseSchema:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DirectSchemaError("direct response schema is unreadable") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise DirectSchemaError("direct response schema has an unsupported format")
    scope = document.get("source_scope")
    if not isinstance(scope, str):
        raise DirectSchemaError("direct response schema has no evidence scope")
    if require_authorized_staging and scope != "authorized_queue_it_staging":
        raise DirectSchemaError("direct response schema requires authorised staging evidence")
    raw_fields = document.get("fields")
    if not isinstance(raw_fields, Mapping):
        raise DirectSchemaError("direct response schema has no field mappings")
    fields: dict[DirectField, JSONPath] = {}
    try:
        for raw_name, raw_path in raw_fields.items():
            if not isinstance(raw_name, str) or not isinstance(raw_path, list):
                raise TypeError
            field = DirectField(raw_name)
            if not raw_path or not all(isinstance(part, str | int) for part in raw_path):
                raise TypeError
            fields[field] = tuple(cast(list[JSONPathPart], raw_path))
    except (TypeError, ValueError) as exc:
        raise DirectSchemaError("direct response schema contains an invalid mapping") from exc
    try:
        return DirectResponseSchema(fields, scope)
    except ValueError as exc:
        raise DirectSchemaError(str(exc)) from exc
