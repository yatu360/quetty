"""Read-only consistency checks for persisted HYBRID browser state."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from queue_load_test.models import BrowserBackendName, QueueSession, QueueStatus, SessionMode
from queue_load_test.repository.base import RecoverySummary, SessionRepository
from queue_load_test.state.base import (
    StateCorruptError,
    StateSessionMismatchError,
    StateUnreadableError,
)
from queue_load_test.state.filesystem import FileSystemStateStore, read_state_document

_UNUSABLE_STATE_KINDS = frozenset(
    {"corrupt_state_file", "unreadable_state_file", "mismatched_state_file"}
)


@dataclass(frozen=True, slots=True)
class StateConsistencyFinding:
    """One actionable database/filesystem inconsistency."""

    kind: str
    state_path: str
    session_ids: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class StateConsistencyReport:
    """Aggregate result of a non-mutating state consistency scan."""

    database_sessions: int
    hybrid_sessions_requiring_state: int
    referenced_state_paths: int
    state_files: int
    temporary_files: int
    findings: tuple[StateConsistencyFinding, ...]
    legacy_state_files: int = 0

    @property
    def is_consistent(self) -> bool:
        return not self.findings

    def count(self, kind: str) -> int:
        return sum(finding.kind == kind for finding in self.findings)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class StateConsistencyChecker:
    """Compare repository state references with one local state directory.

    The scan is intentionally report-only. It never removes or repairs files, and all
    filesystem traversal and JSON parsing run in a worker thread. Findings carry paths
    and session IDs only, never state contents, Queue IDs, or transfer URLs.
    """

    def __init__(
        self,
        repository: SessionRepository,
        state_store: FileSystemStateStore,
    ) -> None:
        self._repository = repository
        self._state_store = state_store

    async def check(self) -> StateConsistencyReport:
        sessions, active_run = await asyncio.gather(
            self._repository.list(),
            self._repository.get_active_run(),
        )
        expected_backend = active_run.browser_backend if active_run is not None else None
        return await asyncio.to_thread(self._check_filesystem, sessions, expected_backend)

    async def recovery_summary(self, *, now: datetime) -> RecoverySummary:
        """Add explicit filesystem findings to the aggregate database summary."""

        database_summary, state_report = await asyncio.gather(
            self._repository.recovery_summary(now=now),
            self.check(),
        )
        return replace(
            database_summary,
            missing_state_files=state_report.count("missing_state_file"),
            corrupt_state_files=sum(
                finding.kind in _UNUSABLE_STATE_KINDS for finding in state_report.findings
            ),
        )

    def _check_filesystem(
        self,
        sessions: list[QueueSession],
        expected_backend: BrowserBackendName | None,
    ) -> StateConsistencyReport:
        directory = self._state_store.directory
        normalize = _PathNormalizer()
        paths_to_sessions: dict[Path, list[str]] = {}
        display_paths: dict[Path, Path] = {}
        required_paths: set[Path] = set()
        required_session_count = 0
        findings: list[StateConsistencyFinding] = []

        for session in sessions:
            normalized = normalize(session.state_path)
            paths_to_sessions.setdefault(normalized, []).append(session.session_id)
            display_paths.setdefault(normalized, session.state_path)
            if _requires_state(session):
                required_paths.add(normalized)
                required_session_count += 1

            if expected_backend is not None and session.browser_backend is not expected_backend:
                findings.append(
                    StateConsistencyFinding(
                        kind="backend_provenance_mismatch",
                        state_path=str(session.state_path),
                        session_ids=(session.session_id,),
                        detail=(
                            f"run backend is {expected_backend.value}; "
                            f"session backend is {session.browser_backend.value}"
                        ),
                    )
                )

            expected_path = self._state_store.path_for(session.session_id)
            if session.mode is SessionMode.HYBRID and normalized != normalize(expected_path):
                findings.append(
                    StateConsistencyFinding(
                        kind="conflicting_state_path",
                        state_path=str(session.state_path),
                        session_ids=(session.session_id,),
                        detail=f"expected {expected_path}",
                    )
                )

        for normalized, session_ids in paths_to_sessions.items():
            if len(session_ids) > 1:
                findings.append(
                    StateConsistencyFinding(
                        kind="duplicate_state_path",
                        state_path=str(display_paths[normalized]),
                        session_ids=tuple(sorted(session_ids)),
                    )
                )

        state_files, temporary_files = _directory_files(directory)
        state_files_by_normalized = {normalize(path): path for path in state_files}

        def exists(normalized: Path) -> bool:
            return normalized in state_files_by_normalized or normalized.is_file()

        for normalized in sorted(required_paths, key=str):
            if not exists(normalized):
                findings.append(
                    StateConsistencyFinding(
                        kind="missing_state_file",
                        state_path=str(display_paths[normalized]),
                        session_ids=tuple(sorted(paths_to_sessions[normalized])),
                    )
                )

        for normalized, path in sorted(
            state_files_by_normalized.items(), key=lambda item: str(item[0])
        ):
            if normalized not in paths_to_sessions:
                findings.append(
                    StateConsistencyFinding(
                        kind="orphaned_state_file",
                        state_path=str(path),
                    )
                )

        candidate_files = dict(state_files_by_normalized)
        for normalized in paths_to_sessions:
            if normalized not in candidate_files and normalized.is_file():
                candidate_files[normalized] = display_paths[normalized]
        legacy_state_files = 0
        for normalized, path in sorted(candidate_files.items(), key=lambda item: str(item[0])):
            referencing = tuple(sorted(paths_to_sessions.get(normalized, ())))
            expected_session_id = referencing[0] if len(referencing) == 1 else path.stem
            kind, detail, legacy = _inspect_state_file(path, expected_session_id)
            legacy_state_files += legacy
            if kind is not None:
                findings.append(
                    StateConsistencyFinding(
                        kind=kind,
                        state_path=str(path),
                        session_ids=referencing,
                        detail=detail,
                    )
                )

        for path in temporary_files:
            findings.append(
                StateConsistencyFinding(
                    kind="stale_temporary_file",
                    state_path=str(path),
                    detail="report only; remove explicitly after investigating interrupted writes",
                )
            )

        findings.sort(key=lambda finding: (finding.kind, finding.state_path, finding.session_ids))
        return StateConsistencyReport(
            database_sessions=len(sessions),
            hybrid_sessions_requiring_state=required_session_count,
            referenced_state_paths=len(paths_to_sessions),
            state_files=len(state_files),
            temporary_files=len(temporary_files),
            findings=tuple(findings),
            legacy_state_files=legacy_state_files,
        )


def _requires_state(session: QueueSession) -> bool:
    return (
        session.mode is SessionMode.HYBRID
        and session.queue_id is not None
        and session.status is not QueueStatus.FAILED
    )


class _PathNormalizer:
    """Resolve paths to absolute form, resolving each parent directory only once.

    Resolving every path individually cost about half of a 10,000-file scan in
    ``lstat`` calls. The final component is not followed, so a symlinked state file is
    compared by its own location rather than its target.
    """

    def __init__(self) -> None:
        self._parents: dict[Path, Path] = {}

    def __call__(self, path: Path) -> Path:
        path = path.expanduser()
        parent = self._parents.get(path.parent)
        if parent is None:
            parent = path.parent.resolve(strict=False)
            self._parents[path.parent] = parent
        return parent / path.name


def _directory_files(directory: Path) -> tuple[list[Path], list[Path]]:
    try:
        with os.scandir(directory) as entries:
            files = [Path(entry.path) for entry in entries if entry.is_file()]
    except FileNotFoundError:
        return [], []
    state_files = sorted((path for path in files if path.suffix == ".json"), key=str)
    temporary_files = sorted((path for path in files if path.name.endswith(".tmp")), key=str)
    return state_files, temporary_files


def _inspect_state_file(path: Path, session_id: str) -> tuple[str | None, str | None, bool]:
    """Return ``(finding kind, detail, legacy)`` for one state file."""

    try:
        document = read_state_document(path, session_id)
    except FileNotFoundError:
        return "missing_state_file", "removed during scan", False
    except StateUnreadableError as exc:
        return "unreadable_state_file", _cause_name(exc), False
    except StateSessionMismatchError:
        return "mismatched_state_file", "document belongs to another session", False
    except StateCorruptError as exc:
        return "corrupt_state_file", str(exc), False
    if os.name == "nt":
        return None, None, document.legacy
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        return "unreadable_state_file", type(exc).__name__, document.legacy
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            "insecure_state_permissions",
            f"mode {stat.S_IMODE(mode):04o}; expected 0600",
            document.legacy,
        )
    return None, None, document.legacy


def _cause_name(exc: BaseException) -> str:
    return type(exc.__cause__).__name__ if exc.__cause__ is not None else type(exc).__name__
