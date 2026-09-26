# Project Context

## Project

This repository is an authorised Queue-it staging test system. It creates independent
browser visitors, persists their Queue-it identities and progress, parks them without
keeping browsers open, and restores a bounded subset in installed Google Chrome for
live monitoring.

## Current Status

- Current phase: Phase 1 — Core Validation.
- Last completed work: Phase 1 Prompt 12 — integration-test and controlled-run harness.
- Completion: Phase 1 implementation is complete through Prompt 12.
- Acceptance: **PARTIAL**. The deterministic local 10-session run is PASS, but no
  authorised real-staging run or generated `phase1-acceptance.json` is present.
- Next planned phase: Phase 2 — 100 Sessions.

Unresolved Phase 1 work is evidence collection, not additional scaling: run the opt-in
10-session harness against the real authorised staging event through its timed states,
capture performance/resource results, and verify the staging theme, transfer behavior,
update cadence, and protected destination. The normal `queue-load-test` entry point
currently validates configuration only; it does not assemble `ApplicationRuntime`
unless a runtime is supplied programmatically.

## Core Objective

Reach configurable `TARGET_QUEUE_IDS=N` with independent Queue-it visitor sessions.
Persist many identities and their progress, keep most sessions parked, and use only a
small bounded pool of live Chrome `BrowserContext` objects. A scheduler claims due
parked sessions, bounded workers restore and inspect them, persist the observation,
close the context, release the lease, and park them again.

## Architecture Invariants

- Python 3.12+, typed Python, and `asyncio`.
- Browser control uses `playwright.async_api` and installed Google Chrome via
  `channel="chrome"`.
- Never launch one Chrome process per visitor. Share each process across isolated
  contexts and enforce per-browser and global limits.
- Queue ID identity and queue lifecycle status are separate concepts.
- A Queue ID may already exist during `PRE_QUEUE`; its existence never implies
  `ACTIVE_QUEUE`.
- Detect `PRE_QUEUE` independently from active-queue progress.
- Inspect the live DOM/page state after JavaScript has executed; do not rely solely on
  initial static HTML.
- Capture only the transfer mechanism exposed by the Queue-it page. Do not manually
  construct undocumented Queue-it URLs or infer private APIs from traffic.
- `HYBRID` persists transfer identity and Playwright `storage_state`; restoration tries
  the supported transfer path first and uses state as fallback where required.
- `TRANSFER_ONLY` persists transfer identity and progress without relying on long-term
  local `storage_state`.
- Never silently replace an expected Queue ID after an identity mismatch.
- Use bounded queues, fixed worker pools, leases, and explicit context ownership. Do not
  create a task, browser, or live context per persisted visitor.
- Do not add stealth, fingerprint spoofing, automation hiding, or anti-bot evasion.
- Use only an authorised staging environment.
- Treat transfer URLs and browser-state files as sensitive session data. Do not include
  them in normal INFO logs, reports, or object representations.

## Current Architecture

- Typed environment configuration and validation:
  `src/queue_load_test/config.py`.
- Session/progress dataclasses, enums, lifecycle evaluation, and transition validation:
  `src/queue_load_test/models/session.py`, `progress.py`, and `lifecycle.py`.
- Defensive pure value parsing and live DOM extraction:
  `src/queue_load_test/queue_monitor/parsing.py` and `extractor.py`.
- Protected-destination admission and explicit terminal-page detection:
  `src/queue_load_test/queue_monitor/admission.py`.
- Repository boundary and SQLite implementation:
  `src/queue_load_test/repository/base.py` and `sqlite.py`.
- State-store protocol and atomic filesystem implementation:
  `src/queue_load_test/state/base.py` and `filesystem.py`.
- Shared-Chrome resource manager:
  `src/queue_load_test/browser/manager.py`.
- Supported transfer-link and Queue ID extraction:
  `src/queue_load_test/transfer/extractor.py`.
- Identity-safe transfer/state restoration:
  `src/queue_load_test/transfer/restoration.py`.
- Bounded target acquisition and creation workers:
  `src/queue_load_test/scheduler/creation.py`.
- Adaptive per-session monitoring and bounded due-session scheduling:
  `src/queue_load_test/scheduler/monitoring.py`.
- Signal-aware shutdown coordination:
  `src/queue_load_test/runtime.py`.
- Structured logging, Prometheus metrics, and text/HTTP status:
  `src/queue_load_test/metrics/`.
