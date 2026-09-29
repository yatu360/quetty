"""Gated Phase 8 direct-then-browser observation equivalence harness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from queue_load_test.browser import BrowserManager
from queue_load_test.config import Settings
from queue_load_test.direct_replay import (
    DirectStatusReplayClient,
    HeaderProfile,
    ProtectedReplayStateStore,
    cookies_from_browser_state,
    load_replay_recipe,
)
from queue_load_test.models import MonitoringObservation, MonitoringStrategy, QueueSession
from queue_load_test.observation_equivalence import (
    DirectObservationError,
    DirectResponseParser,
    ShadowEquivalenceRunner,
    load_direct_response_schema,
)
from queue_load_test.queue_monitor import AdmissionDetector
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.scheduler import browser_observation_from_restore_result
from queue_load_test.scheduler.monitoring import is_verified_observation
from queue_load_test.state import FileSystemStateStore
from queue_load_test.transfer import QueueSessionRestorer

_EXPERIMENT_GATE = "RUN_PHASE8_EQUIVALENCE"


@dataclass(frozen=True, slots=True)
class ShadowCase:
    session_id: str
    artifact: Path
    exchange_sequence: int


def load_manifest(path: Path) -> tuple[ShadowCase, ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("equivalence manifest is unreadable") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("scope") != "authorized_queue_it_staging"
    ):
        raise ValueError("equivalence manifest lacks authorised staging provenance")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= 50:
        raise ValueError("equivalence manifest must contain between 1 and 50 cases")
    cases: list[ShadowCase] = []
    try:
        for raw in raw_cases:
            if not isinstance(raw, dict):
                raise TypeError
            session_id = raw["session_id"]
            artifact = raw["artifact"]
            sequence = raw["exchange_sequence"]
            if (
                not isinstance(session_id, str)
                or not session_id
                or not isinstance(artifact, str)
                or not artifact
                or not isinstance(sequence, int)
                or sequence < 1
            ):
                raise TypeError
            artifact_path = Path(artifact)
            if not artifact_path.is_absolute():
                artifact_path = path.parent / artifact_path
            cases.append(ShadowCase(session_id, artifact_path, sequence))
    except (KeyError, TypeError) as exc:
        raise ValueError("equivalence manifest contains an invalid case") from exc
    if len({case.session_id for case in cases}) != len(cases):
        raise ValueError("equivalence manifest contains a duplicate session")
    return tuple(cases)


async def run_equivalence_experiment(
    *,
    settings: Settings,
    database: Path,
    schema_path: Path,
    cases: tuple[ShadowCase, ...],
    protected_output_directory: Path,
) -> dict[str, object]:
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    run = await repository.get_active_run()
    if run is None or run.monitoring_strategy is not MonitoringStrategy.DIRECT:
        raise RuntimeError("the persisted run must use Direct Monitoring Strategy")
    if not await repository.is_monitoring_paused():
        raise RuntimeError("pause automatic monitoring before shadow comparison")
    schema = load_direct_response_schema(schema_path)
    admission = AdmissionDetector.from_urls(run.target_url)
    parser = DirectResponseParser(schema, admission_matcher=admission.matches)
    sessions = []
    state_parent: Path | None = None
    for case in cases:
        session = await repository.get(case.session_id)
        if session is None or session.queue_id is None:
            raise RuntimeError("every shadow case needs a persisted Queue ID")
        if session.worker_id is not None or session.manual_owner_id is not None:
            raise RuntimeError("shadow sessions must have no browser owner")
        if state_parent is None:
            state_parent = session.state_path.parent
        elif session.state_path.parent.resolve() != state_parent.resolve():
            raise RuntimeError("all shadow cases must use one protected state store")
        store = FileSystemStateStore(session.state_path.parent)
        if store.path_for(session.session_id).resolve() != session.state_path.resolve():
            raise RuntimeError("a shadow session state path is not its state-store document")
        sessions.append((case, session))
    if state_parent is None:
        raise RuntimeError("no shadow cases were supplied")
    runtime_settings = settings.model_copy(
        update={
            "staging_url": run.target_url,
            "browser_backend": run.browser_backend,
            "monitoring_strategy": run.monitoring_strategy,
            "state_directory": state_parent,
        }
    )
    state_store = FileSystemStateStore(state_parent)
    manager = BrowserManager.from_settings(runtime_settings)
    restorer = QueueSessionRestorer.from_settings(
        runtime_settings,
        browser_manager=manager,
        repository=repository,
        state_store=state_store,
    )
    reports: list[dict[str, object]] = []
    await manager.start()
    try:
        for index, (case, original_session) in enumerate(sessions, start=1):
            worker_id = f"phase8-equivalence-{uuid4()}"
            now = datetime.now(UTC)
            session = await repository.acquire_operator_lease(
                original_session.session_id,
                worker_id=worker_id,
                now=now,
                lease_until=now + timedelta(minutes=5),
            )
            try:
                reports.append(
                    await _run_case(
                        index=index,
                        case=case,
                        session=session,
                        parser=parser,
                        restorer=restorer,
                        state_store=state_store,
                        replay_store=ProtectedReplayStateStore(
                            protected_output_directory / "replay-state"
                        ),
                    )
                )
            finally:
                await repository.release_lease(session.session_id, worker_id=worker_id)
    finally:
        await manager.shutdown()
        await repository.close()
    kinds = Counter(
        field["kind"]
        for report in reports
        for field in cast(list[dict[str, object]], report.get("fields", []))
    )
    lifecycle = {
        status: _lifecycle_result(reports, status)
        for status in (
            "PRE_QUEUE",
            "ACTIVE_QUEUE",
            "PAUSED",
            "SERVICED_SOON",
            "TURN_STARTED",
            "ADMITTED",
        )
    }
    return {
        "schema_version": 1,
        "scope": "authorized_queue_it_staging",
        "generated_at": datetime.now(UTC).isoformat(),
        "sessions": len(reports),
        "comparisons": reports,
        "comparison_kind_counts": dict(sorted(kinds.items())),
        "lifecycle_coverage": lifecycle,
        "hard_failures": sum(bool(report.get("hard_failure")) for report in reports),
        "production_direct_monitoring_enabled": False,
    }


async def _run_case(
    *,
    index: int,
    case: ShadowCase,
    session: QueueSession,
    parser: DirectResponseParser,
    restorer: QueueSessionRestorer,
    state_store: FileSystemStateStore,
    replay_store: ProtectedReplayStateStore,
) -> dict[str, object]:
    if session.queue_id is None:
        raise TypeError("shadow case requires an identified QueueSession")
    queue_id = session.queue_id
    recipe = load_replay_recipe(
        case.artifact,
        session_id=session.session_id,
        expected_queue_id=queue_id,
        exchange_sequence=case.exchange_sequence,
    )
    browser_state = await state_store.load(session.session_id)
    if browser_state is None:
        raise RuntimeError("shadow case browser state is missing")
    replay_cookies = await replay_store.load(
        session_id=session.session_id, recipe_fingerprint=recipe.fingerprint
    )
    cookies = (
        replay_cookies
        if replay_cookies is not None
        else cookies_from_browser_state(browser_state)
    )

    async def direct_provider(_session: QueueSession) -> MonitoringObservation:
        async with DirectStatusReplayClient(
            recipe=recipe,
            cookies=cookies,
            state_store=replay_store,
            max_connections=1,
            max_concurrency=1,
        ) as client:
            result = await client.replay(HeaderProfile.FULL_DERIVED)
        if not result.succeeded or result.response_json is None:
            failure = result.failure.value if result.failure is not None else "schema"
            raise RuntimeError(f"direct_replay_{failure}")
        return parser.parse(
            result.response_json,
            session_id=session.session_id,
            expected_queue_id=queue_id,
            observed_at=datetime.now(UTC),
        )

    async def browser_provider(_session: QueueSession) -> MonitoringObservation:
        result = await restorer.restore(session)
        if not (is_verified_observation(result) or result.admitted or result.expired):
            failure = result.failure.value if result.failure is not None else "unverified"
            raise RuntimeError(f"browser_observation_{failure}")
        return browser_observation_from_restore_result(
            session_id=session.session_id,
            result=result,
            observed_at=datetime.now(UTC),
        )

    runner = ShadowEquivalenceRunner(
        direct_provider=direct_provider,
        browser_provider=browser_provider,
    )
    try:
        comparison = await runner.compare(session)
    except DirectObservationError as exc:
        return _failed_case(index, f"direct_{exc.failure.value}")
    except RuntimeError as exc:
        # Fixed prefixes only; no third-party error body or URL reaches the report.
        error = str(exc)
        safe = error if error.startswith(("direct_replay_", "browser_observation_")) else "runtime"
        return _failed_case(index, safe)
    report = comparison.report
    return {
        "case": index,
        "direct_status": report.direct_status.value,
        "browser_status": report.browser_status.value,
        "observation_gap_seconds": round(report.observation_gap_seconds, 6),
        "hard_failure": report.hard_failure,
        "equivalent": report.equivalent,
        "fields": [asdict(field) for field in report.fields],
    }


def _failed_case(index: int, failure: str) -> dict[str, object]:
    return {
        "case": index,
        "hard_failure": True,
        "equivalent": False,
        "failure": failure,
        "fields": [],
    }


def _lifecycle_result(reports: list[dict[str, object]], status: str) -> str:
    matching = [
        report
        for report in reports
        if report.get("direct_status") == status and report.get("browser_status") == status
    ]
    if not matching:
        return "UNKNOWN"
    return "FAIL" if any(report.get("hard_failure") for report in matching) else "PASS"


def _write_protected_report(directory: Path, report: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    marker = directory / ".gitignore"
    if not marker.exists():
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("*\n")
    path = directory / "observation-equivalence-report.json"
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=".equivalence-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _gates(confirmed: bool) -> None:
    if not confirmed:
        raise SystemExit("Pass --confirm-authorized-staging to enable shadow traffic")
    if os.environ.get("RUN_STAGING_TESTS") != "1" or os.environ.get(_EXPERIMENT_GATE) != "1":
        raise SystemExit(
            f"Set RUN_STAGING_TESTS=1 and {_EXPERIMENT_GATE}=1 to enable shadow traffic"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument(
        "--protected-output-directory", type=Path, default=Path(".observation-equivalence")
    )
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    args = parser.parse_args(argv)
    _gates(args.confirm_authorized_staging)
    report = asyncio.run(
        run_equivalence_experiment(
            settings=Settings(),
            database=args.database,
            schema_path=args.schema,
            cases=load_manifest(args.manifest),
            protected_output_directory=args.protected_output_directory,
        )
    )
    _write_protected_report(args.protected_output_directory, report)
    print(
        json.dumps(
            {
                "sessions": report["sessions"],
                "hard_failures": report["hard_failures"],
                "lifecycle_coverage": report["lifecycle_coverage"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
