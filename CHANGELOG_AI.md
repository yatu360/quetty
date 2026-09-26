# AI Development Changelog

## How to Use This File

Every Codex, Claude, ChatGPT, or other AI coding session should append an entry after
completing a prompt. Use repository evidence, distinguish observed facts from inference,
and never report a staging PASS without an authorised staging result.

Use this template:

```markdown
## YYYY-MM-DD — Phase X Prompt Y — <Prompt Name>

### Agent / Model
<model if known, otherwise "unknown">

### Goal
<short prompt objective>

### Changes Made
- ...

### Files Added
- ...

### Files Modified
- ...

### Tests Run
- `<command>` — <result>

### Staging Tests
- `<command>` — PASS / FAIL / NOT RUN
- <reason if not run>

### Important Decisions
- ...

### Known Issues
- ...

### Follow-Up
<exact next prompt/task>

### Git State
- Commit: <hash if available>
- Branch: <branch>
- Working tree: <clean/dirty/unknown>
```

## Phase 1 Summary — Prompts 1–12

This is a best-effort reconstruction from Git history, source, tests, README, and the
checked-in acceptance report. The repository does not record the exact agent/model or a
separate commit for every original prompt, so the history below does not invent those
details.

### Agent / Model

unknown (not recorded in Git history)

### Goal

Build and validate the 10-session Phase 1 foundation for an authorised Queue-it staging
test system using one shared installed-Chrome process, at most five active contexts,
SQLite, local JSON browser state, bounded acquisition/monitoring, safe identity
restoration, lifecycle completion, observability, and an opt-in acceptance harness.

### Verified Commit History

All listed Phase 1 commits are on `main` and dated 2026-09-26:

| Commit | Verified scope from commit message/diff |
|---|---|
| `c8fb445` | Typed Phase 1 foundation: configuration, initial models, project layout, tests |
| `6292b21` | Lifecycle parsing/validation, defensive value parsing, SQLite repository, filesystem state store |
| `79d52ad` | Shared-Chrome `BrowserManager`, capacity and cleanup tests |
| `3c5b6de` | Live Queue-it DOM extractor, fixtures, Playwright integration tests |
| `d726730` | Supported transfer control and Queue ID extraction |
| `e450e61` | Bounded Queue ID acquisition and creation workers |
| `f893976` | Identity-safe TRANSFER_ONLY/HYBRID restoration |
| `45f1d53` | Bounded parked-session scheduler, leases, adaptive polling |
| `328b3f5` | Admission/expiry handling, failure recovery, graceful shutdown |
| `b4c6684` | Structured logging, Prometheus metrics, status endpoint |
| `41d70be` | Controlled 10-session integration run and gated staging acceptance harness |

### Changes Made

- Added typed Pydantic settings and validation for Phase 1 capacity, polling, retry,
  SQLite, state paths, and Prometheus port.
- Added `QueueSession`, `QueueProgress`, session modes/statuses, explicit transition
  validation, and deterministic multi-signal lifecycle evaluation.
- Added defensive pure parsing plus live JavaScript-rendered DOM extraction with compact
  diagnostics and optional layout-dependent fields.
- Added `SessionRepository`/`SQLiteSessionRepository`, unique nullable Queue IDs,
  progress persistence, due-session leases, and atomic `FileSystemStateStore` JSON
  writes.
- Added a shared installed-Chrome `BrowserManager` with isolated contexts, hard capacity
  limits, failure detection/restart, ownership cleanup, and graceful shutdown.
- Added supported page-exposed transfer URL extraction, host/path validation, expected
  versus observed identities, and explicit mismatch reporting.
- Added bounded creation workers/controller that continue until the unique-ID target is
  met, excluding failed and duplicate attempts.
- Added TRANSFER_ONLY restore and HYBRID transfer-first/storage-state fallback without
  overwriting persisted identity.
- Added due-session scheduling, fixed monitoring workers, leases, retry/backoff,
  adaptive polling, stale-update tracking, re-parking, and terminal-state handling.
- Added normal-navigation admission detection, Chrome crash recovery, SIGINT/SIGTERM
  shutdown coordination, and restart persistence tests.
- Added structured JSON logs, low-cardinality Prometheus metrics, and `/status` and
  `/metrics` HTTP output.
- Added sensitive-data-safe acceptance reporting, a deterministic local 10-session
  Chrome simulator, an explicitly gated staging runner, and a controlled acceptance
  report.

### Files Added

Verified major additions include:

- `src/queue_load_test/config.py`, `main.py`, and `runtime.py`
- `src/queue_load_test/models/`
- `src/queue_load_test/browser/manager.py`
- `src/queue_load_test/repository/` and `state/`
- `src/queue_load_test/queue_monitor/` and `transfer/`
- `src/queue_load_test/scheduler/`
- `src/queue_load_test/metrics/`
- `src/queue_load_test/harness/`
- `tests/unit/`, `tests/integration/`, `tests/staging/`, and Queue-it HTML fixtures
- `docs/phase1-acceptance-report.md`

### Files Modified

- `README.md`, `.env.example`, `pyproject.toml`, and `.gitignore` evolved with the Phase
  1 implementation.
- Existing modules/tests were incrementally updated by later Phase 1 commits; use the
  commit table above rather than assuming one original prompt maps to exactly one file.

### Tests Run

- Latest handoff verification: `python -m pytest -q` — 177 passed, 1 deselected.
- Latest handoff static checks: `python -m ruff check src tests` — passed.
- Latest handoff type check: `python -m mypy src` — passed.
- Historical per-commit test commands/results are not stored in Git and are therefore
  not reconstructed.

### Staging Tests

- `python -m pytest -o addopts="" -m staging tests/staging` — **NOT RUN**.
- No authorised real-staging URL/run result or generated `phase1-acceptance.json` is
  present. The ordinary suite deselects the staging test by default.

### Important Decisions

- Preserve Queue ID independently from lifecycle status; Queue ID may exist in
  PRE_QUEUE and percentage alone is not lifecycle truth.
