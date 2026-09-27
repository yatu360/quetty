"""Browser backend domain values."""

from __future__ import annotations

from enum import StrEnum


class BrowserBackendName(StrEnum):
    """Supported managed-browser implementations."""

    CHROME = "chrome"
    CAMOUFOX = "camoufox"

    @classmethod
    def parse(cls, value: str | BrowserBackendName) -> BrowserBackendName:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            supported = ", ".join(backend.value for backend in cls)
            raise ValueError(
                f"Unknown browser backend {value!r}; expected one of: {supported}"
            ) from exc
