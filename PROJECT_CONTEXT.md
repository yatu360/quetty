# Project Context

## Project

This repository is an authorised Queue-it staging test system. It creates independent
browser visitors, persists their Queue-it identities and progress, parks them without
keeping browsers open, and restores a bounded subset in installed Google Chrome for
live monitoring.

## Current Status

- Current phase: Phase 3 — 1,000 Sessions, Prompts 1–7 complete locally.
- Last completed work: Phase 3 Prompt 7 — synthetic recovery, repeated restart, and
  expired-lease resumption across 1,000 persisted sessions.
- Completion: local configuration, bounded-concurrency, SQLite query/lease/scheduler,
  and short synthetic browser-capacity evidence only. No Phase 3 Queue-it staging
  benchmark has run, and Phase 2 measurement gaps remain.
- Phase 2 acceptance remains **3 PASS, 2 FAIL, 15 UNKNOWN**. The missing measurements
  are carried as explicit blockers, not converted into Phase 3 scalability claims.
- Next planned work: **Phase 3 Prompt 8 — Phase 3 Acceptance Report**.
  No real acquisition or monitoring throughput should be inferred from synthetic tests.

Unresolved Phase 1 work is evidence collection, not additional scaling: run the opt-in
10-session harness against the real authorised staging event through its timed states,
capture performance/resource results, and verify the staging theme, transfer behavior,
update cadence, and protected destination. The normal `queue-load-test` entry point
currently validates configuration only; it does not assemble `ApplicationRuntime`
unless a runtime is supplied programmatically.

The final Phase 2 acceptance report is `docs/phase2-acceptance.md`. It records no
measured creation/check throughput, restore rates, CPU/RAM, real browser stability,
one-versus-two-browser comparison, or concurrency saturation point because no Phase 2
result artifacts exist. These values remain `UNKNOWN`; they must not be inferred from
unit tests or harness availability.

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
- Shared-Chrome resource manager with stable process-slot identifiers, least-loaded
  multi-process allocation, and per-slot asynchronous crash recovery:
  `src/queue_load_test/browser/manager.py`.
- Supported transfer-link and Queue ID extraction:
  `src/queue_load_test/transfer/extractor.py`.
- Identity-safe transfer/state restoration:
  `src/queue_load_test/transfer/restoration.py`.
- Bounded target acquisition and creation workers with deficit-aware scheduling,
  restart continuation, duplicate isolation, and aggregate activity/rate metrics:
  `src/queue_load_test/scheduler/creation.py`.
- Adaptive per-session monitoring and bounded due-session scheduling with backlog,
  queue-depth, active-worker, claim, and lease-conflict telemetry:
  `src/queue_load_test/scheduler/monitoring.py`.
- Signal-aware shutdown coordination:
  `src/queue_load_test/runtime.py`.
- Structured logging, Prometheus metrics, and text/HTTP status:
  `src/queue_load_test/metrics/`.
- Sensitive-data-safe Phase 1 acceptance reporting plus explicitly gated Phase 1,
  Phase 2 HYBRID restore, resource/stability, and concurrency-matrix benchmark runners:
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
  progress operations, aggregate recovery summary, eligible due-session count, bounded
  due-session claims, lease release, and close. This is the boundary intended to permit
  a future PostgreSQL implementation.
- SQLite has separate `queue_sessions` and `queue_progress` tables, a unique nullable
  `queue_id`, and lightweight `worker_id`/`lease_until` fields. The due-session query
  uses a partial ordered expression index on
  `COALESCE(next_check_at, created_at), created_at, session_id` for monitorable states.
  Existing Phase 2 indexes are migrated in place during initialization.
- Successful-ID counting excludes `FAILED` rows. Duplicate non-null Queue IDs raise
  `QueueIdConflictError`.
- `FileSystemStateStore` defaults to `.browser-state/<session_id>.json`, validates safe
  session IDs, writes a temporary file, flushes/fsyncs it, applies restrictive file
  permissions, and atomically replaces the destination. Save, load, and delete work is
  dispatched with `asyncio.to_thread`, so filesystem operations do not execute directly
  on the event loop.
