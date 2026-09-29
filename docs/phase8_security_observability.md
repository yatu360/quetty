# Phase 8 Prompt 6 — Direct Monitoring Security, Observability, and Failure Hardening

**Date:** 2026-09-29

**Implementation status:** COMPLETE

**Queue-it staging:** NOT RUN (every Queue-it direct question remains UNKNOWN)

Headed Window Strategy semantics are unchanged. Everything here applies to the Direct
Monitoring Strategy path and to shared logging.

## Sensitive-data policy

These are treated as sensitive: Cookie, Set-Cookie, Authorization, API keys,
CSRF/session/bearer tokens, raw `storage_state`, transfer URLs, visitor-status URLs,
raw request and response bodies, HAR/network traces, and protected direct-monitor
recipes and state.

| Surface | What it may contain |
|---|---|
| Structured logs | session ID, persisted Queue ID (existing policy), worker ID, lifecycle status, closed reason enum values, exception **type names**, counts, durations |
| Prometheus | aggregate counters, gauges, and histograms. Labels are closed enums only: `reason`, `capability`, `strategy`. No session, Queue ID, or URL label |
| Dashboard | run-level strategy, aggregate direct capability counts, direct/fallback totals |
| SQLite `direct_monitor_status` | capability, sanitized last reason, one-way recipe reference, consecutive failures, last success/failure/update times |
| Protected `.direct-monitor/` store | the exact recipe (URL, method, headers, body) and response cookies |
| Protected discovery directory | raw Prompt 2 evidence (unchanged policy, now retention-bounded) |
| Reports (`docs/results`, consistency report) | aggregate counts, check names, session IDs for findings only |

Queue IDs follow the existing policy. They never appear as a Prometheus label or in
aggregate benchmark reports. Structured logs keep the persisted expected Queue ID, as
elsewhere in the project. The contradicting value from a direct response is never
logged.

## Protected state

`DirectMonitorStateStore` is the minimal extension of the protected boundary:

- atomic temp-file plus `os.replace` writes, with fsync, mode 0600, in mode-0700
  directories, with ignore-all markers;
- each record is bound to its session and the persisted Queue ID;
- a SHA-256 integrity check detects corruption, and no partial record is ever
  visible;
- restart-safe: record format version 2 adds `unavailable_since`, and version 1 is
  still readable.

Prompt 6 changes:

- **No automatic destructive repair.** A corrupt record, or a record for another
  identity, is no longer overwritten. The check falls back as `recipe_uncertain` and
  the file stays for the report. Only a new legitimate browser observation replaces it
  (`adopt_recipe`).
- **Report-only consistency audit.** `DirectMonitorConsistencyChecker`, also available
  as `queue-load-test-state-check --direct-monitor-directory .direct-monitor`, reports:
  - `corrupt_record`;
  - `orphan_record`;
  - `identity_mismatch`;
  - `orphan_cookies`;
  - `metadata_mismatch` (SQLite versus record);
  - `unsafe_permissions`.

  It reports session IDs and kinds only, performs no repairs
  (`repairs_performed: 0`), and changes no file bytes or modes.
- **Bookkeeping never loses a valid check.** If the success-counter save fails after a
  valid direct observation, the observation is still persisted. If a failure-counter
  save fails, the browser fallback still runs.

## SQLite metadata (schema change)

A new table was added. `CREATE TABLE IF NOT EXISTS` migrates existing databases, and
`ON DELETE CASCADE` means Delete and Replace remove the row. `reset_all` clears it.

```sql
direct_monitor_status(session_id PK → queue_sessions, capability, last_reason,
    recipe_reference, consecutive_failures, last_success_at, last_failure_at, updated_at)
```

`recipe_reference` is `r1-` followed by 16 hex characters of
`sha256("direct-recipe-reference:" + fingerprint)`. It tracks which recipe version is in
use without revealing the recipe or its fingerprint. The handler writes the row after
each Direct check (best effort; a database failure is counted and logged, and never
fails the check). `direct_capability_counts()` provides aggregates: a monitorable
identified session without a row counts as `DISCOVERY_REQUIRED`.

## Observability

Prometheus (`GET /metrics` on the operator UI; aggregate only):

| Metric | Type |
|---|---|
| `monitoring_strategy_info{strategy}` | gauge (1 for the current run) |
| `direct_monitoring_attempts_total` | counter |
| `direct_monitoring_successes_total` | counter |
| `direct_monitoring_fallbacks_total{reason}` | counter (every reason pre-registered) |
| `direct_monitoring_request_duration_seconds` | histogram |
| `direct_monitoring_fallback_duration_seconds` | histogram |
| `direct_monitoring_disagreements_total` | counter: a refused direct observation (for example unknown lifecycle) whose evaluated status differs from the browser fallback's |
| `direct_monitoring_identity_mismatches_total` | counter |
| `direct_monitoring_recipes_refreshed_total` | counter |
| `direct_monitoring_sessions{capability}` | gauge, refreshed from SQLite aggregates on scrape |

The dashboard summary for Direct runs shows `Direct DISCOVERY_REQUIRED`,
`Direct DIRECT_CAPABLE`, `Direct DIRECT_UNAVAILABLE`, and `Direct checks`
(`N direct / M fallback`).

Structured events:

