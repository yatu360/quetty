"""Typed runtime configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from queue_load_test.models.session import SessionMode


class Settings(BaseSettings):
    """Environment-backed application settings."""

    staging_url: HttpUrl = Field(alias="STAGING_URL")
    target_queue_ids: int = Field(default=10, alias="TARGET_QUEUE_IDS", ge=1)
    session_mode: SessionMode = Field(default=SessionMode.HYBRID, alias="SESSION_MODE")
    chrome_process_count: int = Field(default=1, alias="CHROME_PROCESS_COUNT", ge=1)
    max_contexts_per_browser: int = Field(default=5, alias="MAX_CONTEXTS_PER_BROWSER", ge=1)
    max_active_contexts: int = Field(default=5, alias="MAX_ACTIVE_CONTEXTS", ge=1)
    creation_workers: int = Field(default=1, alias="CREATION_WORKERS", ge=1)
    monitor_workers: int = Field(default=1, alias="MONITOR_WORKERS", ge=1)
    monitor_queue_capacity: int = Field(default=5, alias="MONITOR_QUEUE_CAPACITY", ge=1)
    monitor_claim_batch_size: int = Field(default=5, alias="MONITOR_CLAIM_BATCH_SIZE", ge=1)
    monitor_lease_seconds: float = Field(default=120.0, alias="MONITOR_LEASE_SECONDS", gt=0)
    monitor_scheduler_tick_seconds: float = Field(
        default=1.0, alias="MONITOR_SCHEDULER_TICK_SECONDS", gt=0
    )
    queue_poll_seconds: float = Field(default=30.0, alias="QUEUE_POLL_SECONDS", gt=0)
    poll_jitter_seconds: float = Field(default=5.0, alias="POLL_JITTER_SECONDS", ge=0)
    pre_queue_poll_min_seconds: float = Field(
        default=60.0, alias="PRE_QUEUE_POLL_MIN_SECONDS", gt=0
    )
    pre_queue_poll_max_seconds: float = Field(
        default=300.0, alias="PRE_QUEUE_POLL_MAX_SECONDS", gt=0
    )
    active_early_poll_min_seconds: float = Field(
        default=60.0, alias="ACTIVE_EARLY_POLL_MIN_SECONDS", gt=0
    )
    active_early_poll_max_seconds: float = Field(
        default=120.0, alias="ACTIVE_EARLY_POLL_MAX_SECONDS", gt=0
    )
    active_mid_poll_min_seconds: float = Field(
        default=30.0, alias="ACTIVE_MID_POLL_MIN_SECONDS", gt=0
    )
    active_mid_poll_max_seconds: float = Field(
        default=60.0, alias="ACTIVE_MID_POLL_MAX_SECONDS", gt=0
    )
    serviced_soon_poll_min_seconds: float = Field(
        default=10.0, alias="SERVICED_SOON_POLL_MIN_SECONDS", gt=0
    )
    serviced_soon_poll_max_seconds: float = Field(
        default=30.0, alias="SERVICED_SOON_POLL_MAX_SECONDS", gt=0
    )
    active_mid_progress_percentage: float = Field(
        default=50.0, alias="ACTIVE_MID_PROGRESS_PERCENTAGE", ge=0, le=100
    )
    turn_started_poll_seconds: float = Field(default=0.0, alias="TURN_STARTED_POLL_SECONDS", ge=0)
    stale_update_seconds: float = Field(default=180.0, alias="STALE_UPDATE_SECONDS", gt=0)
    headless: bool = Field(default=True, alias="HEADLESS")
    database_url: str = Field(default="sqlite:///queue_load_test.sqlite3", alias="DATABASE_URL")
    state_directory: Path = Field(default=Path(".browser-state"), alias="STATE_DIRECTORY")
    prometheus_port: int = Field(default=9090, alias="PROMETHEUS_PORT", ge=1, le=65535)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("database_url")
    @classmethod
    def require_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must use sqlite:///")
        return value

    @field_validator("session_mode", mode="before")
    @classmethod
    def parse_session_mode(cls, value: object) -> SessionMode:
        if not isinstance(value, str | SessionMode):
            # Pydantic reports ValueError as configuration validation failure.
            raise ValueError("SESSION_MODE must be a string")  # noqa: TRY004
        return SessionMode.parse(value)

    @model_validator(mode="after")
    def validate_capacity(self) -> "Settings":
        total_capacity = self.chrome_process_count * self.max_contexts_per_browser
        if self.max_active_contexts > total_capacity:
            raise ValueError(
                "MAX_ACTIVE_CONTEXTS cannot exceed "
                "CHROME_PROCESS_COUNT * MAX_CONTEXTS_PER_BROWSER"
            )

        if self.creation_workers > self.max_active_contexts:
            raise ValueError("CREATION_WORKERS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.monitor_workers > self.max_active_contexts:
            raise ValueError("MONITOR_WORKERS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.monitor_claim_batch_size > self.monitor_queue_capacity:
            raise ValueError("MONITOR_CLAIM_BATCH_SIZE cannot exceed MONITOR_QUEUE_CAPACITY")

        if self.poll_jitter_seconds >= self.queue_poll_seconds:
            raise ValueError("POLL_JITTER_SECONDS must be less than QUEUE_POLL_SECONDS")

        interval_pairs = (
            (
                "PRE_QUEUE_POLL_MIN_SECONDS",
                self.pre_queue_poll_min_seconds,
                "PRE_QUEUE_POLL_MAX_SECONDS",
                self.pre_queue_poll_max_seconds,
            ),
            (
                "ACTIVE_EARLY_POLL_MIN_SECONDS",
                self.active_early_poll_min_seconds,
                "ACTIVE_EARLY_POLL_MAX_SECONDS",
                self.active_early_poll_max_seconds,
            ),
            (
                "ACTIVE_MID_POLL_MIN_SECONDS",
                self.active_mid_poll_min_seconds,
                "ACTIVE_MID_POLL_MAX_SECONDS",
                self.active_mid_poll_max_seconds,
            ),
            (
                "SERVICED_SOON_POLL_MIN_SECONDS",
                self.serviced_soon_poll_min_seconds,
                "SERVICED_SOON_POLL_MAX_SECONDS",
                self.serviced_soon_poll_max_seconds,
            ),
        )
        for minimum_name, minimum, maximum_name, maximum in interval_pairs:
            if minimum > maximum:
                raise ValueError(f"{minimum_name} cannot exceed {maximum_name}")

        return self


@lru_cache
def get_settings(**overrides: Any) -> Settings:
    """Return validated settings.

    Keyword overrides are intended for tests and small scripts.
    """

    return Settings(**overrides)
