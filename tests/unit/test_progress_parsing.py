from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from queue_load_test.queue_monitor import (
    parse_connection_lost,
    parse_estimated_wait_text,
    parse_expected_service_time,
    parse_first_in_line,
    parse_last_updated,
    parse_progress_percentage,
    parse_queue_number,
    parse_queue_paused,
    parse_serviced_soon,
    parse_turn_started,
    parse_users_ahead,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42%", 42.0),
        ("Progress: 37.5 %", 37.5),
        ("12,5", 12.5),
        (0, 0.0),
        ("not available", None),
        ("12..5%", None),
        ("101%", None),
        ("-1%", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_progress_percentage(raw: str | int | None, expected: float | None) -> None:
    assert parse_progress_percentage(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234 users ahead", 1234),
        ("Users ahead: 0", 0),
        (42, 42),
        ("twelve", None),
        ("12.5", None),
        ("1,23 users ahead", None),
        ("12people", None),
        ("-2", None),
        (None, None),
        (" ", None),
    ],
)
def test_parse_users_ahead(raw: str | int | None, expected: int | None) -> None:
    assert parse_users_ahead(raw) == expected


def test_text_parsers_normalize_layout_whitespace_and_labels() -> None:
    assert parse_queue_number("  Your queue number: Q-123  ") == "Q-123"
    assert parse_estimated_wait_text("  About\n  12 minutes ") == "About 12 minutes"
    assert parse_queue_number("N/A") is None


@pytest.mark.parametrize(
    "parser",
    [
        parse_progress_percentage,
        parse_queue_number,
        parse_users_ahead,
        parse_estimated_wait_text,
        parse_expected_service_time,
        parse_last_updated,
        parse_queue_paused,
        parse_first_in_line,
        parse_serviced_soon,
        parse_turn_started,
        parse_connection_lost,
    ],
)
def test_missing_blank_and_hidden_values_return_none(parser: object) -> None:
    assert parser(None) is None  # type: ignore[operator]
    assert parser("   ") is None  # type: ignore[operator]
    assert parser("otherwise valid", visible=False) is None  # type: ignore[operator]


def test_timestamp_parsers_support_common_layout_values() -> None:
    assert parse_expected_service_time("2026-09-26T14:30:00Z") == datetime(
        2026, 9, 26, 14, 30, tzinfo=UTC
    )
    assert parse_last_updated("Last updated: 26/09/2026 14:31") == datetime(
        2026, 9, 26, 14, 31, tzinfo=UTC
    )
    assert parse_expected_service_time(
        "14:30", reference_date=date(2026, 9, 26)
    ) == datetime(2026, 9, 26, 14, 30, tzinfo=UTC)


def test_malformed_or_date_less_timestamps_return_none() -> None:
    assert parse_expected_service_time("sometime later") is None
    assert parse_last_updated("14:31") is None


@pytest.mark.parametrize(
    ("parser", "positive", "negative"),
    [
        (parse_queue_paused, "The queue is paused", "The queue is not paused"),
        (parse_first_in_line, "You are first in line", "You are not first in line"),
        (parse_serviced_soon, "You will be serviced soon", "Not being serviced soon"),
        (parse_turn_started, "Your turn has started", "Your turn has not started"),
        (parse_connection_lost, "Connection lost", "Connection restored"),
    ],
)
def test_boolean_signal_parsers(
    parser: object,
    positive: str,
    negative: str,
) -> None:
    assert parser(positive) is True  # type: ignore[operator]
    assert parser(negative) is False  # type: ignore[operator]
    assert parser("unrecognized layout text") is None  # type: ignore[operator]


def test_disconnected_is_not_misread_as_connected() -> None:
    assert parse_connection_lost("Browser disconnected") is True


@pytest.mark.parametrize(
    ("raw", "hour", "minute"),
    [("2:45 PM", 14, 45), ("2:45PM", 14, 45), ("12:05 am", 0, 5), ("11:59:30 PM", 23, 59)],
)
def test_twelve_hour_clock_times_use_the_reference_date(raw: str, hour: int, minute: int) -> None:
    from datetime import date

    from queue_load_test.queue_monitor.parsing import parse_expected_service_time

    parsed = parse_expected_service_time(raw, reference_date=date(2026, 9, 29))
    assert parsed is not None
    assert (parsed.date(), parsed.hour, parsed.minute) == (date(2026, 9, 29), hour, minute)


def test_invalid_twelve_hour_clock_time_is_not_invented() -> None:
    from datetime import date

    from queue_load_test.queue_monitor.parsing import parse_expected_service_time

    assert parse_expected_service_time("13:45 PM", reference_date=date(2026, 9, 29)) is None


def test_turn_started_phrase_from_the_confirmation_dialog() -> None:
    from queue_load_test.queue_monitor.parsing import parse_turn_started

    assert parse_turn_started("Your turn started at 14:30 Please confirm") is True
    assert parse_turn_started("Your turn has not started") is False


# ------------------------------------------------------------- timezone handling

LONDON = ZoneInfo("Europe/London")


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)  # type: ignore[arg-type, misc]