- `StateConsistencyChecker` performs an explicitly non-mutating database/filesystem
  audit in a worker thread. It reports missing, orphaned, corrupt, duplicate/conflicting,
  and stale temporary state paths; cleanup is never automatic. Its optional recovery
  summary adds missing/corrupt counts to the database aggregates, while normal startup
  deliberately avoids this full directory scan.
- SQLite files, `.browser-state/`, and generated Phase 1 JSON reports are git-ignored.

## Browser Model

The Phase 3 readiness defaults are two Chrome processes, 25 contexts per browser, and
a 50-context global ceiling, with only one creation and one monitoring worker enabled
by default. The controlled local benchmark exercised 2/3/4 Chrome processes at 25
contexts each for 50/75/100 global ceilings. All three short in-memory-page cases
completed with exact per-process accounting, zero recorded failures, and full cleanup.
This is local installed-Chrome evidence, not a safe operating-point or Queue-it staging
claim; the Phase 3 configuration rejects ceilings above 100.
`BrowserManager` launches Chromium with
`channel="chrome"`, selects the least-loaded connected process, and rejects allocations
above either capacity. Browser slot IDs remain stable when only a failed Chrome process
is replaced. The manager reports per-slot and aggregate capacity, and a context exposes
the ID of its owning browser slot for diagnostics.

A fresh context is created without storage state; state is supplied only for explicit
restoration. `OwnedBrowserContext`, the manager's async context manager, idempotent
close, and shutdown paths ensure tracked contexts are released. Capacity calls detect
disconnected browsers; restart immediately removes and invalidates lost contexts,
relaunches only the failed slot, preserves persisted session identity outside the
browser, and updates metrics. Relaunch runs outside the global manager lock, so a
healthy process can continue accepting contexts while another process restarts.

## Target Acquisition Model

`SessionCreationController` reads the successful unique Queue ID count from the
repository on startup and schedules at most `min(CREATION_WORKERS, remaining target)`
work items. Its `asyncio.Queue` is bounded and the worker-task count is fixed; it never
creates one task per requested or persisted session. Successful persisted outcomes
advance a local count; SQLite is queried authoritatively at startup and before target
completion rather than after every one of 1,000 outcomes. Concurrency contracts as the
target approaches. Existing persisted successes support restart continuation, while
failed and duplicate records do not count.

The controller records completed work items and retries separately from creation
attempts and failure outcomes. A graceful stop ceases replenishment and drains only the
already-issued bounded work. Forced cancellation cancels and joins the fixed workers
instead of trying to enqueue shutdown sentinels into a potentially full queue. Sessions
committed before cancellation remain in SQLite and are counted on restart. Synthetic
tests cover a 613-to-1,000 resume, shutdown after five completions followed by resume,
an already-satisfied 1,000 target, and mixed duplicates/failures without overshoot.

`QueueSessionCreator` always obtains a fresh context through `BrowserManager`, follows
the configured staging URL, extracts page-exposed transfer identity and live progress,
saves HYBRID state, persists, and releases the context. SQLite's unique nullable
`queue_id` constraint remains authoritative. A duplicate deletes newly saved state,
creates an explicit `FAILED` attempt without a Queue ID, and leaves the existing
session unchanged.

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
claim; it does not emulate PostgreSQL distributed locking. The scheduler performs one
due-backlog count per tick, limits each claim by both free queue slots and configured
batch size, and sleeps for the configured tick when idle. Its task count is fixed by
`MONITOR_WORKERS`, independent of whether 1 or 1,000 sessions are persisted.
Queued/active session IDs are tracked in a bounded local ownership set. If a slow local
check outlives its lease and the same scheduler reclaims it, the claim renews ownership
without enqueueing a simultaneous duplicate check.

The local Phase 3 synthetic benchmark seeded 1,000 mixed rows with 600 eligible due
sessions. `EXPLAIN QUERY PLAN` used `idx_queue_sessions_due` without a temporary sort.
For a 50-row batch, measured p50 latencies were 0.098 ms due count, 0.555 ms claim,
0.365 ms update, 0.279 ms lease release, and 0.668 ms scheduler iteration. Two SQLite
connections made disjoint concurrent claims. This supports retaining SQLite for the
current local scale; it is not a browser, staging, or multi-host result.

