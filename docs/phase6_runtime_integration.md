# Phase 6 Camoufox Runtime Integration

**Date:** 2026-09-27  
**Scope:** Phase 6 Prompt 4; local simulator only  
**Decision:** **PASS for opt-in Queue-session workflows; Camoufox remains non-default**

## Prompt 3 design correction

Camoufox 0.5.6 still does not expose a supported complete per-context fingerprint
descriptor that reproduces an identical fingerprint after parking. That capability is
**FAIL / unsupported**, but it is **not a Quetty requirement**.

Queue ID is the authoritative persisted external journey identity. Transfer URL and
same-backend Playwright storage state are restoration mechanisms. BrowserContexts are
temporary and disposable, and Camoufox browser/device characteristics may change on
every reconstruction. A fingerprint change never permits Queue ID replacement. Every
restore compares the observed Queue ID with the expected persisted value; mismatch
preserves the expected value and follows the existing failure/retry path.

No fingerprint preset, generated configuration, init script, or private Camoufox value
is persisted.

## Implemented runtime model

`run_config` and every `queue_sessions` row now persist one
`browser_backend` enum (`chrome` or `camoufox`). New sessions inherit the immutable run
backend through the normal creator. Pre-Phase-6 databases are migrated to `chrome`,
which matches repository history. Restart reconstructs the runtime from the persisted
run backend rather than a changed environment setting.

A run/session provenance mismatch returns `BACKEND_MISMATCH` before opening a context.
The report-only state checker also emits `backend_provenance_mismatch`. Cross-engine
storage fallback is therefore never attempted implicitly. This provenance is not a
fingerprint identity.

The lifecycle is:

```text
create fresh context
-> acquire Queue ID and transfer URL
-> save HYBRID state (or no state for TRANSFER_ONLY)
-> close and park
-> create fresh same-backend context
-> restore transfer first
-> use same-backend HYBRID fallback only when needed
-> verify Queue ID
-> inspect/evaluate/save
-> close and park
```

The scheduler, lifecycle evaluator, repository, dashboard, operator pool, and monitoring
orchestration remain browser-agnostic. Chrome remains the configuration default.

## Workflows exercised

The backend-parameterized controlled workflow runs the real FastAPI lifespan, SQLite
repository, creator, scheduler, monitor, lifecycle evaluator, operator pool, manual
ownership manager, browser processes, and state files against `LocalQueueSimulator`.
No Queue-it or staging host is contacted.

Both Chrome and Camoufox exercised:

- first-run setup and bounded deficit acquisition;
- park and automatic transfer-first reconstruction;
- repeated monitoring and progress persistence;
- pause/resume, including Refresh Now while paused;
- Add with duplicate-submit deduplication;
- create-first Replace and population accounting;
- Delete including progress/state cleanup;
- manual Open, scheduler exclusion, Close, and final state refresh;
- actual page/window loss with ownership release;
- opening a FAILED/no-ID row, unique Queue-ID adoption, and continued provenance;
- duplicate adoption rejection with conflicting new state discarded;
- expected Queue-ID mismatch without replacement;
- full application restart with immutable target, IDs, backend, and pause state;
- interrupted acquisition restart and exact deficit recovery;
- Stop & Reset Run with processes, contexts, owners, rows, progress, and state removed.

Backend-agnostic focused tests additionally cover failed/duplicate Replace preserving
the old row, action fencing, capacity refusal, browser loss, reset failure recovery,
and deletion while owned. A dedicated installed-Camoufox test covers TRANSFER_ONLY
creation/restoration without a state file.

## Results

| Evidence | Chrome | Camoufox |
|---|---:|---:|
| Complete controlled application workflow | **PASS, 61/61** | **PASS, 61/61** |
| HYBRID transfer-first restore | **PASS** | **PASS** |
| TRANSFER_ONLY without storage state | existing regression **PASS** | **PASS** |
| Manual ownership/Open/Close/window loss | **PASS** | **PASS** |
| No-ID adoption and duplicate rejection | **PASS** | **PASS** |
| Full app restart and deficit resume | **PASS** | **PASS** |
| Stop & Reset cleanup | **PASS** | **PASS** |
| Expected Queue-ID mismatch preservation | **PASS** | **PASS** |

The dedicated Camoufox continuity test completed **20/20** consecutive fresh-context
restore/verify/inspect/HYBRID-save/close cycles for one persisted Queue session. After
cycle 10 it shut down Camoufox and Playwright completely, closed the repository, opened
new repository/state objects, constructed a new manager/backend/process, and completed
the remaining ten cycles without changing Queue ID or backend provenance. An injected
observed-ID mismatch then failed and left the persisted expected Queue ID unchanged.