- Sensitive-data-safe acceptance report and explicitly gated staging runner:
  `src/queue_load_test/harness/`.
- Local controlled Chrome run and opt-in staging test:
  `tests/integration/test_phase1_controlled_run.py` and
  `tests/staging/test_phase1_staging.py`.

## Current Data Model

`QueueSession` holds stable identity, persistence, scheduling, and ownership data:
`session_id`, optional `queue_id`, sensitive `transfer_url`, `mode`, `status`, sensitive
`state_path`, `created_at`, `last_checked_at`, `last_queue_update`,
`last_progress_change_at`, `next_check_at`, `attempt_count`, `last_error`, `worker_id`,
and `lease_until`.

`QueueProgress` holds optional layout-dependent observations: `queue_number`,
`users_ahead`, `progress_percentage`, `estimated_wait_text`, `expected_service_time`,
`last_updated_at`, `queue_paused`, `first_in_line`, `serviced_soon`, `turn_started`,
`connection_lost`, `pre_queue`, `active_queue`, `manual_update_warning`, and compact
extraction diagnostics. All Queue-it layout fields can be `None`.

Modes are `HYBRID` and `TRANSFER_ONLY`. Statuses are `NEW`, `CREATING`, `PRE_QUEUE`,
`ACTIVE_QUEUE`, `PARKED`, `CHECKING`, `PAUSED`, `SERVICED_SOON`, `TURN_STARTED`,
`READY`, `ADMITTED`, `CONNECTION_LOST`, `EXPIRED`, and `FAILED`.

## Persistence

- `SQLiteSessionRepository` uses the path from `DATABASE_URL`; the default is
  `sqlite:///queue_load_test.sqlite3`.
- The repository protocol exposes create, update, get, list, successful-ID count,
  progress operations, due-session claims, lease release, and close. This is the
  boundary intended to permit a future PostgreSQL implementation.
- SQLite has separate `queue_sessions` and `queue_progress` tables, a unique nullable
  `queue_id`, a due-session index, and lightweight `worker_id`/`lease_until` fields.
- Successful-ID counting excludes `FAILED` rows. Duplicate non-null Queue IDs raise
  `QueueIdConflictError`.
- `FileSystemStateStore` defaults to `.browser-state/<session_id>.json`, validates safe
  session IDs, writes a temporary file, flushes/fsyncs it, applies restrictive file
  permissions, and atomically replaces the destination.
- SQLite files, `.browser-state/`, and generated Phase 1 JSON reports are git-ignored.

## Browser Model

Phase 1 defaults to one Chrome process, five contexts per browser, and five total active
contexts. `BrowserManager` launches Chromium with `channel="chrome"`, selects the
least-loaded connected process, and rejects allocations above either capacity.

A fresh context is created without storage state; state is supplied only for explicit
restoration. `OwnedBrowserContext`, the manager's async context manager, idempotent
close, and shutdown paths ensure tracked contexts are released. Capacity calls detect
disconnected browsers; restart closes/invalidates lost contexts, relaunches the failed
slot, preserves persisted session identity outside the browser, and updates metrics.

## Session Lifecycle

The principal lifecycle is:

`NEW → CREATING → PRE_QUEUE → ACTIVE_QUEUE → SERVICED_SOON → TURN_STARTED → READY → ADMITTED`

Supported side/interruption states are `PARKED`, `CHECKING`, `PAUSED`,
`CONNECTION_LOST`, `EXPIRED`, and `FAILED`. Repeated observations are idempotent.
`ADMITTED` and `EXPIRED` are terminal; `FAILED` can return to `CREATING`. Explicit
transition validation rejects suspicious backward transitions. State evaluation gives
priority to connection loss, expiry, admission, turn started, first-in-line/ready,
serviced soon, paused, pre-queue, and active-queue evidence. Progress percentage alone
does not establish lifecycle state.

## Queue-it Extraction

The live extractor supports Queue-it selectors for progress, queue number, users ahead,
expected service time, estimated wait, last update, paused, first-in-line,
serviced-soon, turn-started, and manual connection warnings. It also supports stable
staging `data-testid` selectors. PRE_QUEUE evidence includes the configured test ID,
body class containing `before`, safe primitive `isBeforeOrIdle`, `secondsToStart`, and
`eventStartTime` signals. Optional missing, blank, hidden, malformed, or failing fields
do not abort the whole observation; diagnostics retain selector/error names, not HTML.