@pytest.mark.parametrize(
    ("now", "raw", "expected"),
    [
        # Winter: Europe/London is GMT (UTC+0).
        (_utc(2026, 1, 15, 14, 0), "14:30", _utc(2026, 1, 15, 14, 30)),
        (_utc(2026, 1, 15, 14, 0), "2:30 PM", _utc(2026, 1, 15, 14, 30)),
        # Summer: Europe/London is BST (UTC+1); 14:30 on the page is 13:30 UTC.
        (_utc(2026, 7, 15, 13, 0), "14:30", _utc(2026, 7, 15, 13, 30)),
        (_utc(2026, 9, 29, 22, 12), "11:13PM", _utc(2026, 9, 29, 22, 13)),
        # DST starts 29 March 2026 at 01:00 UTC; 03:00 BST is 02:00 UTC.
        (_utc(2026, 3, 29, 1, 30), "03:00", _utc(2026, 3, 29, 2, 0)),
        # Midnight rollover picks the nearest occurrence, not "today".
        (_utc(2026, 1, 15, 23, 58), "00:05", _utc(2026, 1, 16, 0, 5)),
        (_utc(2026, 1, 16, 0, 2), "23:59", _utc(2026, 1, 15, 23, 59)),
    ],
)
def test_time_only_values_use_the_page_timezone_and_nearest_date(
    now: datetime, raw: str, expected: datetime
) -> None:
    from queue_load_test.queue_monitor.parsing import (
        parse_expected_service_time,
        parse_last_updated,
    )

    assert parse_expected_service_time(raw, zone=LONDON, now=now) == expected
    assert parse_last_updated(raw, zone=LONDON, now=now) == expected


def test_full_timestamps_use_the_zone_only_when_they_carry_no_offset() -> None:
    from queue_load_test.queue_monitor.parsing import parse_last_updated

    assert parse_last_updated("2026-07-01 14:30", zone=LONDON) == _utc(2026, 7, 1, 13, 30)
    assert parse_last_updated("2026-01-01 14:30", zone=LONDON) == _utc(2026, 1, 1, 14, 30)
    assert parse_last_updated("2026-07-01T14:30:00+02:00", zone=LONDON) == _utc(2026, 7, 1, 12, 30)
    assert parse_last_updated("2026-07-01T14:30:00Z", zone=LONDON) == _utc(2026, 7, 1, 14, 30)
    # Without a zone the historical UTC interpretation is unchanged.
    assert parse_last_updated("2026-07-01 14:30") == _utc(2026, 7, 1, 14, 30)


@pytest.mark.parametrize(
    ("label", "offset_hours"),
    [
        ("(GMT+01:00)", 1),
        ("GMT+1", 1),
        ("UTC-05:00", -5),
        ("(UTC)", 0),
        ("GMT", 0),
        ("BST", 1),
        ("utc +5:30", 5.5),
    ],
)
def test_page_timezone_labels_are_parsed(label: str, offset_hours: float) -> None:
    from datetime import timedelta

    from queue_load_test.queue_monitor.parsing import parse_timezone_label

    zone = parse_timezone_label(label)
    assert zone is not None
    assert zone.utcoffset(None) == timedelta(hours=offset_hours)


def test_iana_labels_follow_daylight_saving_and_unknown_labels_are_not_guessed() -> None:
    from datetime import timedelta

    from queue_load_test.queue_monitor.parsing import parse_timezone_label

    zone = parse_timezone_label("Europe/London")
    assert zone is not None
    assert zone.utcoffset(_utc(2026, 7, 1).replace(tzinfo=None)) == timedelta(hours=1)
    assert zone.utcoffset(_utc(2026, 1, 1).replace(tzinfo=None)) == timedelta(0)
    for unknown in ("CET", "EST", "local time", "GMT+99", "", None):
        assert parse_timezone_label(unknown) is None