The Phase 3 synthetic sweep benchmark separately made all 1,000 sessions due and ran
the actual bounded scheduler, SQLite updates, and lease releases with 20 fixed workers,
a 50-item queue, and 50-row claims. Two sweeps completed in 1.0582 and 1.0557 seconds
(944.98 and 947.26 synthetic checks/s), with p95 handler durations of 14.64 and 14.33
ms. Each backlog drained from 1,000 to zero, queue depth peaked at 50, worker activity
peaked at 20, and lease conflicts remained zero. A jittered pass processed all 1,000
sessions across a ten-second simulated due window and drained every checkpoint. These
are scheduler/SQLite measurements using a synthetic handler, not Queue-it check rates.

`QueueSessionMonitor` restores one leased session, evaluates live status, verifies
identity, persists progress and timestamps, refreshes HYBRID state when appropriate,
computes `next_check_at`, and lets the scheduler release the lease. Defaults are:
PRE_QUEUE 60–300 seconds, early ACTIVE_QUEUE 60–120, mid ACTIVE_QUEUE 30–60,
SERVICED_SOON 10–30, and TURN_STARTED/READY immediate. Jitter is applied. A static
progress value is not automatically failure; `last_updated_at` is tracked separately
and is stale only when Queue-it supplied a timestamp older than the configured limit.
Unexpected worker errors re-park the session with a configured delay before releasing
the owned lease. Shutdown drains within its timeout, then cancels active workers and
releases active/queued leases without deleting persisted identities.

`MonitoringMetrics.checked` counts every worker-handled session, including exception
paths, separately from successful completed outcomes. Tests cover bounded batches and
queues, worker backpressure, repeated sweeps, transient restoration retry, expired
lease recovery, terminal-state exclusion, and statistical jitter spread across 1,000
generated intervals.

## Failure Handling

- Creation and monitoring use bounded exponential backoff plus jitter for transient
  navigation, HTTP, connection, context, and browser failures.
- Expired/event-closed sessions become `EXPIRED`; missing identity, invalid transfer,
  corrupt state, and unresolvable identity mismatch are permanent failures and do not
  retry forever.
- HYBRID restoration tries transfer first, then storage state for recoverable transfer
  failure. Missing/corrupt state, context failure, refresh failure, and transfer failure
  are explicit structured results.
- Controlled benchmark probes can force exactly one transfer or storage-state method
  without refreshing the stored browser state. Storage-only probes load a context with
  `storage_state` and navigate to the configured staging destination; normal HYBRID
  restoration retains transfer-first/fallback and state-refresh behavior.
- Identity mismatch is observable, increments metrics, preserves the expected Queue ID,
  and can become `FAILED`.
- Browser disconnect recovery replaces only the failed process while the persisted
  identity remains in SQLite. A healthy process remains allocatable during that
  restart; monitor retry policy can retry through healthy capacity.
- SIGINT/SIGTERM stop producers, drain in-flight work within a timeout, release queued
  leases, close contexts/Chrome, and close SQLite. Persisted journeys are not deleted.
- Runtime startup uses one aggregate SQLite query for persisted totals, valid Queue IDs,
  active/expired leases, due work, retry candidates, terminal sessions, and lifecycle
  counts. It no longer loads every session row into Python to initialize gauges.

## Observability

- `JsonLogFormatter` emits timestamp, level, logger, message, and approved contextual
  fields (`session_id`, `queue_id`, status, worker/browser, attempt, restore method,
  duration, and error type). Transfer URLs are not approved log fields.
- `PrometheusMetrics` implements aggregate creation, lifecycle, browser, restore,
  identity, navigation, check, duration, and progress metrics without Queue ID or
  session ID labels. Creation telemetry includes attempts, acquired IDs, duplicates,
  transient/permanent failures, active workers, bounded queue depth, duration, and the
  current-run acquisition rate.
- Monitoring telemetry includes aggregate checks/rate/duration, active fixed workers,
  bounded queue depth, due unleased backlog, claimed sessions, and lease conflicts.
  Restore failures and identity mismatches remain separately counted.
- `ObservabilityHttpServer` serves `/status` and `/metrics`; `StatusSummary` also renders
  terminal-readable text.
- The Phase 1 harness records creation/context/navigation/restore/monitor latency,
  transfer and storage restore success rates, controller CPU/RAM when available,
  browser crashes, navigation failures, and identity mismatches. Generated reports omit
  Queue IDs and transfer URLs.