Transfer extraction checks multiple official-control selectors, including known
Queue-it IDs, configured staging test IDs, and the visible “Continue my journey on
another browser or device” link. It reads the exact `href`, `value`, or supported data
attribute, validates scheme/origin/path against the expected journey, extracts the `q`
identity when present, detects ambiguous IDs, and reports expected/observed identity
mismatch without replacing the expected ID.

## Monitoring Model

`ParkedSessionScheduler` selects only due, unleased, non-terminal sessions through the
repository, claims at most the free space in a bounded `asyncio.Queue`, and feeds a
fixed worker pool. SQLite uses a short `BEGIN IMMEDIATE` transaction for the local
claim; it does not emulate PostgreSQL distributed locking.

`QueueSessionMonitor` restores one leased session, evaluates live status, verifies
identity, persists progress and timestamps, refreshes HYBRID state when appropriate,
computes `next_check_at`, and lets the scheduler release the lease. Defaults are:
PRE_QUEUE 60–300 seconds, early ACTIVE_QUEUE 60–120, mid ACTIVE_QUEUE 30–60,
SERVICED_SOON 10–30, and TURN_STARTED/READY immediate. Jitter is applied. A static
progress value is not automatically failure; `last_updated_at` is tracked separately
and is stale only when Queue-it supplied a timestamp older than the configured limit.

## Failure Handling

- Creation and monitoring use bounded exponential backoff plus jitter for transient
  navigation, HTTP, connection, context, and browser failures.
- Expired/event-closed sessions become `EXPIRED`; missing identity, invalid transfer,
  corrupt state, and unresolvable identity mismatch are permanent failures and do not
  retry forever.
- HYBRID restoration tries transfer first, then storage state for recoverable transfer
  failure. Missing/corrupt state, context failure, refresh failure, and transfer failure
  are explicit structured results.
- Identity mismatch is observable, increments metrics, preserves the expected Queue ID,
  and can become `FAILED`.
- Browser disconnect recovery replaces the failed process while the persisted identity
  remains in SQLite; monitor retry policy can retry through healthy capacity.
- SIGINT/SIGTERM stop producers, drain in-flight work within a timeout, release queued
  leases, close contexts/Chrome, and close SQLite. Persisted journeys are not deleted.

## Observability

- `JsonLogFormatter` emits timestamp, level, logger, message, and approved contextual
  fields (`session_id`, `queue_id`, status, worker/browser, attempt, restore method,
  duration, and error type). Transfer URLs are not approved log fields.
- `PrometheusMetrics` implements aggregate creation, lifecycle, browser, restore,
  identity, navigation, check, duration, and progress metrics without Queue ID or
  session ID labels.
- `ObservabilityHttpServer` serves `/status` and `/metrics`; `StatusSummary` also renders
  terminal-readable text.
- The Phase 1 harness records creation/context/navigation/restore/monitor latency,
  transfer and storage restore success rates, controller CPU/RAM when available,
  browser crashes, navigation failures, and identity mismatches. Generated reports omit
  Queue IDs and transfer URLs.

## Tests

- Normal suite: `python -m pytest`. Pytest configuration excludes `staging` by default
  but includes unit tests and local Chrome integration tests.
- Unit-only: `python -m pytest tests/unit`.
- Integration-only: `python -m pytest tests/integration`.
- Staging: set `RUN_STAGING_TESTS=1`, configure the authorised environment, then run
  `python -m pytest -o addopts="" -m staging tests/staging`.
- Marker: `staging` means an opt-in test that sends browser traffic to an authorised
  staging environment. The test also has a runtime environment-variable gate.
- Latest locally verified result on 2026-09-26: `python -m pytest -q` reported
  **177 passed, 1 deselected**. The deselected test was the staging test.

## Phase 1 Acceptance Results

No authorised real-staging acceptance run is recorded. Per the acceptance rule, all
real-staging questions remain `UNKNOWN`; the checked-in local controlled report records
PASS for the corresponding mechanisms.