- Persist many sessions while keeping only a bounded number of browser contexts live.
- Use only installed Google Chrome through Playwright `channel="chrome"`; no stealth or
  evasion behavior.
- Accept only page-exposed supported transfer values; do not construct undocumented
  Queue-it URLs.
- Keep expected identity immutable across restore failures and mismatches.
- Keep `SessionRepository` and `StateStore` boundaries replaceable without introducing
  PostgreSQL/distributed complexity in Phase 1.
- Treat local controlled acceptance as mechanism evidence, not proof of real Queue-it
  staging behavior.

### Known Issues

- Phase 1 acceptance is PARTIAL: controlled local mechanisms pass, but all real-staging
  questions and performance/resource measurements are UNKNOWN.
- `queue-load-test` validates configuration and exits unless an `ApplicationRuntime` is
  injected; the general runtime is not assembled by the default CLI.
- PostgreSQL, distributed workers, shared state storage, and Phase 2 scaling are not
  implemented.

### Follow-Up

Phase 1 handoff documentation, followed by:
**Phase 2 Prompt 1 — Phase 2 Configuration and Scaling Readiness**

### Git State

- Last Phase 1 implementation commit: `41d70be` (`Add Phase 1 acceptance harness`)
- Branch: `main`
- Working tree: clean before the handoff documentation files were created

## 2026-09-26 — Phase 1 Handoff Documentation

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Create persistent cross-session and cross-model project context from actual repository
evidence without changing application behavior or beginning Phase 2.

### Changes Made

- Created `PROJECT_CONTEXT.md` with the concise current-state handoff, architecture
  invariants, implemented components, commands, evidence, and unknowns.
- Created `PHASE_PLAN.md` with the four-phase roadmap, Phase 2 prompt sequence, future
  Phase 3/4 plans, and evidence-based scaling gates.
- Created `CHANGELOG_AI.md` with this append-only session template and a best-effort,
  clearly labeled Phase 1 history.
- Cross-checked acceptance wording so controlled PASS results are not represented as
  real-staging PASS results.

### Files Added

- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Files Modified

- None.

### Tests Run

- `python -m pytest -q` — 177 passed, 1 deselected.
- `python -m ruff check src tests` — passed.
- `python -m mypy src` — passed.
- Documentation path/command cross-check — passed.

### Staging Tests

- `python -m pytest -o addopts="" -m staging tests/staging` — **NOT RUN**.
- Reason: no authorised real-staging configuration/run window was supplied; staging is
  intentionally opt-in and excluded from normal tests.

### Important Decisions

- Current Phase 1 acceptance is documented as PARTIAL.
- Every real-staging acceptance question remains UNKNOWN despite controlled local PASS
  evidence.
- Phase 2 planning is documented, but no Phase 2 behavior or configuration was
  implemented.

### Known Issues

- Real staging behavior and benchmark measurements remain unverified.
- The default application CLI does not assemble the general runtime.

### Follow-Up

Phase 2 Prompt 1 — Phase 2 Configuration and Scaling Readiness

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: documentation-only additions; clean before this task

## 2026-09-26 — Phase 2 Prompt 1 — Phase 2 Configuration and Scaling Readiness

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Prepare the existing parked-session architecture for a 100-session Phase 2 profile with
up to 25 live contexts and one or two Chrome processes, without performance tuning,
PostgreSQL, distributed workers, or Phase 3 behavior.

### Changes Made

- Changed runtime and example-environment defaults to 100 target Queue IDs, HYBRID
  mode, one Chrome process, 25 contexts per browser, and 25 global active contexts.
- Kept creation and monitoring worker defaults at one to avoid unmeasured concurrency
  tuning; both remain configurable.
- Added validation that the configured browser processes can supply the global context
  limit, that each worker count fits the limit, and that combined creation/monitor
  worker demand cannot exceed global context capacity.
- Added explicit one- and two-browser Phase 2 configuration tests plus zero/invalid and
  contradictory-capacity cases.
- Added a 100-target creation test proving the controller uses only its fixed worker
  pool and bounded queues.
- Added a 100-row parked-session test proving the scheduler claims only the 25 available
  bounded-queue slots rather than creating work for the entire population.
- Updated README and persistent handoff/phase-plan truth for the Phase 2 profile.
- Audited `create_task`, `gather`, queue, context, browser-launch, and repository-list
  call sites. Population-related gathers remain limited to fixed workers/tasks; no
  application path creates a task, context, or Chrome process per persisted session.

### Files Added

- None.

### Files Modified

