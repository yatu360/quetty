"""Small JSON logging helpers with a fixed observability context schema."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

_CONTEXT_FIELDS = (
    "session_id",
    "queue_id",
    "status",
    "worker_id",
    "browser_id",
    "attempt",
    "restore_method",
    "duration",
    "error_type",
    "classification",
    "retryable",
    "backoff_seconds",
    "operation",
    "count",
    "recovered_leases",
    "lost_contexts",
    "total_sessions",
    "valid_queue_ids",
    "lost_queue_ids",
    "leased_sessions",
    "expired_leases",
    "sessions_due",
    "sessions_requiring_retry",
    "terminal_sessions",
    "missing_state_files",
    "corrupt_state_files",
    "browser_backend",
    "browser_version",
    "package_version",
    "recorded_build",
    "installed_build",
)

REDACTED_URL = "<redacted-url>"
REDACTED_TRANSPORT_DETAIL = "<redacted-http-client-detail>"
# HTTP client libraries format request lines, headers (Cookie, Set-Cookie,
# Authorization), and connection traces into their messages. Direct Monitoring
# replays visitor credentials through them, so their free text never reaches the
# structured log; the logger name, level, and exception type remain.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "hpack", "h11", "h2")


def is_http_client_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in HTTP_CLIENT_LOGGERS)


def quiet_http_client_loggers() -> None:
    """Floor HTTP client loggers at WARNING; child loggers inherit the level."""

    for name in HTTP_CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)
# Transfer URLs carry the Queue-it identity token, so any absolute URL is removed
# from free text. This also covers third-party messages (for example a Playwright
# navigation error) that reach the root logger.
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?|file)://[^\s\"'<>]+")


def redact_urls(value: str) -> str:
    """Replace every absolute URL in ``value`` with a fixed placeholder."""

    return _URL_PATTERN.sub(REDACTED_URL, value)


def _safe_value(value: object) -> object:
    if isinstance(value, str):
        return redact_urls(value)
    if isinstance(value, bool | int | float):
        return value
    return redact_urls(str(value))


class JsonLogFormatter(logging.Formatter):
    """Serialize log records without accidentally including transfer URLs."""

    def __init__(self, *, run_id: str | None = None) -> None:
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": (
                REDACTED_TRANSPORT_DETAIL
                if is_http_client_logger(record.name)
                else redact_urls(record.getMessage())
            ),
        }
        if self._run_id is not None:
            payload["run_id"] = self._run_id
        context = getattr(record, "observability_context", {})
        if isinstance(context, dict):
            for field_name in _CONTEXT_FIELDS:
                value = context.get(field_name)
                if value is not None:
                    payload[field_name] = _safe_value(value)
        if record.exc_info is not None:
            error_class = record.exc_info[0]
            if error_class is not None:
                payload.setdefault("error_type", error_class.__name__)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_structured_logging(
    level: int = logging.INFO,
    *,
    run_id: str | None = None,
) -> None:
    """Install JSON logging; ``run_id`` correlates every line of one process run."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter(run_id=run_id))
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
    quiet_http_client_loggers()


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    **context: object,
) -> None:
    """Log approved context keys; transfer URLs are ignored even if supplied."""

    safe_context = {
        key: value for key, value in context.items() if key in _CONTEXT_FIELDS and value is not None
    }
    logger.log(level, message, extra={"observability_context": safe_context})
