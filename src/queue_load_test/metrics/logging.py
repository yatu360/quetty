"""Small JSON logging helpers with a fixed observability context schema."""

import json
import logging
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
)


class JsonLogFormatter(logging.Formatter):
    """Serialize log records without accidentally including transfer URLs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "observability_context", {})
        if isinstance(context, dict):
            for field_name in _CONTEXT_FIELDS:
                value = context.get(field_name)
                if value is not None:
                    payload[field_name] = value
        if record.exc_info is not None:
            error_class = record.exc_info[0]
            if error_class is not None:
                payload.setdefault("error_type", error_class.__name__)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_structured_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


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