- `.env.example`
- `README.md`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`
- `src/queue_load_test/config.py`
- `tests/unit/test_config.py`
- `tests/unit/test_creation.py`
- `tests/unit/test_monitoring.py`
- `tests/unit/test_acceptance_report.py`

### Tests Run

- `python -m pytest -q tests/unit/test_config.py tests/unit/test_creation.py tests/unit/test_monitoring.py tests/unit/test_acceptance_report.py tests/unit/test_browser_manager.py` — 58 passed.
- `python -m pytest -q` — 187 passed, 1 deselected.
- `python -m ruff check src tests` — passed.
- `python -m mypy src` — passed with no issues in 35 source files.

### Staging Tests

- `python -m pytest -o addopts="" -m staging tests/staging` — **NOT RUN**.
- Reason: this prompt changes and validates configuration/readiness only; no authorised
  staging run or performance tuning was requested. The normal suite deselected the
  staging test.

### Important Decisions

- Selected one Chrome process with a per-browser/global limit of 25 as the conservative
  default. A two-process profile with 13 contexts per browser and 25 globally is valid.
- Left worker defaults at one because concurrency tuning belongs to Phase 2 Prompt 7;
  operators can configure higher bounded counts within the validated global limit.
- Added combined worker validation because creation and monitoring run concurrently and
  share the same `BrowserManager` capacity.
- Preserved the repository, SQLite, local state, lifecycle, transfer, restoration, and
  parked-session design unchanged.

### Known Issues

- No 100-session real-staging run or performance/resource benchmark has been performed.
- Phase 1 real-staging acceptance remains PARTIAL/UNKNOWN as documented previously.
- The Phase 1 staging harness now requires explicit Phase 1 target/context overrides
  when starting from the Phase 2 `.env.example` profile.
- Dedicated multi-browser allocation/failure validation is intentionally deferred to
  the next prompt.

### Follow-Up

Phase 2 Prompt 2 — Multi-Browser BrowserManager

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 1 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 2 — Multi-Browser BrowserManager

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Make `BrowserManager` reliable with one or two shared Google Chrome processes and a
global limit of 25 active contexts while preserving the bounded parked-session design.

### Changes Made

- Added an explicit stable browser-slot ID accessor to owned contexts for diagnostics
  and allocation verification.
- Moved failed-process relaunches into one tracked task per browser slot, allowing a
  healthy Chrome process to remain allocatable while another process restarts.
- Kept disconnected-slot cleanup, capacity release, and replacement installation
  synchronized without holding the global manager lock during Chrome relaunch.
- Ensured only the disconnected process is replaced and its lost contexts are marked
  closed; healthy processes and contexts are preserved.
- Moved context close I/O outside the accounting lock after capacity is safely released.
- Added tests for one- and two-browser startup, Phase 2's 25-context cap and balanced
  13/12 allocation, context-creation failure, isolated restart, healthy capacity during
  a deliberately blocked restart, restored capacity, and leak-free cleanup.

### Files Added

- None.

### Files Modified

- `src/queue_load_test/browser/manager.py`
- `tests/unit/test_browser_manager.py`
- `PROJECT_CONTEXT.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest tests/unit/test_browser_manager.py -q` — 19 passed.
- `python -m pytest -q` — 193 passed, 1 deselected.
- `python -m ruff check .` — passed.
- `python -m mypy src` — passed with no issues in 35 source files.

### Staging Tests

- `python -m pytest -o addopts="" -m staging tests/staging` — **NOT RUN**.
- Reason: the prompt requested mock/fake verification and did not require a large or
  real-staging load run. The normal suite deselected the opt-in staging test.

### Important Decisions

- Retained least-loaded allocation with the slot index as a stable internal browser ID.
- Used at most one restart task per configured browser slot; this is bounded by Chrome
  process count and is never tied to the persisted session population.
- A caller waits for restart only when no healthy slot can accept work. Explicit
  restart/capacity probes may wait for all currently failed slots to finish recovery.
- Kept worker code dependent only on `BrowserManager.context()` / `create_context()`;
  no scheduler or worker receives a raw Playwright `Browser`.

### Known Issues

- Real installed-Chrome behavior with two processes, actual process crashes, resource
  use, and staging traffic remains unverified; current failure coverage uses fakes.
- Browser context creation is deliberately serialized by the manager's accounting lock;
  creation-throughput tuning belongs to later Phase 2 prompts and was not attempted.
- No 100-session real-staging acquisition or performance benchmark has been run.

### Follow-Up

Phase 2 Prompt 3 — Scale Queue ID Acquisition to 100 Sessions

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 2 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 3 — Scale Queue ID Acquisition to 100 Sessions

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Validate and complete bounded acquisition of 100 successful unique Queue IDs without
counting failed, duplicate, or pre-identity attempts and without creating population-
sized task sets.

### Changes Made

- Preserved the fixed-worker, bounded-queue creation architecture and made its runtime
  activity visible through controller metrics.
- Added initial persisted count, IDs acquired during the current run, maximum active
  workers, current/maximum queue depth, and sessions-per-second to `CreationMetrics`.
- Added low-cardinality Prometheus counters for duplicate, transient, and permanent
  outcomes plus gauges for active creation, queue depth, and current-run acquisition
  rate; retained the existing creation-duration histogram.
- Wired transient/permanent classification and duplicate detection from the real
  creator into the new metrics.
- Verified duplicate persistence leaves the original session untouched, removes the
  duplicate attempt's HYBRID state file, and stores the attempt as `FAILED` without a
  Queue ID.
- Added deterministic partial-restart and 99/100 near-target tests, plus stronger
  worker saturation and 100-target telemetry assertions.
- Updated persistent project context and the Phase 2 roadmap.

### Files Added

- None.

### Files Modified

- `src/queue_load_test/scheduler/creation.py`
- `src/queue_load_test/metrics/prometheus.py`
- `tests/unit/test_creation.py`
- `tests/unit/test_observability.py`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest tests/unit/test_creation.py -q` — 11 passed.
- `python -m pytest tests/unit/test_creation.py tests/unit/test_observability.py -q` —
  14 passed during focused development.
- `python -m pytest -q` — 196 passed, 1 deselected.
- `python -m ruff check .` — passed.
- `python -m mypy src` — passed with no issues in 35 source files.

### Staging Tests

- `python -m pytest -o addopts="" -m staging tests/staging` — **NOT RUN**.
- Reason: this prompt requested acquisition correctness, not Phase 2 benchmarking, and
  no authorised 100-session staging run configuration/window was supplied.

### Important Decisions

- Retained repository recounting as the source of truth after each completed result;
  in-memory success totals never decide target completion alone.
- Near the target, scheduled work is capped by the remaining repository deficit. At
  99/100 only one item is submitted even if ten workers are configured.
- Completed in-flight work is drained on target completion/shutdown. Within the single
  controller, in-flight work is already bounded by the deficit; no queued item is
  cancelled after it may have created a staging identity.
- Kept target coordination single-process and SQLite-backed; no distributed reservation
  mechanism or PostgreSQL behavior was introduced.

### Known Issues

- No authorised 100-session Queue-it staging acquisition, throughput measurement, or
  resource benchmark has been run.
- Multiple independent creation controllers are not supported. If introduced outside
  the intended runtime, they could produce a bounded-per-controller target overshoot
  with different valid Queue IDs, although database uniqueness still rejects duplicates.
- Real Queue-it duplicate frequency and transient/permanent failure rates remain unknown.

### Follow-Up

