"""Report-only consistency audit of the protected direct-monitor store.

The audit reads protected records and non-secret SQLite metadata, then reports
findings by session ID and a closed finding kind. It never repairs, rewrites, or
deletes anything, and never includes recipe, URL, header, body, or cookie values.
"""

from __future__ import annotations

import asyncio
import stat
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from queue_load_test.direct_monitor.store import (
    DirectMonitorStateError,
    DirectMonitorStateStore,
)
from queue_load_test.models import QueueSession
from queue_load_test.repository import DirectMonitorStatus


class DirectMonitorFindingKind(StrEnum):
    CORRUPT_RECORD = "corrupt_record"
    ORPHAN_RECORD = "orphan_record"
    IDENTITY_MISMATCH = "identity_mismatch"
    ORPHAN_COOKIES = "orphan_cookies"
    METADATA_MISMATCH = "metadata_mismatch"
    UNSAFE_PERMISSIONS = "unsafe_permissions"


@dataclass(frozen=True, slots=True)
class DirectMonitorFinding:
    kind: DirectMonitorFindingKind
    session_id: str | None


@dataclass(frozen=True, slots=True)
class DirectMonitorConsistencyReport:
    records_checked: int
    findings: tuple[DirectMonitorFinding, ...] = field(default_factory=tuple)

    @property
    def consistent(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, object]:
        counts = Counter(finding.kind.value for finding in self.findings)
        return {
            "records_checked": self.records_checked,
            "consistent": self.consistent,
            "finding_counts": dict(sorted(counts.items())),
            "findings": [
                {"kind": finding.kind.value, "session_id": finding.session_id}
                for finding in self.findings
            ],
            "repairs_performed": 0,
        }


class _Repository(Protocol):
    async def get(self, session_id: str) -> QueueSession | None: ...

    async def get_direct_monitor_status(self, session_id: str) -> DirectMonitorStatus | None: ...


class DirectMonitorConsistencyChecker:
    def __init__(self, repository: _Repository, store: DirectMonitorStateStore) -> None:
        self._repository = repository
        self._store = store

    async def check(self) -> DirectMonitorConsistencyReport:
        findings: list[DirectMonitorFinding] = []
        records, cookies, unsafe = await asyncio.to_thread(_inventory, self._store.directory)
        findings.extend(
            DirectMonitorFinding(DirectMonitorFindingKind.UNSAFE_PERMISSIONS, session_id)
            for session_id in unsafe
        )
        for session_id in records:
            session = await self._repository.get(session_id)
            if session is None:
                findings.append(
                    DirectMonitorFinding(DirectMonitorFindingKind.ORPHAN_RECORD, session_id)
                )
                continue
            try:
                record = await self._store.load(session_id)
            except DirectMonitorStateError:
                findings.append(
                    DirectMonitorFinding(DirectMonitorFindingKind.CORRUPT_RECORD, session_id)
                )
                continue
            if record is None:
                continue
            if record.expected_queue_id != session.queue_id:
                findings.append(
                    DirectMonitorFinding(DirectMonitorFindingKind.IDENTITY_MISMATCH, session_id)
                )
            metadata = await self._repository.get_direct_monitor_status(session_id)
            if metadata is not None and metadata.capability != record.capability.value:
                findings.append(
                    DirectMonitorFinding(DirectMonitorFindingKind.METADATA_MISMATCH, session_id)
                )
        for session_id in sorted(cookies - set(records)):
            if await self._repository.get(session_id) is None:
                findings.append(
                    DirectMonitorFinding(DirectMonitorFindingKind.ORPHAN_COOKIES, session_id)
                )
        return DirectMonitorConsistencyReport(len(records), tuple(findings))


def _inventory(directory: Path) -> tuple[list[str], set[str], list[str | None]]:
    """Session names of record/cookie files, plus paths with unsafe permissions."""

    unsafe: list[str | None] = []
    records: list[str] = []
    cookies: set[str] = set()
    for folder, names in ((directory / "records", records), (directory / "cookies", None)):
        if not folder.is_dir():
            continue
        if stat.S_IMODE(folder.stat().st_mode) & 0o077:
            unsafe.append(None)
        for path in sorted(folder.glob("*.json")):
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                unsafe.append(path.stem)
            if names is not None:
                names.append(path.stem)
            else:
                cookies.add(path.stem)
    return records, cookies, unsafe
