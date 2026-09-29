"""Normalize evidence-mapped direct JSON into the common monitoring observation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Never

from queue_load_test.models import (
    MonitoringObservation,
    ObservationSource,
    QueuePageSignals,
    QueueProgress,
)
from queue_load_test.observation_equivalence.schema import (
    DirectField,
    DirectResponseSchema,
    JSONPath,
)

type AdmissionMatcher = Callable[[str], bool]

_MISSING = object()


class DirectObservationFailure(StrEnum):
    SCHEMA = "schema"
    IDENTITY_AMBIGUITY = "identity_ambiguity"
    IDENTITY_MISMATCH = "identity_mismatch"


class DirectObservationError(ValueError):
    def __init__(self, failure: DirectObservationFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


class DirectResponseParser:
    """Strict parser whose field paths come only from a reviewed evidence schema."""

    def __init__(
        self,
        schema: DirectResponseSchema,
        *,
        admission_matcher: AdmissionMatcher | None = None,
    ) -> None:
        self._schema = schema
        self._admission_matcher = admission_matcher

    def parse(
        self,
        payload: object,
        *,
        session_id: str,
        expected_queue_id: str,
        observed_at: datetime | None = None,
    ) -> MonitoringObservation:
        if not isinstance(payload, Mapping):
            raise DirectObservationError(
                DirectObservationFailure.SCHEMA, "direct response JSON is not an object"
            )
        queue_id = self._value(payload, DirectField.QUEUE_ID)
        if not isinstance(queue_id, str) or not queue_id:
            raise DirectObservationError(
                DirectObservationFailure.IDENTITY_AMBIGUITY,
                "direct response has no unambiguous Queue ID",
            )
        if queue_id != expected_queue_id:
            raise DirectObservationError(
                DirectObservationFailure.IDENTITY_MISMATCH,
                "direct response Queue ID contradicts persisted identity",
            )
        missing = tuple(
            field.value
            for field in self._schema.fields
            if field is not DirectField.QUEUE_ID and self._value(payload, field) is _MISSING
        )
        redirect = self._optional_string(payload, DirectField.REDIRECT_URL)
        admitted = (
            self._admission_matcher(redirect)
            if redirect is not None and self._admission_matcher is not None
            else None
        )
        try:
            progress = QueueProgress(
                session_id=session_id,
                progress_percentage=self._optional_number(
                    payload, DirectField.PROGRESS_PERCENTAGE
                ),
                queue_number=self._optional_identifier(payload, DirectField.QUEUE_NUMBER),
                users_ahead=self._optional_integer(payload, DirectField.USERS_AHEAD),
                last_updated_at=self._optional_datetime(payload, DirectField.LAST_UPDATED_AT),
                queue_paused=self._optional_boolean(payload, DirectField.QUEUE_PAUSED),
                expected_service_time=self._optional_datetime(
                    payload, DirectField.EXPECTED_SERVICE_TIME
                ),
                estimated_wait_text=self._optional_string(
                    payload, DirectField.ESTIMATED_WAIT_TEXT
                ),
                first_in_line=self._optional_boolean(payload, DirectField.FIRST_IN_LINE),
                serviced_soon=self._optional_boolean(payload, DirectField.SERVICED_SOON),
                turn_started=self._optional_boolean(payload, DirectField.TURN_STARTED),
                connection_lost=self._optional_boolean(
                    payload, DirectField.CONNECTION_LOST
                ),
                pre_queue=self._optional_boolean(payload, DirectField.PRE_QUEUE),
                active_queue=self._optional_boolean(payload, DirectField.ACTIVE_QUEUE),
            )
        except ValueError as exc:
            raise DirectObservationError(
                DirectObservationFailure.SCHEMA,
                "direct response contains an invalid normalized progress value",
            ) from exc
        return MonitoringObservation(
            source=ObservationSource.DIRECT_RESPONSE,
            session_id=session_id,
            observed_at=observed_at or datetime.now(UTC),
            expected_queue_id=expected_queue_id,
            observed_queue_id=queue_id,
            identity_match=True,
            progress=progress,
            page=QueuePageSignals(
                expired=self._optional_boolean(payload, DirectField.EXPIRED),
                admitted=admitted,
                pre_queue=progress.pre_queue,
                active_queue=progress.active_queue,
            ),
            redirect_present=redirect is not None,
            poll_after_seconds=self._optional_nonnegative_number(
                payload, DirectField.POLL_AFTER_SECONDS
            ),
            missing_fields=missing,
            unknown_fields=_unknown_leaf_paths(payload, set(self._schema.fields.values())),
        )

    def _value(self, payload: Mapping[object, object], field: DirectField) -> object:
        path = self._schema.fields.get(field)
        if path is None:
            return _MISSING
        current: object = payload
        for part in path:
            if isinstance(part, str) and isinstance(current, Mapping):
                if part not in current:
                    return _MISSING
                current = current[part]
            elif isinstance(part, int) and isinstance(current, list):
                if part < 0 or part >= len(current):
                    return _MISSING
                current = current[part]
            else:
                return _MISSING
        return current

    def _optional_string(self, payload: Mapping[object, object], field: DirectField) -> str | None:
        value = self._value(payload, field)
        if value is _MISSING or value is None:
            return None
        if not isinstance(value, str):
            self._schema_failure(field)
        return value

    def _optional_identifier(
        self, payload: Mapping[object, object], field: DirectField
    ) -> str | None:
        value = self._value(payload, field)
        if value is _MISSING or value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, str | int):
            self._schema_failure(field)
        return str(value)

    def _optional_number(
        self, payload: Mapping[object, object], field: DirectField
    ) -> float | None:
        value = self._value(payload, field)
        if value is _MISSING or value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            self._schema_failure(field)
        return float(value)

    def _optional_nonnegative_number(
        self, payload: Mapping[object, object], field: DirectField
    ) -> float | None:
        value = self._optional_number(payload, field)
        if value is not None and value < 0:
            self._schema_failure(field)
        return value

    def _optional_integer(
        self, payload: Mapping[object, object], field: DirectField
    ) -> int | None:
        value = self._value(payload, field)
        if value is _MISSING or value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self._schema_failure(field)
        return value

    def _optional_boolean(
        self, payload: Mapping[object, object], field: DirectField
    ) -> bool | None:
        value = self._value(payload, field)
        if value is _MISSING or value is None:
            return None
        if not isinstance(value, bool):
            self._schema_failure(field)
        return value

    def _optional_datetime(
        self, payload: Mapping[object, object], field: DirectField
    ) -> datetime | None:
        value = self._value(payload, field)
        if value is _MISSING or value is None:
            return None
        if not isinstance(value, str):
            self._schema_failure(field)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            self._schema_failure(field)
        if parsed.tzinfo is None:
            self._schema_failure(field)
        return parsed

    @staticmethod
    def _schema_failure(field: DirectField) -> Never:
        raise DirectObservationError(
            DirectObservationFailure.SCHEMA,
            f"direct response field {field.value!r} has an unexpected type",
        )


def _unknown_leaf_paths(
    payload: Mapping[object, object], known: set[JSONPath]
) -> tuple[str, ...]:
    leaves = _leaf_paths(payload)
    return tuple(sorted(_display_path(path) for path in leaves if path not in known))


def _leaf_paths(value: object, prefix: JSONPath = ()) -> set[JSONPath]:
    if isinstance(value, Mapping):
        return {
            path
            for key, child in value.items()
            for path in _leaf_paths(child, prefix + (str(key),))
        }
    if isinstance(value, list):
        return {
            path
            for index, child in enumerate(value)
            for path in _leaf_paths(child, prefix + (index,))
        }
    return {prefix}


def _display_path(path: JSONPath) -> str:
    return ".".join(str(part) for part in path)