## Defects found and fixed

1. Run and session records lacked backend provenance. Persisted enum columns and safe
   legacy migrations now prevent environment changes or state shape from silently
   converting Chrome sessions.
2. Backend mismatch could otherwise reach browser restoration. It now fails before a
   context opens and is reported by the state checker.
3. Browser launch and Playwright shutdown were not both explicitly deadline-bounded.
   They now use the existing manager operation/close deadlines; navigation, restore,
   context close, browser close, and restart were already bounded.
4. The process evidence helper counted Camoufox child processes as independent browser
   processes. It now counts only the parent `camoufox` executable.
5. Camoufox 0.5.6 accepted concurrent contexts but repeated concurrent HTTP navigation
   waves sometimes left `page.goto` pending. `CamoufoxBackend` now serializes live
   contexts per managed process. This stays below the backend boundary, uses shared
   bounded processes, and does not allocate a process per session.
8. The first serialization implementation used one lease for the whole backend
   instance and waited for it while the manager held its allocation lock. It serialized
   *all* processes on a manager, a hung context close stranded the lease, and a second
   retained manual window would have blocked every allocation on the manual pool until
   the operation deadline and then failed. The lease is now keyed per managed process,
   released as soon as a close starts, dropped with its process, and the long-lived
   manual Open pool (`create_browser_backend(..., long_lived_contexts=True)`) does not
   serialize. Unit tests cover per-process independence, release under a hung close,
   and concurrent operator contexts.
6. Acceptance checks assumed Chrome's concurrent monitor timing. They now wait for all
   expected persisted observations and correctly allow already-in-flight work to drain
   after pause.
7. UI labels and errors that described generic browser ownership as Chrome were made
   browser-neutral. The persisted/internal `OPEN_IN_CHROME` value remains unchanged for
   compatibility and is documented as historical naming.

## Storage compatibility and fallback

Prompt 3's meaningful local matrix remains:

| Source | Destination | Result |
|---|---|---|
| Camoufox | Camoufox | **PASS** |
| Chrome | Camoufox | **PASS locally; UNKNOWN for Queue-it** |
| Camoufox | Chrome | **FAIL locally** (cookie absent) |
| Chrome | Chrome | **PASS** |

The runtime does not use the locally passing cross-engine direction: provenance must
match before transfer or state restoration. HYBRID remains transfer-first and refreshes
state after a verified observation. TRANSFER_ONLY has no long-term state dependency.

## Capacity, cleanup, and timeouts

Automatic, creation, and manual managers continue to share the global active-context
coordinator. Fixed workers and bounded queues are unchanged. The controlled Camoufox
run observed one automatic parent process; a second bounded process existed only while
the separate manual pool was live. Shutdown and reset returned parent-process count,
active contexts, and persisted ownership to zero.

Camoufox's per-process context serialization for the automatic and creation pools is a
conservative 0.5.6 reliability constraint; the manual Open pool is bounded by
`MAX_MANUAL_OPEN_SESSIONS` instead, because its contexts are retained for the operator. Its sustainable throughput and recovery behavior are deliberately left for
Prompt 5 rather than guessed here.

## Known limitations / UNKNOWN

- No authorised Queue-it staging traffic ran. Real Queue-it transfer, storage fallback,
  admission, and no-ID adoption remain **UNKNOWN** for Camoufox.
- The automated manual workflow used a real Camoufox browser with headless display mode
  for deterministic local execution. Visible headed Camoufox operator ergonomics on
  each target host remain **UNKNOWN**; ownership and retained-context behavior use the
  same path.
- Camoufox fingerprint continuity remains **FAIL**, intentionally not required.
- Cross-engine Queue-it storage compatibility remains **UNKNOWN** and is disabled by
  provenance checks.
- Concurrent navigation reliability of several simultaneously open manual Camoufox
  windows on one process remains **UNKNOWN**; each is bounded by navigation deadlines.
- Capacity beyond the conservative serialized per-process profile and deliberate
  Camoufox crash/restart pressure remain Prompt 5 work.

## Validation

- Focused persistence/restoration/UI tests: **PASS**.
- Dedicated Camoufox continuity and TRANSFER_ONLY tests: **PASS**.
- Controlled Chrome workflow: **PASS, 61/61**.
- Controlled Camoufox workflow: **PASS, 61/61**.
- Full non-staging suite: **483 passed, 4 staging tests deselected**.
- Ruff: **PASS**.
- strict mypy: **PASS**.
- Staging: **NOT RUN**.
