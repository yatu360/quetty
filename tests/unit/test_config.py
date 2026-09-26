from pathlib import Path

import pytest
from pydantic import ValidationError

from queue_load_test.config import Settings
from queue_load_test.models import SessionMode


def settings_kwargs(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "STAGING_URL": "https://staging.example.test",
        "TARGET_QUEUE_IDS": 10,
        "SESSION_MODE": "HYBRID",
        "CHROME_PROCESS_COUNT": 1,
        "MAX_CONTEXTS_PER_BROWSER": 5,
        "MAX_ACTIVE_CONTEXTS": 5,
        "CREATION_WORKERS": 1,
        "MONITOR_WORKERS": 1,
        "QUEUE_POLL_SECONDS": 30.0,
        "POLL_JITTER_SECONDS": 5.0,
        "HEADLESS": True,
        "DATABASE_URL": "sqlite:///queue_load_test.sqlite3",
        "STATE_DIRECTORY": ".browser-state",
        "PROMETHEUS_PORT": 9090,
    }
    values.update(overrides)
    return values


def test_valid_initial_configuration() -> None:
    settings = Settings(**settings_kwargs())

    assert settings.target_queue_ids == 10
    assert settings.session_mode is SessionMode.HYBRID
    assert settings.chrome_process_count == 1
    assert settings.max_active_contexts == 5
    assert settings.queue_poll_seconds == 30
    assert settings.state_directory == Path(".browser-state")


def test_rejects_active_contexts_above_browser_capacity() -> None:
    with pytest.raises(ValidationError, match="MAX_ACTIVE_CONTEXTS"):
        Settings(**settings_kwargs(MAX_ACTIVE_CONTEXTS=6))


def test_rejects_non_sqlite_database_url() -> None:
    with pytest.raises(ValidationError, match="sqlite"):
        Settings(**settings_kwargs(DATABASE_URL="postgresql://localhost/queue"))


def test_rejects_non_http_staging_url() -> None:
    with pytest.raises(ValidationError, match="STAGING_URL"):
        Settings(**settings_kwargs(STAGING_URL="ftp://staging.example.test"))


def test_rejects_jitter_equal_to_poll_interval() -> None:
    with pytest.raises(ValidationError, match="POLL_JITTER_SECONDS"):
        Settings(**settings_kwargs(QUEUE_POLL_SECONDS=5.0, POLL_JITTER_SECONDS=5.0))


def test_rejects_inverted_adaptive_poll_range() -> None:
    with pytest.raises(ValidationError, match="PRE_QUEUE_POLL_MIN_SECONDS"):
        Settings(
            **settings_kwargs(
                PRE_QUEUE_POLL_MIN_SECONDS=301,
                PRE_QUEUE_POLL_MAX_SECONDS=300,
            )
        )


def test_hybrid_and_transfer_only_mode_parsing() -> None:
    assert Settings(**settings_kwargs(SESSION_MODE="HYBRID")).session_mode is SessionMode.HYBRID
    assert (
        Settings(**settings_kwargs(SESSION_MODE="transfer-only")).session_mode
        is SessionMode.TRANSFER_ONLY
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CHROME_PROCESS_COUNT", 0),
        ("MAX_CONTEXTS_PER_BROWSER", 0),
        ("CREATION_WORKERS", 6),
        ("MONITOR_WORKERS", 6),
        ("PROMETHEUS_PORT", 65_536),
    ],
)
def test_rejects_invalid_or_contradictory_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**settings_kwargs(**{field: value}))


def test_rejects_unknown_session_mode() -> None:
    with pytest.raises(ValidationError, match="Unknown SessionMode"):
        Settings(**settings_kwargs(SESSION_MODE="browser-only"))
