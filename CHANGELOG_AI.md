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