- The Phase 2 restore benchmark records per-invocation expected/observed Queue IDs,
  method, timestamp, duration, identity outcome, lifecycle status, sanitized error, and
  browser/context failure, then aggregates success/mismatch rates, p50/p95, mechanism
  reliability, fallback use, and errors. It never records transfer URLs or browser
  state. Its JSON is sensitive because Queue IDs are retained for identity auditing.
- The Phase 2 resource harness records fixed-interval application/Chrome CPU and RAM
  when optional `psutil` is installed, managed/observed process counts, active contexts,
  optional file descriptors, bounded queue/backlog depths, aggregate failures,
  throughput, and average/p50/p95 creation/context/check/restore latency. Its JSON has
  no session IDs, Queue IDs, transfer URLs, or browser state. Aggregate Prometheus
  counters now explicitly include BrowserContext creation and navigation failures.
- The Phase 2 tuning harness runs isolated cases sequentially from a generated matrix
  or explicit JSON manifest. It combines resource runs with optional explicit transfer
  and storage-state probes, emits per-case data and concise comparison rows, and flags
  configurable saturation symptoms without ranking cases or choosing a winner.
  BrowserManager context-acquisition and page-navigation durations are aggregate
  histograms without session labels.
- The Phase 3 browser-capacity harness adds separate context-creation and manager-lock
  wait timings, cleanup failure accounting, raw fixed-interval application/Chrome
  CPU/RSS samples, managed and observed process counts, machine-readable JSON, and a
  concise comparison. CPU is aggregate core-percent; summed Chrome RSS can double-count
  shared pages and is treated as a pressure indicator rather than unique memory.
- The Phase 3 acquisition harness reports initial/final unique counts, attempts,
  duplicates, retry/failure classifications, throughput, p50/p95 latency, navigation
  and browser failures, context peak, bounded queue depth, and sampled CPU/RSS without
  emitting session identities or transfer data. It requires the 50-context/two-process
  candidate and two explicit staging gates. The authorised run is NOT RUN.
- The Phase 3 monitoring harness emits repeated all-due sweep results, check latency,
  checks/s, backlog, bounded queue/worker peaks, lease conflicts, staggered checkpoints,
  and application CPU/RAM. Browser, restore, identity, and navigation fields remain
  nullable/UNKNOWN because its local handler deliberately performs no browser work.

## Tests

- Normal suite: `python -m pytest`. Pytest configuration excludes `staging` by default
  but includes unit tests and local Chrome integration tests.
- Unit-only: `python -m pytest tests/unit`.
- Integration-only: `python -m pytest tests/integration`.
- Staging: set `RUN_STAGING_TESTS=1`, configure the authorised environment, then run
  `python -m pytest -o addopts="" -m staging tests/staging`.
- Marker: `staging` means an opt-in test that sends browser traffic to an authorised
  staging environment. The test also has a runtime environment-variable gate.
- Latest result on 2026-09-26: `.venv/bin/pytest -q` reported **269 passed, 4 deselected
  in 18.20 seconds**. The deselected tests were explicitly gated staging harnesses.
  Phase 3 Prompt 5 focused monitoring/repository/benchmark tests reported 42 passed;
  Ruff and strict mypy passed.

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

## Phase 2 Acceptance Results

Phase 2 acceptance is **PARTIAL**: 3 PASS, 2 FAIL, and 15 UNKNOWN. The complete matrix
and evidence notes are in `docs/phase2-acceptance.md`.

- **PASS:** configured creation/context capacity stays bounded; 100 persisted sessions
  can remain parked without 100 live contexts; the scheduler uses fixed workers and a
  bounded queue/claim size.
- **FAIL:** blockers remain before Phase 3, and readiness to test 1,000 sessions is not
  established.
- **UNKNOWN:** real 100-ID acquisition reliability; creation/check throughput and
  latency; backlog control; transfer, storage-state, and fallback reliability; identity
  mismatch incidence; CPU/RAM; real Chrome/context/navigation failures; one-versus-two
  browser performance; and concurrency saturation.

