"""Opt-in Phase 8 replay of one exact, browser-observed visitor-status candidate.

This is a protected evidence harness, not a production monitoring implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from queue_load_test.direct_replay import (
    DirectStatusReplayClient,
    HeaderProfile,
    ProtectedReplayStateStore,
    ReplayCookie,
    ReplayRecipe,
    classify_artifact_values,
    cookies_from_browser_state,
    load_replay_recipe,
)
from queue_load_test.models import MonitoringStrategy
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore

_EXPERIMENT_GATE = "RUN_PHASE8_DIRECT_REPLAY"


async def run_experiment(
    *,
    database: Path,
    artifact: Path,
    protected_output_directory: Path,
    session_id: str,
    exchange_sequence: int,
    checks_per_profile: int,
    timeout_seconds: float,
    max_cookie_candidates: int = 10,
) -> dict[str, object]:
    """Run bounded full-derived/minimal comparisons for one persisted visitor."""

    if checks_per_profile < 3 or checks_per_profile > 20:
        raise ValueError("checks per profile must be between 3 and 20")
    if max_cookie_candidates < 0 or max_cookie_candidates > 20:
        raise ValueError("cookie-candidate bound must be between 0 and 20")
    repository = SQLiteSessionRepository(database)
    await repository.initialize()
    run = await repository.get_active_run()
    if run is None or run.monitoring_strategy is not MonitoringStrategy.DIRECT:
        raise RuntimeError("the persisted run must use Direct Monitoring Strategy")
    if not await repository.is_monitoring_paused():
        raise RuntimeError("pause automatic monitoring before running the replay experiment")
    session = await repository.get(session_id)
    if session is None or session.queue_id is None:
        raise RuntimeError("the persisted session and authoritative Queue ID are required")
    if session.worker_id is not None or session.manual_owner_id is not None:
        raise RuntimeError("the replay session must have no automatic or manual browser owner")
    state_store = FileSystemStateStore(session.state_path.parent)
    if state_store.path_for(session_id).resolve() != session.state_path.resolve():
        raise RuntimeError("the session state path is not its protected state-store document")
    browser_state = await state_store.load(session_id)
    if browser_state is None:
        raise RuntimeError("the persisted browser state is missing")
    recipe = load_replay_recipe(
        artifact,
        session_id=session_id,
        expected_queue_id=session.queue_id,
        exchange_sequence=exchange_sequence,
    )
    replay_store = ProtectedReplayStateStore(protected_output_directory / "state")
    replay_cookies = await replay_store.load(
        session_id=session_id, recipe_fingerprint=recipe.fingerprint
    )
    restarted_from_replay_state = replay_cookies is not None
    cookies = (
        replay_cookies
        if replay_cookies is not None
        else cookies_from_browser_state(browser_state)
    )
    results = []
    async with DirectStatusReplayClient(
        recipe=recipe,
        cookies=cookies,
        state_store=replay_store,
        timeout_seconds=timeout_seconds,
        max_connections=2,
        max_concurrency=1,
    ) as client:
        for profile in (HeaderProfile.FULL_DERIVED, HeaderProfile.MINIMAL):
            for _ in range(checks_per_profile):
                results.append(await client.replay(profile))
    failures = Counter(
        result.failure.value for result in results if result.failure is not None
    )
    by_profile = {
        profile.value: {
            "attempts": len([item for item in results if item.header_profile is profile]),
            "successes": len(
                [item for item in results if item.header_profile is profile and item.succeeded]
            ),
            "identity_confirmed": len(
                [
                    item
                    for item in results
                    if item.header_profile is profile and item.identity_confirmed
                ]
            ),
            "cookie_refreshes": len(
                [
                    item
                    for item in results
                    if item.header_profile is profile and item.cookies_changed
                ]
            ),
        }
        for profile in HeaderProfile
    }
    full_results = [item for item in results if item.header_profile is HeaderProfile.FULL_DERIVED]
    all_full_succeeded = all(item.succeeded for item in full_results)
    all_full_identity = all(item.identity_confirmed for item in full_results)
    any_cookie_refresh = any(item.cookies_changed for item in results)
    cookie_minimization = await _cookie_minimization(
        recipe=recipe,
        cookies=cookies,
        directory=protected_output_directory / "cookie-ablation",
        checks=checks_per_profile,
        timeout_seconds=timeout_seconds,
        maximum=max_cookie_candidates,
        baseline_valid=all_full_succeeded and all_full_identity,
    )
    stability = [asdict(item) for item in classify_artifact_values(artifact)]
    # A single-session external replay cannot answer cross-session reuse, live-browser
    # effects, or full application-restart equivalence. Never promote it by itself.
    return {
        "schema_version": 1,
        "sensitivity": "PROTECTED_DIRECT_REPLAY_EVIDENCE",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "authorized_queue_it_staging",
        "sessions": 1,
        "checks_per_profile": checks_per_profile,
        "browser_open_case": "NOT_RUN",
        "browser_closed_case": "NO_PERSISTED_BROWSER_OWNER; RUNTIME_NOT_OBSERVED",
        "application_restart_case": (
            "PROTECTED_REPLAY_STATE_RELOADED" if restarted_from_replay_state else "NOT_RUN"
        ),
        "profiles": by_profile,
        "cookie_minimization": cookie_minimization,
        "state_classification": stability,
        "questions": {
            "storage_state_sufficient": "UNKNOWN",
            "cookies_actually_required": (
                "PASS" if any(item["classification"] == "required" for item in cookie_minimization) else "UNKNOWN"
            ),
            "additional_session_values_required": "UNKNOWN",
            "event_values_reusable_across_sessions": "UNKNOWN",
            "semantically_required_headers": "UNKNOWN",
            "request_body_changes_between_polls": _classification_result(
                stability, "request body fields", "request-transient"
            ),
            "query_values_rotate": _classification_result(
                stability, "query parameters", "request-transient"
            ),
            "response_updates_cookies_or_tokens": "PASS" if any_cookie_refresh else "UNKNOWN",
            "external_replay_alters_open_browser": "UNKNOWN",
            "replay_after_browser_context_closed": "UNKNOWN",
            "replay_after_application_restart": "UNKNOWN",
            "authoritative_queue_id_preserved": (
                "PASS" if all_full_succeeded and all_full_identity else "UNKNOWN"
            ),
        },
        "failures": dict(sorted(failures.items())),
        "readiness": "UNKNOWN",
        "readiness_basis": (
            "One session cannot establish cross-session reuse, browser-side effects, "
            "or application-restart equivalence."
        ),
    }


async def _cookie_minimization(
    *,
    recipe: ReplayRecipe,
    cookies: tuple[ReplayCookie, ...],
    directory: Path,
    checks: int,
    timeout_seconds: float,
    maximum: int,
    baseline_valid: bool,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for index, _cookie in enumerate(cookies[:maximum], start=1):
        remaining = cookies[: index - 1] + cookies[index:]
        results = []
        store = ProtectedReplayStateStore(directory / f"candidate-{index}")
        async with DirectStatusReplayClient(
            recipe=recipe,
            cookies=remaining,
            state_store=store,
            timeout_seconds=timeout_seconds,
            max_connections=1,
            max_concurrency=1,
        ) as client:
            for _ in range(checks):
                results.append(await client.replay(HeaderProfile.FULL_DERIVED))
        failures = [item.failure.value for item in results if item.failure is not None]
        if baseline_valid and len(failures) == checks and set(failures) <= {
            "state",
            "identity",
        }:
            classification = "required"
        elif all(item.succeeded and item.identity_confirmed for item in results):
            classification = "not_required_repeated_evidence"
        else:
            classification = "unknown"
        findings.append(
            {
                "candidate": f"cookie-{index}",
                "name": _cookie.name,
                "attempts": checks,
                "successes": sum(item.succeeded for item in results),
                "classification": classification,
            }
        )
    return findings


def _classification_result(
    findings: list[dict[str, object]], value: str, expected: str
) -> str:
    item = next((finding for finding in findings if finding.get("value") == value), None)
    return "PASS" if item is not None and item.get("classification") == expected else "UNKNOWN"


def _gates(confirm_authorized_staging: bool) -> None:
    if not confirm_authorized_staging:
        raise SystemExit("Pass --confirm-authorized-staging to enable replay traffic")
    if os.environ.get("RUN_STAGING_TESTS") != "1" or os.environ.get(_EXPERIMENT_GATE) != "1":
        raise SystemExit(
            f"Set RUN_STAGING_TESTS=1 and {_EXPERIMENT_GATE}=1 to enable replay traffic"
        )


def _write_protected_report(directory: Path, report: dict[str, object]) -> Path:
    # The cookie store has already created this mode-0700, ignore-all directory.
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
    path = directory / "direct-replay-report.json"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=".direct-replay-report-", suffix=".tmp"
    )
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--protected-output-directory", type=Path, default=Path(".direct-replay"))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--exchange-sequence", type=int, required=True)
    parser.add_argument("--checks-per-profile", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-cookie-candidates", type=int, default=10)
    parser.add_argument("--confirm-authorized-staging", action="store_true")
    args = parser.parse_args(argv)
    _gates(args.confirm_authorized_staging)
    report = asyncio.run(
        run_experiment(
            database=args.database,
            artifact=args.artifact,
            protected_output_directory=args.protected_output_directory,
            session_id=args.session_id,
            exchange_sequence=args.exchange_sequence,
            checks_per_profile=args.checks_per_profile,
            timeout_seconds=args.timeout_seconds,
            max_cookie_candidates=args.max_cookie_candidates,
        )
    )
    _write_protected_report(args.protected_output_directory, report)
    # Deliberately emit only aggregate classifications, never paths or identity data.
    print(json.dumps({"readiness": report["readiness"], "profiles": report["profiles"]}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
