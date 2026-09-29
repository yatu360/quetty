"""Typed runtime configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from queue_load_test.models.browser import BrowserBackendName
from queue_load_test.models.run import MonitoringStrategy
from queue_load_test.models.session import SessionMode


class Settings(BaseSettings):
    """Environment-backed application settings."""

    staging_url: HttpUrl | None = Field(default=None, alias="STAGING_URL")
    target_queue_ids: int = Field(default=1000, alias="TARGET_QUEUE_IDS", ge=1)
    session_mode: SessionMode = Field(default=SessionMode.HYBRID, alias="SESSION_MODE")
    # Default for NEW runs only (Phase 7 acceptance: Patchright; Chrome is the supported
    # fallback). An existing run always restarts with the backend persisted in its run
    # configuration; changing this never migrates it.
    browser_backend: BrowserBackendName = Field(
        default=BrowserBackendName.PATCHRIGHT,
        alias="BROWSER_BACKEND",
    )
    # Setup may override this for a new run. Persisted run provenance always wins
    # after creation, so changing the environment never migrates an existing run.
    monitoring_strategy: MonitoringStrategy = Field(
        default=MonitoringStrategy.HEADED_WINDOW,
        alias="MONITORING_STRATEGY",
    )
    # Sensitive browser-network discovery is opt-in and writes only to a dedicated,
    # git-ignored local evidence directory. It never changes the monitoring path.
    status_discovery_enabled: bool = Field(
        default=False,
        alias="STATUS_DISCOVERY_ENABLED",
    )
    status_discovery_directory: Path = Field(
        default=Path(".status-discovery"),
        alias="STATUS_DISCOVERY_DIRECTORY",
    )
    status_discovery_scope: str = Field(
        default="diagnostic_unverified_target",
        alias="STATUS_DISCOVERY_SCOPE",
    )
    status_discovery_confirm_authorized_staging: bool = Field(
        default=False,
        alias="STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING",
    )
    status_discovery_max_exchanges: int = Field(
        default=100,
        alias="STATUS_DISCOVERY_MAX_EXCHANGES",
        ge=1,
        le=1_000,
    )
    status_discovery_max_body_bytes: int = Field(
        default=65_536,
        alias="STATUS_DISCOVERY_MAX_BODY_BYTES",
        ge=1,
        le=1_048_576,
    )
    status_discovery_event_queue_capacity: int = Field(
        default=100,
        alias="STATUS_DISCOVERY_EVENT_QUEUE_CAPACITY",
        ge=1,
        le=1_000,
    )
    status_discovery_cleanup_timeout_seconds: float = Field(
        default=5.0,
        alias="STATUS_DISCOVERY_CLEANUP_TIMEOUT_SECONDS",
        gt=0,
    )
    status_discovery_observe_seconds: float = Field(
        default=30.0,
        alias="STATUS_DISCOVERY_OBSERVE_SECONDS",
        ge=0,
        le=300,
    )
    # Direct Monitoring Strategy (Phase 8 Prompt 5). Replayable request material and
    # response cookies live only beneath this protected, git-ignored directory. A
    # session becomes direct-capable only from its own browser-observed request
    # (status discovery above) validated by a reviewed response schema; without the
    # schema every Direct run check uses the browser fallback.
    direct_monitor_directory: Path = Field(
        default=Path(".direct-monitor"),
        alias="DIRECT_MONITOR_DIRECTORY",
    )
    direct_monitor_schema_path: Path | None = Field(
        default=None,
        alias="DIRECT_MONITOR_SCHEMA_PATH",
    )
    direct_monitor_timeout_seconds: float = Field(
        default=10.0,
        alias="DIRECT_MONITOR_TIMEOUT_SECONDS",
        gt=0,
        le=60,
    )
    direct_monitor_max_response_bytes: int = Field(
        default=65_536,
        alias="DIRECT_MONITOR_MAX_RESPONSE_BYTES",
        ge=1,
        le=1_048_576,
    )
    direct_monitor_failure_threshold: int = Field(
        default=3,
        alias="DIRECT_MONITOR_FAILURE_THRESHOLD",
        ge=1,
        le=100,
    )
    # After a session becomes DIRECT_UNAVAILABLE, wait this long before adopting a
    # newly observed recipe again; it bounds direct-then-fallback churn when a fault
    # affects only direct requests. 0 re-adopts on the next legitimate observation.
    direct_monitor_readopt_cooldown_seconds: float = Field(
        default=300.0,
        alias="DIRECT_MONITOR_READOPT_COOLDOWN_SECONDS",
        ge=0,
    )
    # Newest discovery artifacts kept per session in Direct runs (they can contain
    # visitor credentials). 0 keeps every artifact.
    direct_monitor_discovery_retention: int = Field(
        default=5,
        alias="DIRECT_MONITOR_DISCOVERY_RETENTION",
        ge=0,
        le=1_000,
    )
    chrome_process_count: int = Field(default=2, alias="CHROME_PROCESS_COUNT", ge=1, le=4)
    max_contexts_per_browser: int = Field(
        default=25, alias="MAX_CONTEXTS_PER_BROWSER", ge=1, le=25
    )
    max_active_contexts: int = Field(default=50, alias="MAX_ACTIVE_CONTEXTS", ge=1, le=100)
    creation_workers: int = Field(default=1, alias="CREATION_WORKERS", ge=1)
    creation_queue_capacity: int = Field(default=5, alias="CREATION_QUEUE_CAPACITY", ge=1)
    identity_replacement_limit: int = Field(default=0, alias="IDENTITY_REPLACEMENT_LIMIT", ge=0)
    monitor_workers: int = Field(default=1, alias="MONITOR_WORKERS", ge=1)
    monitor_queue_capacity: int = Field(default=5, alias="MONITOR_QUEUE_CAPACITY", ge=1)
    monitor_claim_batch_size: int = Field(default=5, alias="MONITOR_CLAIM_BATCH_SIZE", ge=1)
    monitor_lease_seconds: float = Field(default=120.0, alias="MONITOR_LEASE_SECONDS", gt=0)
    monitor_scheduler_tick_seconds: float = Field(
        default=1.0, alias="MONITOR_SCHEDULER_TICK_SECONDS", gt=0
    )
    monitor_retry_max_attempts: int = Field(default=3, alias="MONITOR_RETRY_MAX_ATTEMPTS", ge=1)
    monitor_retry_initial_backoff_seconds: float = Field(
        default=0.5, alias="MONITOR_RETRY_INITIAL_BACKOFF_SECONDS", ge=0
    )
    monitor_retry_max_backoff_seconds: float = Field(
        default=5.0, alias="MONITOR_RETRY_MAX_BACKOFF_SECONDS", ge=0
    )
    monitor_retry_jitter_seconds: float = Field(
        default=0.25, alias="MONITOR_RETRY_JITTER_SECONDS", ge=0
    )
    admission_wait_seconds: float = Field(default=5.0, alias="ADMISSION_WAIT_SECONDS", ge=0)
    shutdown_timeout_seconds: float = Field(default=30.0, alias="SHUTDOWN_TIMEOUT_SECONDS", gt=0)
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
    # Queue ID acquisition runs in visible Chrome by default; once a Queue ID is
    # persisted the session is monitored by the HEADLESS automatic pool.
    creation_headless: bool = Field(default=False, alias="CREATION_HEADLESS")
    database_url: str = Field(default="sqlite:///queue_load_test.sqlite3", alias="DATABASE_URL")
    state_directory: Path = Field(default=Path(".browser-state"), alias="STATE_DIRECTORY")
    prometheus_port: int = Field(default=9090, alias="PROMETHEUS_PORT", ge=1, le=65535)
    ui_host: str = Field(default="127.0.0.1", alias="UI_HOST")
    ui_port: int = Field(default=8000, alias="UI_PORT", ge=1, le=65535)
    max_manual_requested_sessions: int = Field(
        default=10_000,
        alias="MAX_MANUAL_REQUESTED_SESSIONS",
        ge=1,
    )
    max_manual_open_sessions: int = Field(
        default=5,
        alias="MAX_MANUAL_OPEN_SESSIONS",
        ge=1,
    )
    manual_open_lease_seconds: float = Field(
        default=30.0,
        alias="MANUAL_OPEN_LEASE_SECONDS",
        gt=3,
    )
    operator_workers: int = Field(default=2, alias="OPERATOR_WORKERS", ge=1)
    operator_queue_capacity: int = Field(
        default=10, alias="OPERATOR_QUEUE_CAPACITY", ge=1
    )
    operator_lease_seconds: float = Field(
        default=300.0, alias="OPERATOR_LEASE_SECONDS", gt=0
    )

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

    def require_staging_url(self) -> str:
        """Return the environment target for explicitly gated legacy harnesses."""

        if self.staging_url is None:
            raise ValueError("STAGING_URL is required for this staging harness")
        return str(self.staging_url)

    @field_validator("session_mode", mode="before")
    @classmethod
    def parse_session_mode(cls, value: object) -> SessionMode:
        if not isinstance(value, str | SessionMode):
            # Pydantic reports ValueError as configuration validation failure.
            raise ValueError("SESSION_MODE must be a string")  # noqa: TRY004
        return SessionMode.parse(value)

    @field_validator("browser_backend", mode="before")
    @classmethod
    def parse_browser_backend(cls, value: object) -> BrowserBackendName:
        if not isinstance(value, str | BrowserBackendName):
            raise ValueError("BROWSER_BACKEND must be a string")  # noqa: TRY004
        return BrowserBackendName.parse(value)

    @field_validator("direct_monitor_schema_path", mode="before")
    @classmethod
    def blank_schema_path_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("monitoring_strategy", mode="before")
    @classmethod
    def parse_monitoring_strategy(cls, value: object) -> MonitoringStrategy:
        if not isinstance(value, str | MonitoringStrategy):
            raise ValueError("MONITORING_STRATEGY must be a string")  # noqa: TRY004
        return MonitoringStrategy.parse(value)

    @model_validator(mode="after")
    def validate_capacity(self) -> "Settings":
        total_capacity = self.chrome_process_count * self.max_contexts_per_browser
        if self.max_active_contexts > total_capacity:
            raise ValueError(
                "MAX_ACTIVE_CONTEXTS cannot exceed CHROME_PROCESS_COUNT * MAX_CONTEXTS_PER_BROWSER"
            )

        if self.creation_workers > self.max_active_contexts:
            raise ValueError("CREATION_WORKERS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.creation_queue_capacity > self.max_active_contexts:
            raise ValueError("CREATION_QUEUE_CAPACITY cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.monitor_workers > self.max_active_contexts:
            raise ValueError("MONITOR_WORKERS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.creation_workers + self.monitor_workers > self.max_active_contexts:
            raise ValueError("CREATION_WORKERS + MONITOR_WORKERS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.monitor_claim_batch_size > self.monitor_queue_capacity:
            raise ValueError("MONITOR_CLAIM_BATCH_SIZE cannot exceed MONITOR_QUEUE_CAPACITY")

        if self.monitor_queue_capacity > self.max_active_contexts:
            raise ValueError("MONITOR_QUEUE_CAPACITY cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.max_manual_open_sessions > self.max_active_contexts:
            raise ValueError("MAX_MANUAL_OPEN_SESSIONS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.operator_workers > self.max_active_contexts:
            raise ValueError("OPERATOR_WORKERS cannot exceed MAX_ACTIVE_CONTEXTS")

        if self.poll_jitter_seconds >= self.queue_poll_seconds:
            raise ValueError("POLL_JITTER_SECONDS must be less than QUEUE_POLL_SECONDS")

        if self.monitor_retry_max_backoff_seconds < self.monitor_retry_initial_backoff_seconds:
            raise ValueError(
                "MONITOR_RETRY_MAX_BACKOFF_SECONDS cannot be less than "
                "MONITOR_RETRY_INITIAL_BACKOFF_SECONDS"
            )

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

        if self.direct_monitor_timeout_seconds >= self.monitor_lease_seconds:
            raise ValueError(
                "DIRECT_MONITOR_TIMEOUT_SECONDS must be less than MONITOR_LEASE_SECONDS"
            )

        allowed_discovery_scopes = {
            "diagnostic_unverified_target",
            "local_simulator",
            "authorized_queue_it_staging",
        }
        if self.status_discovery_scope not in allowed_discovery_scopes:
            raise ValueError(
                "STATUS_DISCOVERY_SCOPE must be one of: "
                + ", ".join(sorted(allowed_discovery_scopes))
            )
        if (
            self.status_discovery_enabled
            and self.status_discovery_scope == "authorized_queue_it_staging"
            and not self.status_discovery_confirm_authorized_staging
        ):
            raise ValueError(
                "STATUS_DISCOVERY_CONFIRM_AUTHORIZED_STAGING=true is required for "
                "authorized_queue_it_staging discovery"
            )

        return self


@lru_cache
def get_settings(**overrides: Any) -> Settings:
    """Return validated settings.

    Keyword overrides are intended for tests and small scripts.
    """

    return Settings(**overrides)