Verified local 100-row evidence includes an exact scripted 100-ID acquisition with at
most 10 concurrent workers, a 90-to-100 restart continuation, a 99-to-100 deficit case,
a 25-item claim from 80 due rows leaving a visible backlog of 55, and a blocked
two-worker/two-queue case owning four rows while 96 remained due. These validate
mechanics only; they are not Queue-it staging or performance measurements.

## Known Issues / Unknowns

- No real authorised staging run or generated performance JSON is checked in; all
  vendor-, theme-, event-timing-, and destination-specific acceptance remains unknown.
- Real transfer and storage-state restore rates, `lastUpdated` cadence, lifecycle timing,
  Queue-it CPU/RAM headroom, browser crash rate, and monitoring sweep time have not been
  measured.
- The Phase 2 HYBRID restore benchmark is implemented but **NOT RUN**. Both staging
  environment gates were absent, so transfer/storage success rates, fallback frequency,
  latency percentiles, error distribution, and identity mismatches remain UNKNOWN.
- The Phase 2 resource/stability harness is implemented but **NOT RUN**. No verified
  application/Chrome CPU or RAM, throughput, operation latency, file-descriptor trend,
  failure rate, or one-browser versus two-browser comparison exists yet.
- The Phase 2 concurrency matrix is implemented but **NOT RUN**. All measured
  comparisons and saturation flags remain UNKNOWN; no operating point has been chosen.
- Process-tree CPU/RAM requires the optional `benchmark` dependency. Open file
  descriptors remain `null` on platforms where `psutil` does not expose `num_fds`;
  missing optional system metrics never abort a run.
- The normal `queue-load-test` CLI validates settings and exits unless code injects an
  assembled `ApplicationRuntime`. The Phase 1 acceptance CLI is fully assembled for its
  narrower controlled purpose.
- The 100-session configuration and bounded controller/scheduler behavior are covered by
  tests. Acquisition correctness is verified for targets 1, 10, and 100, including a
  restart from 90 persisted IDs and 99/100 near-target scheduling, but no 100-session
  browser/staging run or performance tuning has been performed.
- Installed Chrome completed short local 50/75/100-context cases using 2/3/4 managed
  processes with no recorded failures. Summed peak process RSS crossed the conservative
  80%-of-host indicator at 75 and 100 contexts, although shared Chrome pages can be
  double-counted. Sustained operation, Queue-it navigation, and real crash recovery
  still require authorised staging observation; no optimal or staging-safe value is
  selected.
- The 1,000-target acquisition path is validated synthetically with fixed workers,
  bounded queues, uniqueness conflicts, retries, graceful stop, forced cancellation,
  and restart continuation. The real 1,000-ID staging result is NOT RUN, so creation
  throughput, Queue-it duplicate/failure incidence, acquisition CPU/RAM, and observed
  active-context peak remain UNKNOWN.
- The synthetic monitoring configuration drained repeated synchronized 1,000-session
  backlogs and every staggered cohort without dropping work. Real Queue-it sweep time,
  context wait/peak, restore failures, identity mismatches, browser crashes, navigation
  failures, and Chrome resources remain UNKNOWN because staging monitoring was NOT RUN.
- A dedicated local synthetic run stored 1,000 HYBRID rows in a 417,792-byte SQLite
  database and 1,000 equal-size 1,249-byte state files (1,249,000 bytes total). p95
  save/load/replace latency was 0.263/0.063/0.291 ms; 100 deletes had 0.075 ms p95.
  Baseline and restart scans (63.554/61.355 ms) each reconstructed all 1,000 sessions
  with zero findings or temporary files. The event-loop lag probe observed 0.149 ms p95
  and 3.302 ms maximum while database and file operations ran.
- Synthetic fixed-size JSON is filesystem/mechanics evidence, not observed Queue-it
  `storage_state` sizing. Local-disk behavior under concurrent browser refreshes,
  sustained churn, disk exhaustion, power loss, network filesystems, and 10,000 files
  remains unmeasured.
- The 1,000-session recovery benchmark reopened SQLite five times with 0.827 ms p50 and
  0.907 ms p95 initialize-plus-summary latency. Stable identity fields and 100 terminal
  states remained unchanged. A bounded five-worker/50-item scheduler then recovered the
  exact 50 expired leases, processed 50 checks, left 50 unexpired leases untouched, and
  preserved all 960 valid Queue IDs and state associations.
