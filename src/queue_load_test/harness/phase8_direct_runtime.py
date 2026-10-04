"""Phase 8 Prompt 5: complete Direct Monitoring Strategy workflow (local simulator only).

The real operator stack runs end to end: FastAPI app and lifespan, persisted
RunConfig with ``monitoring_strategy=direct``, browser-based creation, the bounded
scheduler, the Direct handler with its browser fallback, Prompt 2 status discovery
(``local_simulator`` scope), protected direct-monitor records, operator actions,
headed Open, SQLite, and an installed browser backend.

The target is ``LocalQueueSimulator`` on 127.0.0.1. Its visitor page polls a JSON
status endpoint that the discovery observer captures, and the response schema is the
simulator's own. This is **not** Queue-it, contacts no staging environment, and is not
evidence of Queue-it direct-status behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from queue_load_test.config import Settings
from queue_load_test.direct_monitor import DirectCapability, DirectMonitorStateStore
from queue_load_test.harness.local_queue_simulator import (
    STAGE_SERVICED,
    LocalQueueSimulator,
)
from queue_load_test.harness.phase5_workflow import (
    Database,
    Recorder,
    _browser_processes,
    _dd,
    until,
    workflow_settings,
)
from queue_load_test.metrics.logging import JsonLogFormatter
from queue_load_test.models import BrowserBackendName, MonitoringStrategy
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.utils.instance_lock import InstanceLock
from queue_load_test.web.app import create_app
from queue_load_test.web.service import ApplicationRunRuntime

REQUESTED = 3
DISCOVERY_RETENTION = 3
HX = {"HX-Request": "true"}

# Fault -> the fallback reason the Direct handler must classify it as.
FAULT_REASONS = {
    "http_500": "unexpected_http_status",
    "slow": "timeout",
    "redirect": "unexpected_redirect",
    "html": "unexpected_content_type",
    "malformed": "malformed_response",
    "wrong_type": "schema_incompatible",
    "missing_id": "identity_ambiguity",
    "mismatch": "identity_mismatch",
    "rejected": "rejected_session_state",
    # Connection-lost lifecycle; a matching Queue ID alone is accepted as PRE_QUEUE.
    "unknown_lifecycle": "unknown_lifecycle",
    "contradictory": "contradictory_lifecycle",
    "admitted": "unsupported_admission",
}


def direct_settings(
    directory: Path,
    *,
    database: Path,
    backend: BrowserBackendName,
) -> Settings:
    schema_path = directory / "protected-schema" / "simulator-schema.json"
    schema_path.parent.mkdir(mode=0o700, exist_ok=True)
    schema_path.write_text(json.dumps(LocalQueueSimulator.status_schema()), encoding="utf-8")
    schema_path.chmod(0o600)
    return workflow_settings(directory, database=database, backend=backend).model_copy(
        update={
            "status_discovery_enabled": True,
            "status_discovery_scope": "local_simulator",
            "status_discovery_directory": directory / "status-discovery",
            "status_discovery_observe_seconds": 1.2,
            "status_discovery_cleanup_timeout_seconds": 2.0,
            "direct_monitor_directory": directory / "direct-monitor",
            "direct_monitor_schema_path": schema_path,
            "direct_monitor_timeout_seconds": 1.0,
            "direct_monitor_failure_threshold": 2,
            # The fault matrix below re-adopts right after each hard failure; the
            # cooldown itself is covered by unit tests.
            "direct_monitor_readopt_cooldown_seconds": 0.0,
            "direct_monitor_discovery_retention": DISCOVERY_RETENTION,
        }
    )


async def run_direct_workflow(
    directory: Path,
    *,
    backend: BrowserBackendName = BrowserBackendName.CHROME,
) -> dict[str, Any]:
    record = Recorder()
    secret = f"SIMSECRET{secrets.token_hex(8)}"
    simulator = LocalQueueSimulator(
        new_identity_prefix="sim-direct",
        status_enabled=True,
        slow_seconds=2.5,
        secret_token=secret,
    )
    captured_logs = _LogCapture()
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(captured_logs)
    root_logger.setLevel(logging.DEBUG)
    await simulator.start()
    database_path = directory / "direct.sqlite3"
    db = Database(database_path)
    settings = direct_settings(directory, database=database_path, backend=backend)
    store = DirectMonitorStateStore(settings.direct_monitor_directory)
    evidence: dict[str, Any] = {"browser_backend": backend.value, "requested": REQUESTED}

    def build(configured: Settings = settings) -> tuple[Any, ApplicationRunRuntime]:
        repository = SQLiteSessionRepository(database_path)
        runtime = ApplicationRunRuntime(
            settings=configured, repository=repository, manual_headless=True
        )
        app = create_app(
            settings=configured,
            repository=repository,
            runtime=runtime,
            instance_lock=InstanceLock.for_database(database_path),
        )
        return app, runtime

    async def capability(session_id: str) -> DirectCapability | None:
        loaded = await store.load(session_id)
        return loaded.capability if loaded is not None else None

    def queue(session_id: str) -> str:
        value = db.identities()[session_id]
        assert value is not None
        return value

    try:
        app, runtime = build()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
                # ------------------------------------------------------ setup
                response = await ui.post(
                    "/setup",
                    data={
                        "target_url": simulator.entry_url,
                        "requested_sessions": str(REQUESTED),
                        "monitoring_strategy": MonitoringStrategy.DIRECT.value,
                    },
                )
                record.check(
                    "direct_run_persists_strategy",
                    response.status_code == 303
                    and db.value("SELECT monitoring_strategy FROM run_config") == "direct",
                )
                elapsed = await until(lambda: db.valid() == REQUESTED, timeout=60, interval=0.25)
                session_ids = sorted(db.identities())
                record.check(
                    "creation_is_browser_based_and_unchanged",
                    elapsed is not None
                    and simulator.new_identities == REQUESTED
                    and sum(simulator.page_requests.values()) >= REQUESTED,
                    seconds=elapsed,
                )

                # ------------------------------------ discovery -> capability
                async def all_capable() -> bool:
                    states = [await capability(item) for item in session_ids]
                    return all(state is DirectCapability.DIRECT_CAPABLE for state in states)

                elapsed = await until(all_capable, timeout=60, interval=0.25)
                metrics = runtime.direct_monitoring_metrics
                record.check(
                    "browser_fallback_discovers_legitimate_recipe_per_session",
                    elapsed is not None
                    and metrics is not None
                    and metrics.fallback_reasons.get("discovery_required", 0) >= REQUESTED
                    and metrics.recipes_adopted >= REQUESTED
                    and all(simulator.browser_status_requests[queue(item)] > 0 for item in session_ids),
                    seconds=elapsed,
                )
                record.check(
                    "replayable_recipe_is_protected_not_in_sqlite",
                    all(
                        (store.path_for(item).stat().st_mode & 0o777) == 0o600
                        for item in session_ids
                    )
                    and b"/status?q=" not in database_path.read_bytes(),
                )

                # --------------------------------------- direct steady state
                first, second, third = session_ids
                q_first, q_second, q_third = queue(first), queue(second), queue(third)
                pages_before = dict(simulator.page_requests)
                direct_before = simulator.direct_status_requests[q_first]
                simulator.progress[q_first] = 73
                simulator.stages[q_second] = STAGE_SERVICED
                elapsed = await until(
                    lambda: db.progress(first) == 73.0
                    and (db.session(second) or {}).get("status") == "SERVICED_SOON",
                    timeout=30,
                )
                record.check(
                    "direct_checks_persist_progress_and_lifecycle_without_browser",
                    elapsed is not None
                    and simulator.direct_status_requests[q_first] > direct_before
                    and simulator.page_requests[q_first] == pages_before.get(q_first)
                    and simulator.page_requests[q_second] == pages_before.get(q_second),
                    seconds=elapsed,
                )
                record.check(
                    "direct_checks_hold_no_browser_ownership_or_contexts",
                    (await runtime.capacity()).active_contexts <= 1,
                )

                # ---------------------------------------- every fallback class
                baseline_identities = db.identities()
                created_before = simulator.new_identities
                observed: dict[str, bool] = {}
                for fault, reason in FAULT_REASONS.items():
                    active = runtime.direct_monitoring_metrics
                    assert active is not None
                    before_reason = active.fallback_reasons.get(reason, 0)
                    before_pages = simulator.page_requests[q_third]
                    simulator.status_faults[q_third] = fault

                    def classified(
                        reason: str = reason, before_reason: int = before_reason
                    ) -> bool:
                        current = runtime.direct_monitoring_metrics
                        return (
                            current is not None
                            and current.fallback_reasons.get(reason, 0) > before_reason
                        )

                    def browser_used_since(before: int = before_pages) -> bool:
                        return simulator.page_requests[q_third] > before

                    seen = await until(classified, timeout=20)
                    browser_used = await until(browser_used_since, timeout=10)
                    simulator.status_faults.pop(q_third, None)
                    observed[fault] = seen is not None and browser_used is not None
                    record.check(
                        f"fallback_{fault}_uses_browser_monitor",
                        observed[fault]
                        and queue(third) == q_third
                        and (db.session(third) or {}).get("status") not in {"FAILED", "EXPIRED"},
                        reason=reason,
                    )
                record.check(
                    "direct_failures_never_reacquire_or_replace_identity",
                    db.identities() == baseline_identities
                    and simulator.new_identities == created_before,
                )
                recovered = await until(
                    lambda: simulator.direct_status_requests[q_third] > 0, timeout=5
                )
                direct_mark = simulator.direct_status_requests[q_third]
                pages_mark = simulator.page_requests[q_third]
                resumed = await until(
                    lambda: simulator.direct_status_requests[q_third] > direct_mark + 1,
                    timeout=30,
                )
                record.check(
                    "legitimate_browser_fallback_refreshes_recipe_and_direct_resumes",
                    recovered is not None
                    and resumed is not None
                    and await capability(third) is DirectCapability.DIRECT_CAPABLE
                    and simulator.page_requests[q_third] <= pages_mark + 1,
                )
                summary = (await ui.get("/partials/summary")).text
                record.check(
                    "run_stays_direct_while_sessions_fall_back",
                    _dd(summary, "Monitoring strategy") == "Direct Monitoring Strategy"
                    and db.value("SELECT monitoring_strategy FROM run_config") == "direct",
                )

                # --------------------------------------------- pause/resume
                await ui.post("/monitoring/pause", headers=HX)
                await until(lambda: db.owners() == 0, timeout=15)
                paused_direct = sum(simulator.direct_status_requests.values())
                paused_pages = sum(simulator.page_requests.values())
                await asyncio.sleep(3.0)
                summary = (await ui.get("/partials/summary")).text
                record.check(
                    "pause_stops_direct_and_browser_automatic_checks",
                    sum(simulator.direct_status_requests.values()) == paused_direct
                    and sum(simulator.page_requests.values()) == paused_pages
                    and "PAUSED" in summary
                    and int(_dd(summary, "Due backlog") or "0") >= 1,
                )

                # ------------------------------- Refresh Now while paused
                simulator.progress[q_first] = 81
                refresh_pages = simulator.page_requests[q_first]
                refresh_direct = simulator.direct_status_requests[q_first]
                response = await ui.post(
                    f"/sessions/{first}/refresh?token=direct-refresh", headers=HX
                )
                elapsed = await until(lambda: db.progress(first) == 81.0, timeout=20)
                record.check(
                    "refresh_now_uses_direct_strategy_while_paused",
                    "Refresh requested" in response.text
                    and elapsed is not None
                    and simulator.direct_status_requests[q_first] == refresh_direct + 1
                    and simulator.page_requests[q_first] == refresh_pages
                    and queue(first) == q_first,
                )
                simulator.status_faults[q_first] = "http_500"
                response = await ui.post(
                    f"/sessions/{first}/refresh?token=direct-refresh-fallback", headers=HX
                )
                elapsed = await until(
                    lambda: simulator.page_requests[q_first] > refresh_pages, timeout=20
                )
                await until(lambda: (db.session(first) or {}).get("worker_id") is None, timeout=15)
                simulator.status_faults.pop(q_first, None)
                record.check(
                    "refresh_now_falls_back_to_browser_on_direct_failure",
                    elapsed is not None and queue(first) == q_first,
                )
                await ui.post("/monitoring/resume", headers=HX)
                resumed_at = sum(simulator.direct_status_requests.values())
                elapsed = await until(
                    lambda: sum(simulator.direct_status_requests.values()) > resumed_at,
                    timeout=20,
                )
                record.check("resume_continues_direct_monitoring", elapsed is not None)

                # ------------------------------------------- manual Open
                await until(lambda: (db.session(first) or {}).get("worker_id") is None, timeout=15)
                response = await ui.post(f"/sessions/{first}/open", headers=HX)
                opened = await until(
                    lambda: (db.session(first) or {}).get("manual_owner_id") is not None,
                    timeout=20,
                )
                await asyncio.sleep(0.5)
                open_direct = simulator.direct_status_requests[q_first]
                open_browser = simulator.browser_status_requests[q_first]
                await asyncio.sleep(3.5)
                record.check(
                    "manual_open_is_browser_based_and_fences_direct_polling",
                    opened is not None
                    and simulator.direct_status_requests[q_first] == open_direct
                    and simulator.browser_status_requests[q_first] > open_browser,
                )
                await ui.post(f"/sessions/{first}/close", headers=HX)
                closed = await until(
                    lambda: (db.session(first) or {}).get("manual_owner_id") is None,
                    timeout=20,
                )
                after_close = simulator.direct_status_requests[q_first]
                elapsed = await until(
                    lambda: simulator.direct_status_requests[q_first] > after_close, timeout=20
                )
                record.check(
                    "close_releases_ownership_and_direct_resumes",
                    closed is not None and elapsed is not None and queue(first) == q_first,
                )

                # ------------------------------------ Add / Replace / Delete
                before = db.identities()
                await ui.post("/sessions/new?token=direct-add", headers=HX)
                elapsed = await until(lambda: db.valid() == REQUESTED + 1, timeout=40)
                added = next(iter(set(db.identities()) - set(before)), None)
                became_capable = (
                    await until(
                        lambda: _capable_sync(store, added) if added else False, timeout=40
                    )
                    if added is not None
                    else None
                )
                record.check(
                    "add_is_browser_based_and_new_session_becomes_direct_capable",
                    elapsed is not None and added is not None and became_capable is not None,
                )
                await until(lambda: (db.session(second) or {}).get("worker_id") is None, timeout=15)
                snapshot = db.identities()
                await ui.post(f"/sessions/{second}/replace?token=direct-replace", headers=HX)
                elapsed = await until(
                    lambda: db.session(second) is None and not store.path_for(second).exists(),
                    timeout=40,
                )
                record.check(
                    "replace_removes_old_direct_record_and_keeps_others",
                    elapsed is not None
                    and await capability(second) is None
                    and all(
                        db.identities().get(key) == value
                        for key, value in snapshot.items()
                        if key != second
                    ),
                )
                await until(lambda: (db.session(third) or {}).get("worker_id") is None, timeout=15)
                await ui.post(f"/sessions/{third}/delete?token=direct-delete", headers=HX)
                elapsed = await until(
                    lambda: db.session(third) is None and not store.path_for(third).exists(),
                    timeout=20,
                )
                record.check(
                    "delete_removes_direct_record_and_cookies",
                    elapsed is not None
                    and await capability(third) is None
                    and not store.cookie_path(third).exists(),
                )
                evidence["direct_metrics"] = _metrics(runtime)
                survivors = db.identities()

                # -------------------------------- observability and secrecy
                exposition = (await ui.get("/metrics")).text
                evidence["metrics_conditions"] = {
                    "strategy": 'monitoring_strategy_info{strategy="direct"} 1.0' in exposition,
                    "attempts": _sample(exposition, "direct_monitoring_attempts_total"),
                    "successes": _sample(exposition, "direct_monitoring_successes_total"),
                    "timeout": 'direct_monitoring_fallbacks_total{reason="timeout"}'
                    in exposition,
                    "capable": 'direct_monitoring_sessions{capability="DIRECT_CAPABLE"}'
                    in exposition,
                    "session_label": 'session_id="' in exposition,
                    "queue_label": 'queue_id="' in exposition,
                }
                record.check(
                    "metrics_expose_low_cardinality_direct_series",
                    'monitoring_strategy_info{strategy="direct"} 1.0' in exposition
                    and _sample(exposition, "direct_monitoring_attempts_total") > 0
                    and _sample(exposition, "direct_monitoring_successes_total") > 0
                    and 'direct_monitoring_fallbacks_total{reason="timeout"}' in exposition
                    and "direct_monitoring_request_duration_seconds_count" in exposition
                    and "direct_monitoring_fallback_duration_seconds_count" in exposition
                    and 'direct_monitoring_sessions{capability="DIRECT_CAPABLE"}' in exposition
                    and 'session_id="' not in exposition
                    and 'queue_id="' not in exposition
                    and not any(
                        queue_value in exposition
                        for queue_value in survivors.values()
                        if queue_value
                    ),
                )
                summary = (await ui.get("/partials/summary")).text
                record.check(
                    "dashboard_shows_aggregate_direct_capability_only",
                    _dd(summary, "Direct DIRECT_CAPABLE") is not None
                    and _dd(summary, "Direct checks") is not None,
                )
                surfaces = {
                    "dashboard": (await ui.get("/dashboard")).text,
                    "summary": summary,
                    "sessions": (await ui.get("/partials/sessions")).text,
                    "metrics": exposition,
                    "sqlite": database_path.read_bytes().decode("latin-1"),
                }
                leaked = sorted(name for name, text in surfaces.items() if secret in text)
                record.check("fake_secret_absent_from_ui_metrics_and_sqlite", leaked == [],
                             leaked=leaked)
                retained = [
                    len(list(settings.status_discovery_directory.glob(f"*-{item}.json")))
                    for item in (first,)
                ]
                record.check(
                    "discovery_evidence_retention_is_bounded",
                    all(count <= DISCOVERY_RETENTION for count in retained)
                    and metrics_pruned(runtime) > 0,
                    retained=retained,
                )
                protected_holds_secret = any(
                    secret in path.read_text(encoding="utf-8")
                    for path in (settings.direct_monitor_directory / "records").glob("*.json")
                )
                record.check(
                    "secret_bearing_recipe_lives_only_in_protected_store",
                    protected_holds_secret,
                )
        record.check(
            "shutdown_releases_all_ownership_and_browsers",
            db.owners() == 0 and _browser_processes(backend) == 0,
            browser_processes=_browser_processes(backend),
        )

        # ---------------------------------------------- restart / immutability
        headed_default = settings.model_copy(
            update={"monitoring_strategy": MonitoringStrategy.HEADED_WINDOW}
        )
        app, runtime = build(headed_default)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as ui:
                summary = (await ui.get("/partials/summary")).text
                record.check(
                    "restart_keeps_persisted_direct_strategy_despite_new_default",
                    _dd(summary, "Monitoring strategy") == "Direct Monitoring Strategy"
                    and runtime.direct_monitoring_metrics is not None
                    and db.identities() == survivors,
                )
                q_first = queue(first)
                restart_pages = simulator.page_requests[q_first]
                restart_direct = simulator.direct_status_requests[q_first]
                await ui.post("/monitoring/resume", headers=HX)
                elapsed = await until(
                    lambda: simulator.direct_status_requests[q_first] > restart_direct,
                    timeout=30,
                )
                record.check(
                    "restart_recovers_direct_capability_without_rediscovery",
                    elapsed is not None
                    and simulator.page_requests[q_first] == restart_pages
                    and await capability(first) is DirectCapability.DIRECT_CAPABLE,
                )
                record.check(
                    "restart_never_reacquires_identity",
                    db.identities() == survivors,
                )
                evidence["restart_direct_metrics"] = _metrics(runtime)
                started = time.perf_counter()
                response = await ui.post("/run/reset", headers=HX)
                records_left = [
                    path
                    for path in (settings.direct_monitor_directory / "records").glob("*.json")
                ]
                record.check(
                    "stop_and_reset_clears_direct_records_and_run",
                    response.headers.get("HX-Redirect") == "/setup"
                    and records_left == []
                    and db.value("SELECT COUNT(*) FROM run_config") == 0
                    and db.value("SELECT COUNT(*) FROM queue_sessions") == 0,
                    seconds=round(time.perf_counter() - started, 2),
                )
    finally:
        await simulator.close()
        root_logger.removeHandler(captured_logs)
        root_logger.setLevel(previous_level)
    leaking = sorted(
        {
            json.loads(line).get("logger", "?") + ":" + json.loads(line).get("message", "")[:40]
            for line in captured_logs.lines
            if secret in line
        }
    )
    record.check(
        "fake_secret_absent_from_structured_logs",
        not leaking and len(captured_logs.lines) > 0,
        lines=len(captured_logs.lines),
        leaking_loggers=[item.replace(secret, "<secret>") for item in leaking],
    )
    evidence["simulator"] = {
        "new_identities": simulator.new_identities,
        "page_requests": sum(simulator.page_requests.values()),
        "browser_status_requests": sum(simulator.browser_status_requests.values()),
        "direct_status_requests": sum(simulator.direct_status_requests.values()),
    }
    passed = sum(check.passed for check in record.checks)
    result: dict[str, Any] = {
        "scope": "local_simulator_only",
        "queue_it_contacted": False,
        "passed": passed,
        "failed": len(record.checks) - passed,
        "checks": [
            {"name": check.name, "passed": check.passed, **check.detail} for check in record.checks
        ],
        "evidence": evidence,
    }
    report_leak = secret in json.dumps(result, default=str)
    record.check("fake_secret_absent_from_aggregate_report", not report_leak)
    result["checks"].append(
        {"name": "fake_secret_absent_from_aggregate_report", "passed": not report_leak}
    )
    result["passed"] += int(not report_leak)
    result["failed"] += int(report_leak)
    return result


class _LogCapture(logging.Handler):
    """Collect every record exactly as the application's JSON formatter emits it."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonLogFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _sample(exposition: str, name: str) -> float:
    for line in exposition.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[1])
    return 0.0


