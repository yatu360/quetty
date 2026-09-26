"""Pure, defensive parsers for text extracted from Queue-it layouts."""

import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

RawValue = str | int | float | bool | datetime | None

_WHITESPACE = re.compile(r"\s+")
_PERCENTAGE = re.compile(r"(?<![\w.])(-?\d+(?:[.,]\d+)?)\s*%?(?![\w.])")
_INTEGER = re.compile(r"(?<![\w.])-?(?:\d[\d,]*\d|\d)(?![\w.])")
_MISSING_TEXT = frozenset({"-", "--", "n/a", "na", "none", "null", "unknown"})


def _visible_text(value: RawValue, visible: bool) -> str | None:
    if not visible or value is None:
        return None
    text = _WHITESPACE.sub(" ", str(value)).strip()
    return text if text and text.casefold() not in _MISSING_TEXT else None


def parse_progress_percentage(value: RawValue, *, visible: bool = True) -> float | None:
    """Parse a percentage or aria-valuenow-style number into the range 0..100."""

    text = _visible_text(value, visible)
    if text is None:
        return None
    match = _PERCENTAGE.search(text)
    if match is None:
        return None
    number_text = match.group(1)
    if "," in number_text and "." not in number_text:
        number_text = number_text.replace(",", ".")
    try:
        parsed = float(number_text)
    except ValueError:
        return None
    return parsed if 0 <= parsed <= 100 else None


def parse_queue_number(value: RawValue, *, visible: bool = True) -> str | None:
    """Return a normalized displayed queue number without assuming its format."""

    text = _visible_text(value, visible)
    if text is None:
        return None
    return re.sub(
        r"^(?:your\s+)?queue\s+(?:id|number)(?:\s+is)?\s*[:#]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ) or None


def parse_users_ahead(value: RawValue, *, visible: bool = True) -> int | None:
    """Parse an integer from plain or labelled users-ahead text."""

    text = _visible_text(value, visible)
    if text is None:
        return None
    match = _INTEGER.search(text)
    if match is None:
        return None
    number_text = match.group(0)
    unsigned_number = number_text.removeprefix("-")
    if "," in unsigned_number and re.fullmatch(r"\d{1,3}(?:,\d{3})+", unsigned_number) is None:
        return None
    try:
        parsed = int(number_text.replace(",", ""))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_estimated_wait_text(value: RawValue, *, visible: bool = True) -> str | None:
    """Normalize human-readable wait text while preserving its wording."""

    return _visible_text(value, visible)


def _parse_datetime(
    value: RawValue,
    *,
    visible: bool,
    reference_date: date | None = None,
) -> datetime | None:
    if not visible or value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    text_value = _visible_text(value, visible)
    if text_value is None:
        return None
    text_value = re.sub(
        r"^(?:last updated|expected service time|estimated service time)\s*:?\s*",
        "",
        text_value,
        flags=re.IGNORECASE,
    )
    candidate = text_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None

    if parsed is None:
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
        ):
            try:
                parsed = datetime.strptime(text_value, pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue

    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text_value)
        except (TypeError, ValueError, OverflowError):
            parsed = None

    if parsed is None and reference_date is not None:
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(text_value, pattern).replace(tzinfo=UTC).time()
                parsed = datetime.combine(reference_date, parsed_time, tzinfo=UTC)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_expected_service_time(
    value: RawValue,
    *,
    visible: bool = True,
    reference_date: date | None = None,
) -> datetime | None:
    """Parse an expected-service timestamp without inventing a missing date."""

    return _parse_datetime(value, visible=visible, reference_date=reference_date)


def parse_last_updated(
    value: RawValue,
    *,
    visible: bool = True,
    reference_date: date | None = None,
) -> datetime | None:
    """Parse a Queue-it last-updated timestamp."""

    return _parse_datetime(value, visible=visible, reference_date=reference_date)


def _parse_boolean(
    value: RawValue,
    *,
    visible: bool,
    true_phrases: tuple[str, ...],
    false_phrases: tuple[str, ...],
) -> bool | None:
    if not visible or value is None:
        return None
    if isinstance(value, bool):
        return value
    text = _visible_text(value, visible)
    if text is None:
        return None
    normalized = text.casefold()
    if normalized in {"false", "no", "0"} or any(
        phrase in normalized for phrase in false_phrases
    ):
        return False
    if normalized in {"true", "yes", "1"} or any(
        phrase in normalized for phrase in true_phrases
    ):
        return True
    return None


def parse_queue_paused(value: RawValue, *, visible: bool = True) -> bool | None:
    return _parse_boolean(
        value,
        visible=visible,
        true_phrases=("queue is paused", "queue has been paused"),
        false_phrases=("queue is not paused", "queue has resumed"),
    )


def parse_first_in_line(value: RawValue, *, visible: bool = True) -> bool | None:
    return _parse_boolean(
        value,
        visible=visible,
        true_phrases=("first in line", "front of the line"),
        false_phrases=("not first in line",),
    )


def parse_serviced_soon(value: RawValue, *, visible: bool = True) -> bool | None:
    return _parse_boolean(
        value,
        visible=visible,
        true_phrases=("serviced soon", "almost your turn"),
        false_phrases=("not being serviced soon",),
    )


def parse_turn_started(value: RawValue, *, visible: bool = True) -> bool | None:
    return _parse_boolean(
        value,
        visible=visible,
        true_phrases=("turn has started", "it is your turn"),
        false_phrases=("turn has not started",),
    )


def parse_connection_lost(value: RawValue, *, visible: bool = True) -> bool | None:
    return _parse_boolean(
        value,
        visible=visible,
        true_phrases=("connection lost", "lost connection", "disconnected"),
        false_phrases=("connection restored",),
    )
