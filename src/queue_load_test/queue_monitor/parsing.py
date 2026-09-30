"""Pure, defensive parsers for text extracted from Queue-it layouts."""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


_OFFSET_LABEL = re.compile(r"^(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
# Only unambiguous zone words are accepted; other abbreviations are never guessed.
_NAMED_LABELS: dict[str, tzinfo] = {
    "UTC": UTC,
    "GMT": UTC,
    "Z": UTC,
    "BST": timezone(timedelta(hours=1), "BST"),
}


def parse_timezone_label(value: RawValue) -> tzinfo | None:
    """Parse a page's own timezone label ("GMT+01:00", "(UTC)", "BST", "Europe/London")."""

    text = _visible_text(value, True) if not isinstance(value, datetime) else None
    if text is None:
        return None
    text = text.strip("()[] ").strip()
    named = _NAMED_LABELS.get(text.upper())
    if named is not None:
        return named
    match = _OFFSET_LABEL.match(text)
    if match is not None:
        sign, hours, minutes = match.groups()
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        if offset > timedelta(hours=14):
            return None
        return timezone(offset if sign == "+" else -offset)
    if "/" in text:
        try:
            return ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return None


def _parse_datetime(
    value: RawValue,
    *,
    visible: bool,
    reference_date: date | None = None,
    zone: tzinfo | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Parse a displayed timestamp and return it in UTC.

    Values without their own offset are interpreted in ``zone`` (the page's timezone;
    UTC when unknown). A time without a date takes the occurrence nearest to ``now``,
    so "00:05" read at 23:58 means the next day; ``reference_date`` is the older
    fallback when no ``now`` is supplied.
    """

    local = zone or UTC
    if not visible or value is None:
        return None
    if isinstance(value, datetime):
        return _to_utc(value, local)

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
        parsed: datetime | None = datetime.fromisoformat(candidate)
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
                parsed = datetime.strptime(text_value, pattern).replace(tzinfo=local)
                break
            except ValueError:
                continue

    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text_value)
        except (TypeError, ValueError, OverflowError):
            parsed = None

    if parsed is None and (now is not None or reference_date is not None):
        # 24-hour and 12-hour clock forms ("14:45", "2:45 PM", "2:45PM").
        compact = re.sub(r"\s+(?=[AaPp][Mm]$)", "", text_value)
        for pattern in ("%H:%M:%S", "%H:%M", "%I:%M:%S%p", "%I:%M%p"):
            try:
                parsed_time = datetime.strptime(compact, pattern).replace(tzinfo=local).time()
            except ValueError:
                continue
            if now is not None:
                parsed = _nearest_occurrence(parsed_time, local, now)
            else:
                assert reference_date is not None
                parsed = datetime.combine(reference_date, parsed_time, tzinfo=local)
            break

    if parsed is None:
        return None
    return _to_utc(parsed, local)


def _nearest_occurrence(clock_time: time, zone: tzinfo, now: datetime) -> datetime:
    local_now = now.astimezone(zone)
    candidates = [
        datetime.combine(local_now.date() + timedelta(days=offset), clock_time, tzinfo=zone)
        for offset in (-1, 0, 1)
    ]
    return min(candidates, key=lambda candidate: abs(candidate - now))


def _to_utc(value: datetime, zone: tzinfo) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=zone)
    return aware.astimezone(UTC)


def parse_expected_service_time(
    value: RawValue,
    *,
    visible: bool = True,
    reference_date: date | None = None,
    zone: tzinfo | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Parse an expected-service timestamp without inventing a missing date."""

    return _parse_datetime(
        value, visible=visible, reference_date=reference_date, zone=zone, now=now
    )


def parse_last_updated(
    value: RawValue,
    *,
    visible: bool = True,
    reference_date: date | None = None,
    zone: tzinfo | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Parse a Queue-it last-updated timestamp."""

    return _parse_datetime(
        value, visible=visible, reference_date=reference_date, zone=zone, now=now
    )


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
        true_phrases=("turn has started", "it is your turn", "your turn started"),
        false_phrases=("turn has not started",),
    )


def parse_connection_lost(value: RawValue, *, visible: bool = True) -> bool | None:
    return _parse_boolean(
        value,
        visible=visible,
        true_phrases=("connection lost", "lost connection", "disconnected"),
        false_phrases=("connection restored",),
    )