Phase 2 Prompt 4 — Monitoring Throughput

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 3 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 4 — Monitoring Throughput

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Validate and improve due-session scheduling for 100 persisted sessions while keeping
browser work, asyncio queues, worker tasks, and leases strictly bounded.

### Changes Made

- Added `count_due_sessions(now=...)` to the repository boundary and SQLite
  implementation, using the same due, lease, and terminal-state criteria as claims.
- Added scheduler visibility for current/maximum queue depth, current/maximum active
  checks, due backlog, scheduling/idle iterations, and lease conflicts.
- Added low-cardinality Prometheus gauges/counters for monitoring workers, queue depth,
  due backlog, claimed sessions, and lease conflicts.
- Kept claim size capped by both free bounded-queue capacity and configured batch size;
  no task or browser context is allocated per persisted session.
- Made failed lease release ownership observable while preserving owner-checked release.
- Added a bounded queued/active ownership set so an expired lease reclaimed by the same
  scheduler renews ownership without enqueueing a simultaneous local duplicate check.
- Added synthetic 100-session due/future and blocked-worker tests, exception re-parking,
  idle tick pacing, backlog telemetry, and explicit lease-expiry count assertions.
- Updated persistent project context and Phase 2 roadmap status.

### Files Added

- None.

### Files Modified

- `src/queue_load_test/repository/base.py`
- `src/queue_load_test/repository/sqlite.py`
- `src/queue_load_test/scheduler/monitoring.py`
- `src/queue_load_test/metrics/prometheus.py`
- `tests/unit/test_monitoring.py`
- `tests/unit/test_repository.py`
- `tests/unit/test_observability.py`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest tests/unit/test_monitoring.py tests/unit/test_repository.py tests/unit/test_observability.py -q` — 27 passed.
- `python -m pytest -q` — 200 passed, 1 deselected.
- `python -m ruff check .` — passed.
- `python -m mypy src` — passed with no issues in 35 source files.

### Staging Tests

- `python -m pytest -o addopts="" -m staging tests/staging` — **NOT RUN**.
- Reason: this task validates bounded monitoring mechanics with synthetic sessions; the
  full Phase 2 real-environment benchmark was explicitly deferred.

### Important Decisions

- Defined backlog as due, non-terminal sessions whose lease is absent or expired;
  queued and actively checked sessions are leased and therefore excluded.
- Retained one fixed task per configured monitoring worker. A slow-worker test with 100
  sessions proves two active checks plus two queued leases, not 100 tasks or contexts.
- Kept lease expiry as crash recovery. Before expiry, atomic SQLite claims prevent
  simultaneous ownership. The same scheduler renews an expired locally owned lease
  without duplicating work; cross-process renewal remains a future concern.
- Used one indexed due-count query per scheduler tick for backlog visibility. This is
  appropriate at 100 sessions but its cost must be measured at later scales.

### Known Issues

- No real Chrome/Queue-it 100-session monitoring sweep, checks-per-second benchmark, or
  backlog drain measurement has been performed.
- Real restore latency may approach the configured lease duration; lease-duration
  adequacy must be validated during restore reliability and resource benchmarks.
- SQLite due-count/claim latency is only unit-tested at this scale, not benchmarked.

### Follow-Up

Phase 2 Prompt 5 — HYBRID Restore Reliability Benchmark

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 4 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 5 — HYBRID Restore Reliability Benchmark

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Create a repeatable, explicitly gated benchmark for official transfer,
`storage_state`, and transfer-first/fallback reliability across up to 100 HYBRID
sessions without exposing transfer URLs or changing expected identities.

### Changes Made

- Added a benchmark domain/reporting module with per-invocation identity, mode,
  timestamp, duration, result status, sanitized failure, browser failure, fallback, and
  nested mechanism-attempt records.
- Added aggregate success/failure and mismatch rates, average/p50/p95 duration,
  transfer/storage reliability, fallback usage, and error-category counts.
- Added atomic JSON report replacement and an aggregate-only terminal summary.
- Added an explicit single-method restorer API for transfer-only and storage-only
  probes. Probe runs do not refresh the stored HYBRID state; normal restoration still
  refreshes state after success.
- Configured storage-only probes to create a context from Playwright `storage_state`
  and navigate to the configured staging destination.
- Added a Phase 2 CLI and staging test protected by both `RUN_STAGING_TESTS=1` and
  `RUN_PHASE2_RESTORE_BENCHMARK=1`, plus the existing confirmation flag.
- Added tests for aggregation, mismatch/failure/fallback accounting, percentiles, mode
  selection, URL omission, gate enforcement, and storage-only behavior.
- Added a NOT RUN/UNKNOWN benchmark document and updated handoff/roadmap documentation.

### Files Added

- `src/queue_load_test/harness/restore_benchmark.py`
- `src/queue_load_test/harness/phase2_restore.py`
- `tests/unit/test_restore_benchmark.py`
- `tests/staging/test_phase2_restore_benchmark.py`
- `docs/phase2-restore-benchmark.md`

### Files Modified

- `.gitignore`
- `README.md`
- `pyproject.toml`
- `src/queue_load_test/harness/__init__.py`
- `src/queue_load_test/transfer/restoration.py`
- `tests/unit/test_restoration.py`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest tests/unit/test_restore_benchmark.py tests/unit/test_restoration.py tests/unit/test_acceptance_report.py -q` — 19 passed.
- `python -m pytest -o addopts="" --collect-only -q -m staging tests/staging` — 2 staging tests collected.
- `python -m pytest -q` — 206 passed, 2 deselected.
- `python -m ruff check .` — passed.
- `python -m mypy src` — passed with no issues in 37 source files.

### Staging Tests

- Phase 2 restore benchmark — **NOT RUN**.
- Reason: `RUN_STAGING_TESTS` and `RUN_PHASE2_RESTORE_BENCHMARK` were both unset and no
  `.env` staging configuration was present. No browser traffic was sent.

### Important Decisions

- Benchmark runs are sequential and reuse `BrowserManager`; sample size never becomes a
  matching number of tasks or contexts.