- Interrupted acquisition/monitoring, browser/context failure, transfer/state failure,
  missing/corrupt state, identity mismatch, and partial-target continuation pass with
  controlled local doubles. Their real Queue-it/Chrome recovery outcomes remain UNKNOWN
  because no authorised staging restart was run.
- Target coordination is intentionally single-controller. If multiple independent
  application processes acquire different valid IDs concurrently, aggregate overshoot
  is not reserved transactionally; distributed target coordination is out of scope.
- Synthetic 100-session tests verify a fixed two-worker/two-queue live set and a visible
  96-session due backlog under blocked workers. Real restore/check latency, sustainable
  checks per second, polling sweep time, and backlog drain rate remain unmeasured.
- PostgreSQL, distributed workers, and shared/object state storage are not implemented.
- No failing ordinary tests or source TODO/FIXME markers were found during this handoff.
- Phase 2 acceptance is documented in `docs/phase2-acceptance.md`; the missing staging
  evidence is a blocker to selecting the Phase 3 50–100-context operating range.
- Phase 3 scale audit: `/status` and runtime startup load all sessions to calculate
  status gauges; the Phase 2 restore harness also loads its full population. At 1,000
  rows these paths need measurement and may need aggregate/paginated repository APIs.
- SQLite serializes each repository instance through one async lock and uses one commit
  per create/update/lease release. The scheduler still runs a due count followed by a
  bounded `BEGIN IMMEDIATE` claim when queue space exists. At 1,000 synthetic rows the
  indexed query and transaction costs were sub-millisecond at p50, but one-writer
  behavior and per-session commits remain later-scale risks.
- `BrowserManager` holds its global lock while `browser.new_context()` runs, which may
  serialize allocation at 50–100 workers. Measure before redesigning reservations.
- Every creation/monitor/restore operation can emit a structured per-session event.
  Logging volume and sink backpressure are unmeasured at 1,000 sessions, although
  Prometheus labels remain aggregate/low-cardinality.
- HYBRID state saves are correctly offloaded to threads but perform an fsync and atomic
  replacement per session. Thread-pool and disk pressure require later measurement;
  no directory-wide scan occurs in the hot path.

## Important Files

- `README.md` — setup, scope, and harness usage.
- `.env.example` — Phase 3 readiness defaults and bounded queue/capacity settings.
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
- `docs/phase2-restore-benchmark.md` — Phase 2 benchmark scope and current NOT RUN result.
- `docs/phase2-resource-benchmark.md` — resource harness procedure and NOT RUN result.
- `docs/phase2-concurrency-benchmark.md` — tuning procedure and NOT RUN result.
- `docs/phase2-acceptance.md` — final Phase 2 PASS/FAIL/UNKNOWN decision and evidence.
- `docs/phase3-repository-benchmark.md` — synthetic population, query plan, latency,
  and SQLite decision for Phase 3 Prompt 2.
- `docs/phase3-browser-capacity-benchmark.md` — local installed-Chrome comparison,
  resource caveats, and staging NOT RUN status for Phase 3 Prompt 3.
- `docs/phase3-acquisition-benchmark.md` — bounded 1,000-target procedure, aggregate
  report fields, restart behavior, and staging NOT RUN status for Phase 3 Prompt 4.
- `docs/phase3-monitoring-benchmark.md` — full-sweep definition, repeated/staggered
  synthetic results, resource observations, and Queue-it UNKNOWN fields for Prompt 5.
- `docs/phase3-storage-benchmark.md` — 1,000-file footprint and latency, consistency
  findings, restart evidence, and local-storage decision for Prompt 6.
- `docs/phase3-recovery.md` — 1,000-session repeated restart, lease recovery, identity
  preservation, scenario PASS/UNKNOWN results, and Prompt 7 limitations.
- `benchmarks/phase2-concurrency-matrix.example.json` — explicit repeatable ten-case matrix.
- `tests/integration/test_phase1_controlled_run.py` — deterministic 10-session run.
- `tests/staging/test_phase1_staging.py` — gated real-staging entry.
- `PHASE_PLAN.md` and `CHANGELOG_AI.md` — roadmap and AI-session history.

## Commands

