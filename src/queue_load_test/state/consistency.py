"""Read-only consistency checks for persisted HYBRID browser state."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from queue_load_test.models import QueueSession, QueueStatus, SessionMode
from queue_load_test.repository.base import RecoverySummary, SessionRepository
from queue_load_test.state.filesystem import FileSystemStateStore


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
    filesystem traversal and JSON parsing run in a worker thread.
    """

    def __init__(
        self,
        repository: SessionRepository,
        state_store: FileSystemStateStore,
    ) -> None:
        self._repository = repository
        self._state_store = state_store

    async def check(self) -> StateConsistencyReport:
        sessions = await self._repository.list()
        return await asyncio.to_thread(self._check_filesystem, sessions)

    async def recovery_summary(self, *, now: datetime) -> RecoverySummary:
        """Add explicit filesystem findings to the aggregate database summary."""

        database_summary, state_report = await asyncio.gather(
            self._repository.recovery_summary(now=now),
            self.check(),
        )
        return replace(
            database_summary,
            missing_state_files=state_report.count("missing_state_file"),
            corrupt_state_files=state_report.count("corrupt_state_file"),
        )

    def _check_filesystem(
        self,
        sessions: list[QueueSession],
    ) -> StateConsistencyReport:
        directory = self._state_store.directory
        paths_to_sessions: dict[Path, list[str]] = {}
        display_paths: dict[Path, Path] = {}
        required_paths: set[Path] = set()
        required_session_count = 0
        findings: list[StateConsistencyFinding] = []

        for session in sessions:
            normalized = _normalized(session.state_path)
            paths_to_sessions.setdefault(normalized, []).append(session.session_id)
            display_paths.setdefault(normalized, session.state_path)
            if _requires_state(session):
                required_paths.add(normalized)
                required_session_count += 1

            expected = _normalized(self._state_store.path_for(session.session_id))
            if session.mode is SessionMode.HYBRID and normalized != expected:
                findings.append(
                    StateConsistencyFinding(
                        kind="conflicting_state_path",
                        state_path=str(session.state_path),
                        session_ids=(session.session_id,),
                        detail=f"expected {self._state_store.path_for(session.session_id)}",
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
        state_files_by_normalized = {_normalized(path): path for path in state_files}

        for normalized in sorted(required_paths, key=str):
            if not normalized.is_file():
                findings.append(
                    StateConsistencyFinding(
                        kind="missing_state_file",
                        state_path=str(display_paths[normalized]),
                        session_ids=tuple(sorted(paths_to_sessions[normalized])),
                    )
                )

        for normalized, path in sorted(state_files_by_normalized.items(), key=lambda item: str(item[0])):
            if normalized not in paths_to_sessions:
                findings.append(
                    StateConsistencyFinding(
                        kind="orphaned_state_file",
                        state_path=str(path),
                    )
                )

        candidate_files = dict(state_files_by_normalized)
        for normalized in paths_to_sessions:
            if normalized.is_file():
                candidate_files.setdefault(normalized, display_paths[normalized])
        for normalized, path in sorted(candidate_files.items(), key=lambda item: str(item[0])):
            error = _json_error(path)
            if error is not None:
                findings.append(
                    StateConsistencyFinding(
                        kind="corrupt_state_file",
                        state_path=str(path),
                        session_ids=tuple(sorted(paths_to_sessions.get(normalized, ()))),
                        detail=error,
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
        )


def _requires_state(session: QueueSession) -> bool:
    return (
        session.mode is SessionMode.HYBRID
        and session.queue_id is not None
        and session.status is not QueueStatus.FAILED
    )


def _normalized(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _directory_files(directory: Path) -> tuple[list[Path], list[Path]]:
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return [], []
    state_files = sorted(
        (path for path in entries if path.is_file() and path.suffix == ".json"),
        key=str,
    )
    temporary_files = sorted(
        (path for path in entries if path.is_file() and path.name.endswith(".tmp")),
        key=str,
    )
    return state_files, temporary_files


def _json_error(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return type(exc).__name__
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return "browser state is not a JSON object with string keys"
    return None