- Raw Queue IDs are retained in the machine report because the prompt requires explicit
  expected/observed identity auditing. Transfer URLs and browser state are excluded;
  the report is git-ignored and documented as sensitive local evidence.
- Transfer-only/storage-only probes avoid state refresh so one mechanism measurement
  does not alter the next. Production HYBRID behavior remains unchanged.
- A HYBRID invocation that encounters any identity mismatch is counted as a mismatch
  even if a later storage fallback restores the expected identity.

### Known Issues

- No real transfer or storage-state reliability, fallback frequency, duration
  percentile, failure distribution, or identity mismatch result exists yet.
- The benchmark requires an existing authorised HYBRID population; it does not create
  100 sessions itself.
- Sequential probing favors repeatability over throughput measurement; resource and
  concurrency benchmarking belongs to the next prompts.

### Follow-Up

Phase 2 Prompt 6 — Resource and Stability Benchmarking

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 5 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 6 — Resource and Stability Benchmarking

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Add a lightweight, repeatable Phase 2 resource and stability benchmark for the
100-session, 25-context HYBRID profile and make one- versus two-Chrome runs directly
comparable without adding high-cardinality telemetry.

### Changes Made

- Added a fixed-cadence recorder for optional application/Chrome CPU and RAM, observed
  and managed process counts, active contexts, optional file descriptors, bounded queue
  depth, and due backlog.
- Added aggregate failure counts, acquisition/check throughput, and average/p50/p95
  creation, context-creation, restore, and check latency.
- Added atomic machine-readable JSON and a concise aggregate terminal report, plus a
  stable comparison helper. Reports contain no session IDs, Queue IDs, transfer URLs,
  or browser state and explicitly warn against Phase 3/4 extrapolation.
- Added an optional `psutil` benchmark dependency. Missing system facilities and
  disappearing processes produce unavailable values instead of aborting the run.
- Added an explicitly gated staging runner that requires a dedicated empty SQLite
  database, acquires 100 sessions with existing bounded workers, then exercises the
  existing parked-session scheduler for a controlled duration.
- Added aggregate BrowserContext-creation and navigation-failure Prometheus counters
  and wired them at their owning browser/navigation boundaries without labels.
- Added the run/comparison procedure and recorded the current real result as NOT RUN.

### Files Added

- `src/queue_load_test/harness/resource_benchmark.py`
- `src/queue_load_test/harness/phase2_resources.py`
- `tests/unit/test_resource_benchmark.py`
- `tests/staging/test_phase2_resource_benchmark.py`
- `docs/phase2-resource-benchmark.md`

### Files Modified

- `.gitignore`
- `pyproject.toml`
- `README.md`
- `src/queue_load_test/metrics/prometheus.py`
- `src/queue_load_test/browser/manager.py`
- `src/queue_load_test/scheduler/creation.py`
- `src/queue_load_test/transfer/restoration.py`
- `tests/unit/test_observability.py`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest tests/unit/test_resource_benchmark.py tests/unit/test_observability.py tests/unit/test_browser_manager.py tests/unit/test_creation.py tests/unit/test_restoration.py -q` — 46 passed.
- `python -m pytest -q` — 210 passed, 3 deselected.
- `python -m pytest -o addopts="" --collect-only -q -m staging tests/staging` — 3 staging tests collected.
- `python -m ruff check .` — passed.
- `python -m mypy src` — passed with no issues in 39 source files.
- `git diff --check` — passed; Git emitted only expected LF-to-CRLF working-copy notices.

### Staging Tests

- Phase 2 resource and stability benchmark — **NOT RUN**.
- Reason: `RUN_STAGING_TESTS` and `RUN_PHASE2_RESOURCE_BENCHMARK` were unset and no
  `.env` staging configuration was present. No browser traffic was sent.

### Important Decisions

- Sampling uses one fixed task and existing bounded controllers; it never creates a
  task or context per persisted session.
- `BrowserManager` remains the authoritative source for managed Chrome-process and
  active-context counts. Optional process-tree discovery is used only for aggregate OS
  CPU/RAM observations and may include Chrome helper processes.
- One- and two-browser runs use separate empty database/state locations and the same
  report schema. A two-process run uses a per-browser capacity of at least 13 to cover
  the global limit of 25.
- A completed benchmark is not an acceptance PASS. Real measurements remain UNKNOWN
  until the explicitly gated staging runs are performed.

### Known Issues

- No authorised one-browser or two-browser resource run was performed, so all host
  headroom, throughput, latency, stability, and comparative observations are UNKNOWN.
- `psutil` is optional; without the `benchmark` extra, process CPU/RAM and file
  descriptors are unavailable while BrowserManager/Prometheus data remain present.
- On platforms without `psutil.Process.num_fds`, the file-descriptor field is `null`.
- The harness records context churn and aggregate SQLite/monitoring behavior but does
  not yet report database/state-directory disk footprint.

### Follow-Up

Phase 2 Prompt 7 — Concurrency Tuning Harness

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 6 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 7 — Concurrency Tuning Harness

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Build a controlled, repeatable Phase 2 concurrency matrix that records objective
behavior as bounded concurrency increases without automatically choosing an optimal
configuration.

### Changes Made

- Added deterministic matrix generation for 5, 10, 15, 20, and 25 maximum contexts
  across one or two Chrome processes, plus strict JSON manifests for explicit browser,
  worker, per-browser capacity, queue, and claim-batch settings.
- Added a checked-in ten-case example manifest covering both process counts.
- Added a sequential matrix collector. Each case uses a fresh 100-session SQLite/state
  location; matrix size never becomes simultaneous benchmark tasks or browsers.
- Composed the existing resource runner with optional transfer-only and
  storage-state-only probes, retaining only non-sensitive restore aggregates in the
  matrix report.
- Added machine-readable per-case results, concise comparison rows, and aggregate text
  output without Queue IDs, session IDs, transfer URLs, or browser state.
- Added configurable observation flags for adjacent p95 creation/check/context-
  acquisition latency, failure-rate increases, sustained CPU, RAM growth, crashes,
  identity mismatches, continuously increasing backlog, and restore reliability drops.
  No ranking or winner is produced.
- Added aggregate navigation-duration and BrowserManager context-acquisition-duration
  histograms without identity labels. Context acquisition includes manager contention.
- Added a separately gated staging matrix runner and documented sensitive case work
  directories, cumulative test volume, repetition, and non-extrapolation constraints.

### Files Added

- `src/queue_load_test/harness/concurrency_tuning.py`
- `src/queue_load_test/harness/phase2_tuning.py`
- `tests/unit/test_concurrency_tuning.py`
- `tests/staging/test_phase2_concurrency_benchmark.py`
- `benchmarks/phase2-concurrency-matrix.example.json`
- `docs/phase2-concurrency-benchmark.md`

### Files Modified

- `.gitignore`
- `pyproject.toml`
- `README.md`
- `src/queue_load_test/browser/manager.py`
- `src/queue_load_test/harness/phase2_resources.py`
- `src/queue_load_test/harness/phase2_restore.py`
- `src/queue_load_test/harness/resource_benchmark.py`
- `src/queue_load_test/metrics/prometheus.py`
- `src/queue_load_test/scheduler/creation.py`
- `src/queue_load_test/transfer/restoration.py`
- `tests/unit/test_observability.py`
- `tests/unit/test_resource_benchmark.py`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest tests/unit/test_concurrency_tuning.py tests/unit/test_resource_benchmark.py tests/unit/test_observability.py tests/unit/test_browser_manager.py tests/unit/test_creation.py tests/unit/test_restoration.py -q` — 51 passed during focused development.
- `python -m pytest -q` — 216 passed, 4 deselected.
- `python -m pytest -o addopts="" --collect-only -q -m staging tests/staging` — 4 staging tests collected.
- `python -m ruff check .` — passed.
- `python -m mypy src` — passed with no issues in 41 source files.
- `git diff --check` — passed; Git emitted only expected LF-to-CRLF working-copy notices.