Install/setup in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
# Add the optional process-tree sampler for resource benchmark runs:
python -m pip install -e ".[test,benchmark]"
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
$env:TARGET_QUEUE_IDS = "10"
$env:CHROME_PROCESS_COUNT = "1"
$env:MAX_CONTEXTS_PER_BROWSER = "5"
$env:MAX_ACTIVE_CONTEXTS = "5"
$env:PHASE1_OBSERVE_SECONDS = "600"
python -m pytest -o addopts="" -m staging tests/staging
```

Phase 1 acceptance run using an empty dedicated SQLite database/state directory:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:TARGET_QUEUE_IDS = "10"
$env:CHROME_PROCESS_COUNT = "1"
$env:MAX_CONTEXTS_PER_BROWSER = "5"
$env:MAX_ACTIVE_CONTEXTS = "5"
queue-load-test-phase1 --confirm-authorized-staging --observe-seconds 600 --report phase1-acceptance.json
```

Phase 2 restore benchmark using an existing authorised 100-session HYBRID database:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_RESTORE_BENCHMARK = "1"
queue-load-test-phase2-restore --confirm-authorized-staging --sample-size 100 --mode all --report phase2-restore-benchmark.json
```

Phase 2 resource benchmark using a dedicated empty SQLite database/state directory:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_RESOURCE_BENCHMARK = "1"
$env:TARGET_QUEUE_IDS = "100"
$env:SESSION_MODE = "HYBRID"
$env:MAX_ACTIVE_CONTEXTS = "25"
$env:CHROME_PROCESS_COUNT = "1" # repeat with 2
$env:MAX_CONTEXTS_PER_BROWSER = "25" # use 13 with two Chrome processes
queue-load-test-phase2-resources --confirm-authorized-staging --monitoring-seconds 600 --sample-interval-seconds 5 --report phase2-resource-benchmark.json
```

Phase 2 concurrency matrix using fresh per-case SQLite/state locations:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE2_CONCURRENCY_BENCHMARK = "1"
queue-load-test-phase2-tuning --confirm-authorized-staging --matrix-file benchmarks/phase2-concurrency-matrix.example.json --monitoring-seconds 600 --restore-sample-size 10 --report phase2-concurrency-benchmark.json
```

Local synthetic Phase 3 repository benchmark:

```powershell
queue-load-test-phase3-repository --sessions 1000 --batch-size 50 --samples 20
```

Local installed-Chrome Phase 3 browser-capacity benchmark:

```powershell
queue-load-test-phase3-browser-capacity --hold-seconds 2 --sample-interval-seconds 0.25 --report phase3-browser-capacity-benchmark.json
```

Authorised Phase 3 acquisition/resume benchmark:

```powershell
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE3_ACQUISITION_BENCHMARK = "1"
queue-load-test-phase3-acquisition --confirm-authorized-staging --report phase3-acquisition-benchmark.json
```

Local synthetic Phase 3 monitoring sweep:

```powershell
queue-load-test-phase3-monitoring --database phase3-monitoring-synthetic.sqlite3 --report phase3-monitoring-benchmark.json --workers 20 --queue-capacity 50 --batch-size 50 --sweeps 2
```

Local synthetic Phase 3 persistence benchmark:

```powershell
queue-load-test-phase3-storage --database phase3-storage-synthetic/sessions.sqlite3 --state-directory phase3-storage-synthetic/state --sessions 1000 --report phase3-storage-benchmark.json
```

Read-only state consistency report for an existing database:

```powershell
queue-load-test-state-check --database queue_load_test.sqlite3 --state-directory .browser-state --report state-consistency-report.json
```

Local synthetic Phase 3 recovery benchmark:

```powershell
queue-load-test-phase3-recovery --database phase3-recovery-synthetic/sessions.sqlite3 --state-directory phase3-recovery-synthetic/state --restarts 5 --report phase3-recovery-benchmark.json
```

Static checks used by this project:

```powershell
python -m ruff check src tests
python -m mypy src
```

## Next Task

**Phase 3 Prompt 8 — Phase 3 Acceptance Report.** Consolidate configuration,
repository, browser-capacity, acquisition, monitoring, storage, and recovery evidence;
record explicit PASS/FAIL/UNKNOWN gates without treating synthetic results as real
Queue-it scalability proof.

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
