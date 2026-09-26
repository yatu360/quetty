"""Browser-state storage boundary."""

from pathlib import Path
from typing import Protocol

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type BrowserState = dict[str, JSONValue]


class StateStore(Protocol):
    """Async storage for sensitive browser state documents."""

    async def save(self, session_id: str, state: BrowserState) -> Path: ...

    async def load(self, session_id: str) -> BrowserState | None: ...

    async def delete(self, session_id: str) -> bool: ...


class StateStoreError(RuntimeError):
    """Raised when stored browser state is invalid or inaccessible."""
