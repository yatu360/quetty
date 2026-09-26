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