def metrics_pruned(runtime: ApplicationRunRuntime) -> int:
    metrics = runtime.direct_monitoring_metrics
    return metrics.artifacts_pruned if metrics is not None else 0


def _capable_sync(store: DirectMonitorStateStore, session_id: str) -> bool:
    path = store.path_for(session_id)
    try:
        return '"capability":"DIRECT_CAPABLE"' in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _metrics(runtime: ApplicationRunRuntime) -> dict[str, Any]:
    metrics = runtime.direct_monitoring_metrics
    if metrics is None:
        return {}
    return {
        "checks": metrics.checks,
        "direct_requests": metrics.direct_requests,
        "direct_successes": metrics.direct_successes,
        "browser_fallbacks": metrics.browser_fallbacks,
        "recipes_adopted": metrics.recipes_adopted,
        "fallback_reasons": dict(sorted(metrics.fallback_reasons.items())),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--backend",
        choices=tuple(backend.value for backend in BrowserBackendName),
        default=BrowserBackendName.PATCHRIGHT.value,
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="phase8-direct-runtime-") as directory:
        result = asyncio.run(
            run_direct_workflow(Path(directory), backend=BrowserBackendName.parse(args.backend))
        )
    text = json.dumps(result, indent=2, default=str)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(f"{result['passed']} passed, {result['failed']} failed")
    raise SystemExit(0 if result["failed"] == 0 else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