### Staging Tests

- Phase 2 concurrency matrix — **NOT RUN**.
- Reason: `RUN_STAGING_TESTS` and `RUN_PHASE2_CONCURRENCY_BENCHMARK` were unset and no
  `.env` staging configuration was present. No browser traffic was sent.

### Important Decisions

- Cases execute sequentially and continue after a sanitized case failure. Case
  concurrency remains bounded by that case’s validated settings.
- The standard matrix splits creation and monitoring workers within the global context
  limit, preserving the runtime invariant that their configured sum cannot exceed
  `MAX_ACTIVE_CONTEXTS`. An explicit manifest can choose any other valid split.
- Transfer and storage reliability probes are optional and sequential; they measure
  mechanism reliability rather than adding uncontrolled restore concurrency.
- Prometheus histogram percentiles are reported as bucket upper-bound estimates. Raw
  operation percentiles remain exact where the resource recorder holds timings.
- The ten-case manifest creates ten separate 100-session populations over time. It is
  not a 1,000-session architecture test and must be limited to authorised staging
  volume.

### Known Issues

- No real matrix case was run. All throughput, latency, CPU/RAM, failure, restore,
  backlog, stability, and saturation observations remain UNKNOWN.
- No optimal or recommended Phase 2 operating point exists without staging evidence.
- The aggregate matrix JSON is identity-free, but each case work directory contains
  sensitive SQLite/state data and, when restore probes run, a Queue ID audit report.
- `psutil` remains optional, and unavailable system measurements cannot contribute to
  the corresponding saturation flags.

### Follow-Up

Phase 2 Prompt 8 — Phase 2 Acceptance Report

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 2 Prompt 7 changes present; clean before this prompt

## 2026-09-26 — Phase 2 Prompt 8 — Phase 2 Acceptance Report

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Consolidate measured Phase 2 repository evidence into a PASS/FAIL/UNKNOWN acceptance
decision without beginning Phase 3 or inferring staging behavior from unit tests.

### Changes Made

- Added the final Phase 2 acceptance report with exact target and locally tested
  configurations, evidence limits, functional results, benchmark sections, all 20
  required decisions, known issues, and Phase 3 readiness.
- Inventoried tracked, untracked, and ignored workspace files. No Phase 2 result JSON,
  staging result, metrics snapshot, SQLite population, browser-state work directory, or
  other benchmark output was present.
- Recorded 3 PASS, 2 FAIL, and 15 UNKNOWN. Only bounded creation/context capacity,
  100-session parked persistence, and fixed-task/bounded-queue scheduling pass.
- Marked Phase 2 implementation complete but acceptance PARTIAL, and marked Phase 3
  blocked pending acquisition, monitoring, restore, resource, stability, comparison,
  and saturation evidence.
- Updated persistent context and the phase plan; no application behavior changed.

### Files Added

- `docs/phase2-acceptance.md`

### Files Modified

- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `python -m pytest -q` — **NOT RUN**; this shell has no `python` executable.
- `python3 -m pytest -q` — **NOT RUN**; available Python is 3.14.7 and has no `pytest`
  module.
- Latest verified repository result remains `python -m pytest -q` — 216 passed,
  4 deselected, recorded by Phase 2 Prompt 7.
- `git diff --check` — passed before the changelog entry; repeated during final review.

### Staging Tests

- Phase 2 acquisition/resource, restore, and concurrency harnesses — **NOT RUN**.
- Reason: no authorised staging configuration, gates, or existing result artifacts were
  available. No browser traffic was sent.

### Important Decisions

- Local deterministic tests are sufficient for bounded architecture properties but not
  for Queue-it reliability, throughput, restore rates, resource use, or stability.
- A configured maximum of 25 contexts is not reported as an observed context peak.
- The generated 5/10/15/20/25 matrix is not listed as tested because no case ran.
- Missing evidence is UNKNOWN; Phase 3 blocker/readiness questions FAIL because the
  required evidence-based operating point and headroom do not exist.

### Known Issues

- All real Phase 2 rates, latency, resource, failure, identity, backlog-drain,
  one-versus-two-browser, and saturation results remain UNKNOWN.
- The final suite could not be rerun in the current shell because project test
  dependencies are unavailable.