| # | Critical question | Real staging | Controlled local evidence |
|---:|---|:---:|:---:|
| 1 | Independent Queue IDs per fresh context | UNKNOWN | PASS |
| 2 | Queue ID availability during PRE_QUEUE | UNKNOWN | PASS |
| 3 | PRE_QUEUE → ACTIVE_QUEUE identity continuity | UNKNOWN | PASS |
| 4 | Official transfer URL extraction | UNKNOWN | PASS for page-exposed simulator control |
| 5 | Transfer restore same journey | UNKNOWN | PASS |
| 6 | `storage_state` restore same journey | UNKNOWN | PASS |
| 7 | Progress extraction | UNKNOWN | PASS |
| 8 | `lastUpdated` health signal | UNKNOWN | PASS |
| 9 | SERVICED_SOON detection | UNKNOWN | PASS |
| 10 | TURN_STARTED detection | UNKNOWN | PASS |
| 11 | ADMITTED detection | UNKNOWN | PASS |
| 12 | Restart/recovery persistence | UNKNOWN | PASS |

## Known Issues / Unknowns

- No real authorised staging run or generated performance JSON is checked in; all
  vendor-, theme-, event-timing-, and destination-specific acceptance remains unknown.
- Real transfer and storage-state restore rates, `lastUpdated` cadence, lifecycle timing,
  CPU/RAM headroom, browser crash rate, and monitoring sweep time have not been measured.
- The normal `queue-load-test` CLI validates settings and exits unless code injects an
  assembled `ApplicationRuntime`. The Phase 1 acceptance CLI is fully assembled for its
  narrower controlled purpose.
- PostgreSQL, distributed workers, shared/object state storage, and Phase 2 scaling are
  not implemented.
- No failing ordinary tests or source TODO/FIXME markers were found during this handoff.

## Important Files

- `README.md` — setup, scope, and harness usage.
- `.env.example` — complete Phase 1 environment defaults.
- `pyproject.toml` — dependencies, scripts, pytest marker/default exclusion.
- `src/queue_load_test/config.py` — configuration and contradiction validation.
- `src/queue_load_test/models/` — identity/progress/lifecycle domain.
- `src/queue_load_test/browser/manager.py` — Chrome/context ownership and recovery.
- `src/queue_load_test/repository/sqlite.py` — persistence and leases.
- `src/queue_load_test/state/filesystem.py` — atomic JSON storage state.
- `src/queue_load_test/scheduler/creation.py` — bounded acquisition.
- `src/queue_load_test/scheduler/monitoring.py` — bounded monitoring scheduler.
- `src/queue_load_test/transfer/` — transfer capture and restoration.
- `src/queue_load_test/queue_monitor/` — parsing, DOM extraction, admission.
- `src/queue_load_test/metrics/` — structured logging, metrics, status server.
- `src/queue_load_test/harness/` — acceptance recorder and staging runner.
- `docs/phase1-acceptance-report.md` — controlled Phase 1 report and assumptions.
- `tests/integration/test_phase1_controlled_run.py` — deterministic 10-session run.
- `tests/staging/test_phase1_staging.py` — gated real-staging entry.
- `PHASE_PLAN.md` and `CHANGELOG_AI.md` — roadmap and AI-session history.

## Commands

Install/setup in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m playwright install chrome
Copy-Item .env.example .env
```

Validate/run the current application entry point:

```powershell
queue-load-test
```

Tests:

```powershell
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest
```

Authorised staging test:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:PHASE1_OBSERVE_SECONDS = "600"
python -m pytest -o addopts="" -m staging tests/staging
```

Phase 1 acceptance run using an empty dedicated SQLite database/state directory:

```powershell
$env:RUN_STAGING_TESTS = "1"
queue-load-test-phase1 --confirm-authorized-staging --observe-seconds 600 --report phase1-acceptance.json
```

Static checks used by this project:

```powershell
python -m ruff check src tests
python -m mypy src
```

## Next Task

Phase 2 begins next.

The next task is:

**Phase 2 Prompt 1 — Phase 2 Configuration and Scaling Readiness**

Do not implement it as part of this handoff.

## Instructions for Future AI Sessions

- Read `PROJECT_CONTEXT.md` first.
- Then inspect `git status` and the recent `git log`.
- Treat the current repository, tests, and latest evidence as source of truth.
- Read `PHASE_PLAN.md` before beginning a new phase.
- Review `CHANGELOG_AI.md` for prior decisions and unresolved evidence.
- Do not redo completed work without evidence that it is broken.
- Do not skip ahead to later prompts or phases.
- Update `PROJECT_CONTEXT.md` and append `CHANGELOG_AI.md` at the end of each prompt.
- Keep abstractions pragmatic and preserve the parked-session architecture.
- Run relevant tests before and after modifications and record failures honestly.
- Never mark staging behavior PASS unless it was actually observed in an authorised
  real-staging run.