- `direct_monitor_success`
- `direct_monitor_failure` (reason enum)
- `direct_monitor_fallback` (reason enum and browser duration)
- `direct_recipe_refreshed`
- `direct_identity_mismatch`
- `direct_browser_disagreement`
- `direct_capability_unavailable`
- `direct_monitor_request_error` (exception type only)
- `direct_monitor_record_save_failed`
- `direct_monitor_metadata_failed`

## HTTP client logging (leak found and fixed)

The workflow's seeded-secret check found a real leak. At DEBUG level, `httpcore` logs
raw response headers, so a seeded `Set-Cookie` value reached the log stream. URL
redaction alone did not catch it. The fix has two layers:

1. `quiet_http_client_loggers()` floors `httpx`, `httpcore`, `h11`, `h2`, and `hpack`
   at WARNING (children inherit it). It runs when the replay client is imported and in
   `configure_structured_logging`.
2. `JsonLogFormatter` replaces every message from those loggers with
   `<redacted-http-client-detail>` at any level. Logger name, level, and exception type
   remain.

Direct-path exception handling records only `type(exc).__name__`, never the message,
because HTTP library exceptions can carry the URL.

## Failure hardening

| Scenario | Behavior (expected Queue ID preserved in every case) |
|---|---|
| Expired cookies | not replayed. The server rejection gives `rejected_session_state` and a browser fallback |
| Browser-state corruption | `missing_visitor_state`, no request, browser fallback |
| Replay-cookie corruption | cookies discarded, then the browser `storage_state` cookies are used |
| Recipe/record corruption | `recipe_uncertain`, no request, file kept for the report |
| Malformed or oversize response | `malformed_response`, hard failure |
| 4xx/5xx (400/404/409/429/502/503) | `unexpected_http_status`, soft failure |
| 401/403/410 | `rejected_session_state`, hard failure |
| Redirect loop | redirects are never followed: exactly one request, `unexpected_redirect` |
| Network interruption (read/write/protocol errors) | `network`. Pool timeout gives `timeout` |
| Application cancellation mid-request | `CancelledError` propagates; record, session, and status untouched; no browser call |
| Shutdown during a real direct request | the scheduler's bounded shutdown cancels it and releases the lease |
| Browser failure after a direct failure | the direct failure is recorded; the browser error propagates to the existing worker repark |
| Record-save failure after a successful observation | observation persisted, failure logged |
| Cookie-persistence failure during replay | `uncertain`, browser fallback |
| Database failure after a successful observation | the worker reparks (`monitor:worker_failure`); no browser fallback (it is not a direct failure); capability unchanged |
| Metadata database failure | check succeeds; failure counted |
| Stale lease owner returning late | fenced write refused; the new owner's state stands |

The Prompt 5 churn issue is bounded. After a session becomes `DIRECT_UNAVAILABLE`, a
new recipe is adopted only after `DIRECT_MONITOR_READOPT_COOLDOWN_SECONDS` (default
300). Discovery evidence is bounded per session to the newest
`DIRECT_MONITOR_DISCOVERY_RETENTION` artifacts (default 5; 0 keeps all). This retention
applies to sensitive evidence and is not a repair.

## Secret-leak tests

Recognizable fake secrets are seeded into cookies, `Set-Cookie`, `Authorization`, a
CSRF header, the status URL, the request body, the response body, `localStorage`, and
an HTTP exception message. They are asserted absent from:

- structured logs (as `JsonLogFormatter` emits them, including third-party loggers);
- Prometheus exposition;
- the `/status` text summary;
- SQLite files;
- the consistency report;
- handler-metric and record `repr`;
- operator action messages (a monitor raising an exception that quotes secrets
  shows only `Refresh failed`).

The same secrets are asserted present in the protected store, so the test proves
they are contained, not missing. The full application workflow seeds a simulator
secret into the visitor cookie, the page-built status URL, the status JSON, and a
status `Set-Cookie`. It then asserts the secret is absent from the dashboard, summary,
sessions partial, `/metrics`, SQLite, every captured structured log line, and the
aggregate result report.

## Validation (local only; not Queue-it evidence)

- `tests/unit/test_direct_security.py`: 33 passed. `tests/unit/test_direct_monitor.py`:
  58 passed (corrupt and foreign records are now report-only).
- Direct application workflow (`queue-load-test-phase8-direct-runtime`): Chrome
  (pytest) and Patchright (CLI, `docs/results/phase8_direct_runtime_result.json`)
  both passed every check, including the new metrics, aggregate-dashboard,
  secret-absence, retention, and protected-containment checks.
- Headed Window workflow regression: `tests/integration/test_phase5_workflow.py`
  (Chrome, Camoufox, Patchright extended), run in the full suite.
- Full non-staging suite: **695 passed, 4 staging deselected**. Ruff: **PASS**. Strict
  mypy: **PASS** (101 source files).

## Remaining limits

- Replay-refreshed cookies still never flow back into browser `storage_state`
  (Queue-it cookie rotation is UNKNOWN).
- The schema review workflow is still procedural (documented, not enforced by
  tooling).
- No per-session direct observability by design; per-session investigation uses the
  protected store and the report-only audit.
- Queue-it staging: **NOT RUN / UNKNOWN**.

## Next task

**Phase 8 Prompt 7 — Direct vs Headed Monitoring Benchmark and Authorised Staging
Validation.**