- The general `queue-load-test` CLI still does not assemble the full runtime by default.

### Follow-Up

Phase 3 Prompt 1 — Phase 3 readiness and 1,000-session profile. Do not begin until the
Phase 2 acceptance blockers are resolved or explicitly treated as blocking evidence.

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: documentation-only Phase 2 acceptance changes

## 2026-09-26 — Phase 2 Prompt 8 — Acceptance Report Verification

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Redo the Phase 2 acceptance review from current repository evidence, rerun local
validation after installing dependencies, and leave all changes uncommitted.

### Changes Made

- Re-read the project context, phase plan, complete AI changelog, repository status,
  history, Phase 2 benchmark documents, test evidence, and metric definitions.
- Re-inventoried tracked and ignored workspace artifacts. No Phase 2 staging or
  benchmark output, metrics snapshot, SQLite population, or browser-state work
  directory was found.
- Revalidated all 20 acceptance answers. The result remains 3 PASS, 2 FAIL, and
  15 UNKNOWN; no staging-dependent UNKNOWN was promoted from local test evidence.
- Added the exact local validation environment and current suite/static-check results
  to the acceptance report, project context, and phase plan.
- Made no application behavior changes and did not begin Phase 3.

### Files Added

- None; the existing report remains `docs/phase2-acceptance.md`.

### Files Modified

- `docs/phase2-acceptance.md`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`

### Tests Run

- `.venv/bin/python -m pytest -q` — 216 passed, 4 deselected in 8.49 seconds. The
  deselected tests were the explicitly gated staging harnesses.
- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/mypy src` — passed with no issues in 41 source files.
- Local validation environment: Python 3.14.7, macOS 26.5.1 arm64, Playwright 1.63.0,
  pytest 9.1.1, psutil 7.2.2, and Google Chrome 153.0.8010.54.

### Staging Tests

- Phase 2 acquisition/resource, restore, and concurrency harnesses — **NOT RUN**.
- No authorised staging configuration or result artifacts were available. No staging
  traffic was sent.

### Important Decisions

- Current local tests strengthen the three local architecture PASS results but do not
  establish staging acquisition reliability, performance, restoration, resources, or
  browser stability.
- The configured 25-context limit remains a cap, not a measured active-context peak.
- Phase 3 remains blocked without a measured Phase 2 operating point and host headroom.

### Known Issues

- All real Phase 2 throughput, latency, restore, identity-incidence, resource,
  stability, backlog-drain, browser-comparison, and saturation results remain UNKNOWN.
- The general `queue-load-test` CLI still does not assemble the full runtime by default.

### Follow-Up

Phase 3 Prompt 1 — Phase 3 readiness and 1,000-session profile. Do not begin while the
Phase 2 acceptance blockers remain unresolved.

### Git State

- Commit: intentionally not created for this verification prompt
- Branch: `main`
- Working tree: documentation-only verification changes

## 2026-09-26 — Phase 3 Prompt 1 — 1,000-Session Readiness

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Prepare bounded local configuration and controller/scheduler mechanics for a 1,000
persisted-session Phase 3 target without running staging load, migrating databases, or
claiming that 50–100 active contexts are safe.

### Changes Made

- Changed readiness defaults to 1,000 HYBRID targets, two Chrome processes, 25 contexts
  per browser, and a 50-context global ceiling while retaining one creation and one
  monitoring worker by default.
- Added explicit `CREATION_QUEUE_CAPACITY`; both creation and monitoring queue capacities
  must remain within the global context ceiling. Capped Phase 3 context candidates at
  100 and retained worker-sum, browser-capacity, and claim-batch validation.
- Added structurally valid 50/75/100 candidate profiles using 2/3/4 Chrome processes at
  25 contexts each. These are benchmark inputs, not safe operating-point claims.
- Removed the creation controller's per-outcome target count query. Successful persisted
  outcomes update the hot-loop count, with authoritative SQLite counts at startup and
  completion.
- Added 1,000-session tests proving 20 fixed creation workers/two target-count queries
  and five fixed monitoring tasks/a 50-item queue; added a fake-based 100-context hard
  cap across four Chrome slots.
- Preserved Phase 1/2 harness gates and made their historical profiles explicit where
  the Phase 3 defaults changed.
- Documented Phase 3 bottleneck candidates and advanced the next task to Prompt 2.

### Files Added

- None.

### Files Modified

- `.env.example`
- `README.md`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`
- `src/queue_load_test/config.py`
- `src/queue_load_test/scheduler/creation.py`
- `src/queue_load_test/harness/staging.py`
- `src/queue_load_test/harness/phase2_resources.py`
- `tests/unit/test_config.py`
- `tests/unit/test_creation.py`
- `tests/unit/test_monitoring.py`
- `tests/unit/test_browser_manager.py`
- `tests/unit/test_acceptance_report.py`
- `tests/unit/test_restore_benchmark.py`

### Tests Run

- `.venv/bin/python -m pytest -q tests/unit/test_config.py tests/unit/test_creation.py tests/unit/test_monitoring.py tests/unit/test_browser_manager.py tests/unit/test_restore_benchmark.py` — 84 passed.
- First full `.venv/bin/python -m pytest -q` — 1 failed, 228 passed, 4 deselected;
  the Phase 1 gate fixture relied on the former one-browser default.
- Final `.venv/bin/python -m pytest -q` — 232 passed, 4 deselected in 9.22 seconds.
- Final focused repository/scheduler/configuration regression run — 112 passed in
  1.10 seconds.
- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/mypy src` — passed with no issues in 41 source files.

### Staging Tests

- Phase 3 staging/load benchmarks — **NOT RUN**.
- The four explicitly gated staging harnesses were deselected. No browser traffic was
  sent to a staging environment.

### Important Decisions

- A 50-context default is a benchmark ceiling, not default demand: one creation and one
  monitoring worker remain conservative until measurements justify increases.
- 75 and 100 contexts are accepted only as explicit benchmark candidates. Values above
  100 are rejected during Phase 3 readiness.
