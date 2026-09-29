"""Protected per-session direct-monitor records kept outside SQLite.

A record holds the exact browser-observed status request (URL, headers, body) that
direct monitoring replays. Those values can carry visitor credentials, so they are
stored only here: one mode-0600 document per session beneath a mode-0700,
ignore-all directory, integrity-checked and bound to the session and its Queue ID.
Response-refreshed cookies live beside it in the Prompt 3 protected cookie store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from queue_load_test.direct_monitor.models import DirectCapability, DirectFallbackReason
from queue_load_test.direct_replay import (
    ProtectedReplayStateStore,
    ReplayRecipe,
    ReplayStateError,
)
from queue_load_test.direct_replay.store import read_protected_json, write_protected_json

_FORMAT = "queue-load-test.phase8-direct-monitor-record"
_VERSION = 1


class DirectMonitorStateError(RuntimeError):
    """A protected record is corrupt, mismatched, or unreadable."""


@dataclass(frozen=True, slots=True)
class DirectMonitorRecord:
    """Minimum direct-monitor metadata for one persisted session."""

    session_id: str
    expected_queue_id: str = field(repr=False)
    capability: DirectCapability
    updated_at: datetime
    recipe: ReplayRecipe | None = field(default=None, repr=False)
    last_reason: DirectFallbackReason | None = None
    consecutive_failures: int = 0

    def __post_init__(self) -> None:
        if self.capability is DirectCapability.DIRECT_CAPABLE and self.recipe is None:
            raise ValueError("a DIRECT_CAPABLE record needs a browser-observed recipe")
        if self.recipe is not None and (
            self.recipe.session_id != self.session_id
            or self.recipe.expected_queue_id != self.expected_queue_id
        ):
            raise ValueError("a direct recipe belongs to another session or identity")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures cannot be negative")


class DirectMonitorStateStore:
    """Atomic session-bound record store plus the response-cookie store."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.cookies = ProtectedReplayStateStore(directory / "cookies")
        self._records = directory / "records"

    def path_for(self, session_id: str) -> Path:
        try:
            cookie_path = self.cookies.path_for(session_id)
        except ReplayStateError as exc:
            raise DirectMonitorStateError(str(exc)) from exc
        return self._records / cookie_path.name

    async def load(self, session_id: str) -> DirectMonitorRecord | None:
        path = self.path_for(session_id)
        try:
            document = await asyncio.to_thread(read_protected_json, path)
        except FileNotFoundError:
            return None
        except ReplayStateError as exc:
            raise DirectMonitorStateError("direct-monitor record is unreadable") from exc
        return _decode(document, session_id)

    async def save(self, record: DirectMonitorRecord) -> Path:
        path = self.path_for(record.session_id)
        payload = _encode(record)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        document = {**payload, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}
        await asyncio.to_thread(write_protected_json, path, document)
        # The ignore-all marker belongs at the top of the protected tree as well.
        await asyncio.to_thread(_ensure_ignore_marker, self.directory)
        return path

    async def adopt_recipe(
        self,
        *,
        session_id: str,
        expected_queue_id: str,
        recipe: ReplayRecipe,
        now: datetime | None = None,
    ) -> DirectMonitorRecord:
        """Become capable from a newly observed legitimate browser request.

        Response cookies retained for a previous recipe are discarded so the next
        direct check starts from the browser's own freshly saved state.
        """

        record = DirectMonitorRecord(
            session_id=session_id,
            expected_queue_id=expected_queue_id,
            capability=DirectCapability.DIRECT_CAPABLE,
            updated_at=now or datetime.now(UTC),
            recipe=recipe,
        )
        await self.cookies.delete(session_id)
        await self.save(record)
        return record

    async def record_success(self, record: DirectMonitorRecord) -> DirectMonitorRecord:
        if record.consecutive_failures == 0 and record.last_reason is None:
            return record
        updated = replace(
            record,
            consecutive_failures=0,
            last_reason=None,
            updated_at=datetime.now(UTC),
        )
        await self.save(updated)
        return updated

    async def record_failure(
        self,
        record: DirectMonitorRecord,
        *,
        reason: DirectFallbackReason,
        make_unavailable: bool,
    ) -> DirectMonitorRecord:
        updated = replace(
            record,
            capability=(
                DirectCapability.DIRECT_UNAVAILABLE if make_unavailable else record.capability
            ),
            last_reason=reason,
            consecutive_failures=record.consecutive_failures + 1,
            updated_at=datetime.now(UTC),
        )
        await self.save(updated)
        return updated

    async def mark_unavailable(
        self,
        *,
        session_id: str,
        expected_queue_id: str,
        reason: DirectFallbackReason,
    ) -> DirectMonitorRecord:
        """Fence an unusable record (for example one for another identity)."""

        record = DirectMonitorRecord(
            session_id=session_id,
            expected_queue_id=expected_queue_id,
            capability=DirectCapability.DIRECT_UNAVAILABLE,
            updated_at=datetime.now(UTC),
            last_reason=reason,
            consecutive_failures=1,
        )
        await self.cookies.delete(session_id)
        await self.save(record)
        return record

    async def delete(self, session_id: str) -> bool:
        record_removed = await asyncio.to_thread(_unlink, self.path_for(session_id))
        cookies_removed = await self.cookies.delete(session_id)
        return record_removed or cookies_removed

    async def clear(self) -> int:
        return await asyncio.to_thread(_clear, self.directory)

    def cookie_path(self, session_id: str) -> Path:
        return self.cookies.path_for(session_id)


def _encode(record: DirectMonitorRecord) -> dict[str, object]:
    recipe = record.recipe
    return {
        "format": _FORMAT,
        "version": _VERSION,
        "session_id": record.session_id,
        "expected_queue_id": record.expected_queue_id,
        "capability": record.capability.value,
        "updated_at": record.updated_at.isoformat(),
        "last_reason": record.last_reason.value if record.last_reason is not None else None,
        "consecutive_failures": record.consecutive_failures,
        "recipe": (
            None
            if recipe is None
            else {
                "source_scope": recipe.source_scope,
                "exchange_sequence": recipe.exchange_sequence,
                "url": recipe.url,
                "method": recipe.method,
                "headers": dict(recipe.headers),
                "body": recipe.body.decode("utf-8") if recipe.body is not None else None,
                "observed_identifiers": {
                    key: list(values) for key, values in recipe.observed_identifiers.items()
                },
                "fingerprint": recipe.fingerprint,
            }
        ),
    }


def _decode(document: dict[str, object], session_id: str) -> DirectMonitorRecord:
    digest = document.pop("sha256", None)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    if digest != hashlib.sha256(canonical.encode()).hexdigest():
        raise DirectMonitorStateError("direct-monitor record failed its integrity check")
    if (
        document.get("format") != _FORMAT
        or document.get("version") != _VERSION
        or document.get("session_id") != session_id
    ):
        raise DirectMonitorStateError("direct-monitor record does not belong to this session")
    try:
        queue_id = document["expected_queue_id"]
        if not isinstance(queue_id, str) or not queue_id:
            raise TypeError
        raw_reason = document.get("last_reason")
        failures = document.get("consecutive_failures")
        if not isinstance(failures, int) or isinstance(failures, bool):
            raise TypeError
        return DirectMonitorRecord(
            session_id=session_id,
            expected_queue_id=queue_id,
            capability=DirectCapability(str(document["capability"])),
            updated_at=datetime.fromisoformat(str(document["updated_at"])),
            recipe=_decode_recipe(document.get("recipe"), session_id, queue_id),
            last_reason=(
                DirectFallbackReason(str(raw_reason)) if raw_reason is not None else None
            ),
            consecutive_failures=failures,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DirectMonitorStateError("direct-monitor record contains invalid values") from exc


def _decode_recipe(value: object, session_id: str, queue_id: str) -> ReplayRecipe | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError
    headers = value["headers"]
    identifiers = value["observed_identifiers"]
    body = value["body"]
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in headers.items()
    ):
        raise TypeError
    if not isinstance(identifiers, dict) or not all(
        isinstance(key, str)
        and isinstance(items, list)
        and all(isinstance(item, str) for item in items)
        for key, items in identifiers.items()
    ):
        raise TypeError
    if body is not None and not isinstance(body, str):
        raise TypeError
    fields = (value["source_scope"], value["url"], value["method"], value["fingerprint"])
    if not all(isinstance(item, str) and item for item in fields):
        raise TypeError
    sequence = value["exchange_sequence"]
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise TypeError
    return ReplayRecipe(
        session_id=session_id,
        expected_queue_id=queue_id,
        source_scope=cast(str, value["source_scope"]),
        exchange_sequence=sequence,
        url=cast(str, value["url"]),
        method=cast(str, value["method"]),
        headers=cast(dict[str, str], headers),
        body=body.encode("utf-8") if body is not None else None,
        observed_identifiers={
            key: tuple(items) for key, items in cast(dict[str, list[str]], identifiers).items()
        },
        fingerprint=cast(str, value["fingerprint"]),
    )


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _ensure_ignore_marker(directory: Path) -> None:
    marker = directory / ".gitignore"
    if marker.exists():
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write("*\n")
    except FileExistsError:
        return
    marker.chmod(0o600)


def _clear(directory: Path) -> int:
    removed = 0
    for child in ("records", "cookies"):
        folder = directory / child
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and path.name != ".gitignore" and _unlink(path):
                removed += 1
    return removed