- SQLite remains the backend. PostgreSQL and distributed workers require Prompt 2 or
  later evidence and were not introduced.
- The creation controller remains single-controller. Eliminating O(target) count queries
  does not add cross-process target reservation semantics.

### Known Issues

- Runtime startup and `/status` load all sessions; the restore benchmark also loads its
  population. Aggregate/paginated APIs may be needed after measurement.
- SQLite uses one serialized connection, per-operation commits, and a count plus bounded
  `BEGIN IMMEDIATE` claim per scheduler tick. Query plans and contention are unmeasured.
- Browser context creation is awaited while the BrowserManager global lock is held.
- Per-session structured events and per-state-file threaded fsync/replace operations may
  create logging, thread-pool, or disk pressure at 1,000 sessions.
- No Phase 3 throughput, latency, resource, fairness, backlog, restoration, or browser
  stability result exists. Phase 2 staging evidence gaps also remain.

### Follow-Up

Phase 3 Prompt 2 — Repository and Scheduler Performance at 1,000 Sessions

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 3 Prompt 1 implementation and documentation changes

## 2026-09-26 — Phase 3 Prompt 2 — Repository and Scheduler Performance

### Agent / Model

Codex (exact model identifier is not recorded in the repository)

### Goal

Validate and improve SQLite repository and bounded scheduler behavior with 1,000
synthetic sessions, retaining SQLite unless measurements demonstrated a real need to
migrate.

### Changes Made

- Replaced the Phase 2 `(next_check_at, lease_until, status)` index with a partial
  ordered expression index matching the actual due predicate and ordering:
  `COALESCE(next_check_at, created_at), created_at, session_id` for monitorable states.
- Added automatic in-place migration of the old index. Kept the unique constraint's
  existing automatic Queue ID index and did not add unsupported status/lease indexes.
- Reused one shared due filter/order definition for counts, bounded claims, and query
  plan inspection. The query now avoids SQLite's temporary ordering B-tree.
- Retained the short `BEGIN IMMEDIATE` claim transaction, one commit per bounded batch,
  deterministic due ordering, lease-owner checks, expired-lease recovery, and terminal
  status filtering.
- Added a local benchmark CLI that seeds 1,000 mixed sessions without browser or
  staging traffic and measures due count, claim, update, lease release, and scheduler
  iteration latency.
- Added 1,000-row tests for mixed due/future/leased/expired/excluded states, concurrent
  disjoint claims from two SQLite connections, index migration/query planning, bounded
  backpressure, and stable repeated scheduler cycles.
- Documented the benchmark method, measured result, SQLite decision, remaining limits,
  and next Phase 3 task.

### Files Added

- `src/queue_load_test/harness/phase3_repository.py`
- `tests/unit/test_phase3_repository_benchmark.py`
- `docs/phase3-repository-benchmark.md`

### Files Modified

- `.gitignore`
- `README.md`
- `PROJECT_CONTEXT.md`
- `PHASE_PLAN.md`
- `CHANGELOG_AI.md`
- `pyproject.toml`
- `src/queue_load_test/repository/sqlite.py`
- `tests/unit/test_repository.py`
- `tests/unit/test_monitoring.py`

### Tests Run

- Baseline `.venv/bin/python -m pytest -q tests/unit/test_repository.py tests/unit/test_monitoring.py` — 25 passed.
- Final repository/scheduler/benchmark tests — 35 passed in 2.36 seconds.
- Final repository plus benchmark utility tests — 19 passed in 1.31 seconds.
- Final scheduler tests — 16 passed in 1.10 seconds.
- `.venv/bin/python -m pytest -q` — 242 passed, 4 deselected in 10.59 seconds.
- `.venv/bin/ruff check src tests` — passed.
- `.venv/bin/mypy src` — passed with no issues in 42 source files.
- `git diff --check` — passed.

### Synthetic Benchmark

- Command: `.venv/bin/python -m queue_load_test.harness.phase3_repository --sessions 1000 --batch-size 50 --samples 20`.
- Population: 1,000 rows, 600 eligible due, 50-row claim/queue batch.
- Seed: 336.935 ms; database size: 450,560 bytes.
- Due count p50/p95: 0.098/0.126 ms.
- Transactional 50-row claim p50/p95: 0.555/0.673 ms.
- Update p50/p95: 0.365/0.386 ms.
- Lease release p50/p95: 0.279/2.321 ms.
- Scheduler iteration p50/p95: 0.668/0.800 ms.
- Query plan: `SEARCH queue_sessions USING INDEX idx_queue_sessions_due (<expr><?)`;
  no temporary B-tree.

### Staging Tests

- **NOT RUN.** The benchmark is intentionally synthetic and local. No browser or
  staging traffic was started.

### Important Decisions

- SQLite remains the Phase 3 backend. The measured 1,000-row workload does not justify
  PostgreSQL or distributed leasing.
- The scheduler keeps its existing configuration names:
  `MONITOR_CLAIM_BATCH_SIZE`, `MONITOR_QUEUE_CAPACITY`, and `MONITOR_LEASE_SECONDS`.
- Claims stay bounded by both free queue space and claim batch size. Persisted
  population size never determines task or BrowserContext count.
- The query-specific partial expression index is justified by the actual plan. A
  separate Queue ID, status, or lease index was not added blindly.

### Known Issues

- Each repository instance still serializes work through one connection/async lock,
  and SQLite remains a one-writer database across connections.
- Updates and lease releases remain separate per-session commits. Measurements were
  comfortable locally, but sustained browser-driven write contention is not covered.
- Runtime startup and `/status` still load the complete population; this benchmark
  targeted scheduler hot paths, not status aggregation.
- Phase 2 staging evidence and Phase 3 browser capacity/resource evidence remain
  missing, so these local results are not a general scalability claim.

### Follow-Up

Phase 3 Prompt 3 — Browser Capacity Benchmark at 50–100 Contexts

### Git State

- Commit: pending at the time this entry was written
- Branch: `main`
- Working tree: Phase 3 Prompt 2 implementation, tests, benchmark, and documentation
