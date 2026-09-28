# Project Context

## Project

This repository is an authorised Queue-it staging test system. It creates independent
browser visitors, persists their Queue-it identities and progress, parks them without
keeping browsers open, and restores a bounded subset through the configured browser
backend (Patchright by default; Chrome fallback; Camoufox retained, uncertified) for
live monitoring.

## Current Status

### Monitoring strategy and direct-status research (Phase 8 Prompts 1–3, 2026-09-29)

- Every run now persists one immutable monitoring strategy independently from its
  browser backend: `headed_window` (**Headed Window Strategy**) or `direct`
  (**Direct Monitoring Strategy**).
- Headed Window Strategy is the historical automatic path: restore through the
  persisted backend, inspect the live page/DOM, persist, and park. Its operator-facing
  name does not force the automatic pool to become visibly headed; `HEADLESS` is
  unchanged and Manual Open remains the explicit headed window.
- Direct Monitoring Strategy still uses the existing browser monitor for every check.
  Prompt 2 adds an opt-in, bounded observer around that legitimate browser activity;
  it does not construct, call, or replay a Queue-it status URL.
- Setup selects and persists the strategy. Startup uses
  `run_config.monitoring_strategy`, never a changed environment default. Legacy runs
  migrate to `headed_window`; changing strategy requires Stop & Reset Run.
- Queue IDs and session rows are not migrated or reacquired. Browser backend and
  monitoring strategy remain independent run dimensions. See
  `docs/phase8_monitoring_strategy.md`.
- Raw discovery artifacts are mode-0600 files beneath a mode-0700, git-ignored local
  directory and may contain credentials. Normal logs expose only sanitized
  classification counts. Discovery data never enters SQLite, metrics, dashboard HTML,
  or aggregate reports. See `docs/phase8_status_discovery.md`.
- Prompt 3 adds a separately gated experimental replay client. It accepts only an exact
  status-candidate exchange from a matching authorised Prompt 2 artifact and that
  session's persisted browser state. It has bounded HTTP resources, distinct failure
  classes, protected response-cookie continuation, repeated header/cookie minimisation,
  and no production-monitor integration. See `docs/phase8_direct_replay.md`.
- Queue-it staging is **NOT RUN / UNKNOWN**. No genuine artifact exists in the
  workspace, so storage sufficiency A/B/C/D remains unknown and Phase 8 Prompt 4 is
  blocked pending an authorised Prompt 3 evidence run.

### Browser backend policy (authoritative, Phase 7 accepted 2026-09-28)

- **Patchright is the default for NEW runs** (`BROWSER_BACKEND=patchright`, also the
  default when unset). It uses Patchright 1.63.0 driving installed Google Chrome via
  `channel="chrome"`. New-run setup runs the Patchright preflight and records the
  observed Chrome build.
- **Chrome** (`BROWSER_BACKEND=chrome`) is the supported fallback/control. It passed the
  Phase 7 fallback regression.
- **Camoufox** (`BROWSER_BACKEND=camoufox`) is retained as dormant/experimental. It is
  **not certified** by Phase 7 and not recommended. Its implementation, config value,
  provenance, schema, and restart path are kept, but routine Phase 7 tests and
  benchmarks exclude it.
- **Existing runs are never migrated.** A run restarts with its persisted
  `run_config.browser_backend`; legacy rows are Chrome. Switching backends requires
  Stop & Reset Run and a new run.
- Queue ID is the authoritative identity. Staging is **NOT RUN / UNKNOWN** for every
  backend in Phase 7.

- **Phase 7 Prompt 6 is complete: Phase 7 ACCEPTED** (`docs/phase7_acceptance.md`,
  `docs/results/phase7_acceptance_result.json`).
  - All 18 decision gates passed, and the new-run default changed Chrome → Patchright.
  - Final `queue-load-test-phase7-acceptance`:
    - Patchright: preflight PASS, headed application workflow 74/74,
      restoration/recovery scenarios 102/102, and the 50-context ceiling healthy.
    - Chrome fallback: preflight PASS, workflow 74/74, scenarios 102/102.
    - 352/352 checks, 0 leftover processes, zero Queue ID changes.
  - Known issues: a restore attempt in flight on a killed browser waits out its
    deadline (both backends); shutdown is bounded per stage; Patchright costs a little
    more than Chrome locally; the Chrome build is not pinned; an intermittent
    fake-based operator-fencing unit test.
  - Next: an authorised Queue-it staging validation of the Patchright default.

- **Phase 7 Prompt 5 is complete: `READY_FOR_DEFAULT_DECISION`**
  (`docs/phase7_patchright_benchmark.md`,
  `docs/results/phase7_patchright_benchmark_result.json`).
  - Patchright (candidate) and Chrome (control) each passed 15/15 scenarios and
    247/247 checks on the same host (`queue-load-test-phase7-patchright-benchmark`).
    Camoufox was excluded.
  - Multiple live Patchright contexts per process are healthy: 1–25 on one process and
    50 on two, with zero reacquisition, churn, wedge, or leak failures. Patchright keeps
    the ordinary Chrome concurrency model and ceilings; no Patchright-specific limit or
    timeout-restart heuristic is needed.
  - Park/reopen (300), browser restart (150), application restart, 29 kills including
    20 repeated (restart p95 0.185 s), stuck navigation, manual headed ownership,
    pause/resume under failure, and the shutdown matrix all passed with zero Queue ID
    changes. Shutdown is bounded by per-stage `SHUTDOWN_TIMEOUT_SECONDS`.
  - Costs vs Chrome locally: about 10–30% higher restore latency and about 8% lower
    monitoring throughput (12 vs 13 checks/s at 100 sessions). Per-process page
    throughput, not context count, is the shared ceiling.
  - Chrome remains the default. Staging is **NOT RUN / UNKNOWN**. Next is Phase 7
    Prompt 6.

- **Phase 7 Prompt 4 is complete:** Patchright passes the complete controlled local
  runtime/dashboard workflow (`docs/phase7_patchright_runtime_integration.md`).
  - Patchright passed 74/74 checks with a visible headed manual window; Chrome passed
    the same 74/74 extended checks as control. Camoufox was excluded from the Phase 7
    workflow result but remains implemented and provenance-compatible.
  - Setup/acquisition, dashboard fields, automatic monitoring, pause/in-flight drain,
    resume, Refresh, Add while paused, create-first Replace, Delete, manual ownership,
    direct window close, manual/automatic browser kills, bounded replacement, paused
    restart, stale/interrupted lease recovery, and interrupted acquisition all passed.
  - Changing the environment backend did not migrate either persisted run. Backend
    change succeeded only after Stop & Reset and new-run creation. New Patchright run
    and session provenance remained explicit; no unexpected Queue ID changed.
  - Patchright shutdown took 0.216 seconds and left zero contexts, browser processes,
    leases, or operator work. Chrome remains the default pending final Phase 7
    acceptance. Staging is **NOT RUN / UNKNOWN**. Next is Phase 7 Prompt 5.

- **Phase 7 Prompt 3 is complete:** Patchright passes the controlled local
  park/destroy/reopen Queue-identity gate with the existing temporary-context
  architecture (`docs/phase7_patchright_identity_strategy.md`).
  - The production creator/restorer path completed 20/20 Patchright park/reopen cycles,
    explicit transfer and storage restoration, HYBRID transfer-first fallback, state
    refresh, a full managed browser-process restart, and a repository/state-store object
    restart. A 5/5 Chrome control passed the same core lifecycle.
  - Missing, corrupt, and unavailable state; transfer failure; identity mismatch;
    backend mismatch; context creation failure; and navigation timeout all retained the
    persisted expected Queue ID and created no replacement identity. A final restore of
    the same row succeeded.
  - All 33 Patchright cleanup checks passed; final active contexts, managed processes,
    and new child Chrome processes were zero. Persistent user-data profiles are not
    needed and were not adopted.
  - Evidence is local simulator only. Staging is **NOT RUN / UNKNOWN**. Prompt 4
    subsequently completed the full runtime/dashboard workflow.

- **Phase 7 Prompt 2 is complete:** Patchright 1.63.0 is integrated behind the existing
  `BrowserBackend` / `BrowserManager` seam (`docs/phase7_patchright_backend_integration.md`).
  `BROWSER_BACKEND=chrome|camoufox|patchright` is supported, while Chrome remains the
  default for new runs.
  - `PatchrightBackend` owns a Patchright async controller and launches installed Google
    Chrome with `channel="chrome"`. BrowserManager still owns all bounded slots,
    contexts, shared capacity, deadlines, replacement, diagnostics, and shutdown.
  - Patchright uses ordinary concurrent temporary contexts; no Camoufox serialization
    was copied. Automatic, creation, and headed/manual pools select it through the same
    factory.
  - New Patchright setup runs a one-context local `data:` preflight before persistence.
    Failure creates no run and gives an explicit Chrome install remedy. A successful run
    records `browser_backend=patchright` and the observed Chrome build; its sessions
    record Patchright provenance.
  - Existing Chrome, Camoufox, and Patchright runs restart with persisted provenance.
    Legacy rows remain Chrome, mismatch fails before context creation, and switching
    still requires Stop & Reset.
  - Patchright-specific Queue identity restoration was subsequently proven locally in
    Prompt 3. Camoufox is retained but excluded from Phase 7 validation. No Queue-it
    traffic was sent; staging is **NOT RUN / UNKNOWN**.
- **Phase 7 Prompt 1 is complete:** `PATCHRIGHT_READY_FOR_INTEGRATION` on local-only
  evidence (`docs/phase7_patchright_readiness.md`). Patchright 1.63.0 is pinned beside
  Playwright 1.62.0 and Camoufox 0.5.6 without resolver conflicts. Its async API launched
  installed Google Chrome 153.0.8010.54 through `channel="chrome"`, completed 20
  disposable-context/`data:` navigation cycles, and left zero contexts or new managed
  Chrome processes. No Patchright browser download is needed for this path.
  - At the Prompt 1 checkpoint, Patchright was not yet runtime-integrated; Prompt 2
    subsequently completed that work as recorded above.
  - Chrome is again the default for **new** runs. Existing persisted Chrome or Camoufox
    runs continue with their recorded backend; switching still requires Stop & Reset.
  - Camoufox is retained intact as dormant/experimental and is excluded from Phase 7
    benchmark/acceptance work.
  - Upstream recommends a persistent Chrome context as its best-practice configuration,
    but normal temporary contexts are supported by the API and passed locally. Queue
    identity restoration is deferred to Prompt 3. Staging is **NOT RUN / UNKNOWN**.
- **Phase 6 is complete** (Prompt 7 acceptance, `docs/phase6_acceptance.md`).
  Camoufox is accepted as a supported backend and as the default for new runs, on
  local simulator evidence. Chrome is retained as a tested fallback. The Queue-it
  staging boundary remains UNKNOWN / NOT RUN.
  - **Final validation:**
    - 513 non-staging tests; Ruff, strict mypy, `pip check`, and the preflight pass;
    - Camoufox headed controlled workflow 63/63 and Chrome 63/63;
    - full benchmark 22/22 scenarios and 282/282 checks with 0 leftover processes;
    - 0 Queue ID changes or replacements.
  - **Defects found and fixed during acceptance:** the first acceptance run hung
    application shutdown for more than 20 minutes (Camoufox shutdown under load).
    - Playwright 1.62 turns a single cancellation into an unbounded abort-ack wait.
      `utils.asyncio_tools.await_bounded` re-cancels or abandons, and now bounds every
      browser call.
    - A stuck `Playwright.stop()` kills the driver as a last resort.
    - The Prompt 4 lease released at close *start* allowed the create/close overlap that
      wedges Camoufox. The lease is now held until the close completes, and a close
      timeout marks the process for replacement.
    - Scheduler shutdown no longer lets externally cancelled workers abort ordered
      shutdown.
    - After the fixes, a 10-run manual/pause/shutdown loop (60 scenario runs) had 0 hangs
      or failures. Camoufox shutdown under load took 10.4–20.5 s.
- Phase 6 Prompt 6 (Camoufox default migration and operational polish) is complete.
  **Camoufox is now the default browser backend for NEW runs**
  (`BROWSER_BACKEND=camoufox`), and Chrome remains a supported, regression-tested
  fallback (`BROWSER_BACKEND=chrome`). See `docs/phase6_operational_migration.md`.
  - **Decision basis:** every default gate passed on local simulator evidence: creation,
    park and repeated reopen, Queue ID verification, transfer and same-backend storage
    restoration, monitoring, Refresh, Add, Replace, Delete, headed Open/Close,
    pause/resume, restart, crash recovery, Stop & Reset, clean shutdown, bounded
    resources, and zero silent Queue ID replacement. Fingerprint replay is explicitly
    not a gate. Staging remains UNKNOWN.
  - **Existing runs keep their backend.** Runs restart with the persisted
    `run_config.browser_backend`; changing the default never migrates a run. Legacy
    unprovenanced rows are backfilled as Chrome. Switching backends requires Stop &
    Reset Run.
  - **New-run preflight:** creating a Camoufox run runs `run_camoufox_preflight()`
    (`browser/preflight.py`). It checks the exact package pins, the installed pinned
    build, one launch/context/`data:` page, and cleanup. On failure nothing is
    persisted, and HTTP 503 names `camoufox fetch official/stable/152.0.4-beta.30` or
    the Chrome fallback. Runtime never downloads a browser.
  - **Build provenance:** `run_config.browser_build` records the pinned build (NULL for
    Chrome and legacy runs). A changed build is logged as `run_browser_build_changed`
    and shown in run info, never applied silently.
  - **Unresponsive-process restart:** 3 consecutive navigation timeouts on a connected
    Camoufox process restart that slot (`browser_unresponsive_restarts_total`). This
    recovered the Prompt 5 wedge case from 0/30 to 10/10 sessions verified with 0
    leaked processes. Chrome keeps disconnect-only recovery.
  - **Diagnostics:** `browser_backend_info{backend,browser_build}` is one series.
  - **Validation:**
    - Camoufox headed controlled workflow 63/63 (now including a `SIGKILL`
      browser-crash step), and Chrome regression 63/63;
    - Prompt 5 recovery scenarios re-run on both backends;
    - 505 non-staging tests; Ruff and mypy pass.
- Phase 6 Prompt 5 (Camoufox Queue-session recovery and capacity benchmark) is
  complete. At that time Camoufox was an opt-in backend, and the default decision was
  made in Prompt 6. See `docs/phase6_camoufox_benchmark.md` and
  `docs/results/phase6_camoufox_benchmark_result.json`. All results are local
  simulator only.
  - **Result:** 22/22 scenarios and 282/282 checks passed across Camoufox and a
    same-host Chrome comparison, with 0 Queue ID changes or replacements.
  - **Queue ID continuity:** held across 200 park/reopen restores, 3 full
    browser-process restarts, an application restart with stranded leases, 10+ real
    `SIGKILL`s including kills mid-sweep, and every restoration fault case.
  - **Headed manual:** Close, window loss, process kill, shutdown, and reopen all pass.
    So do pause under failure and shutdown with automatic, running and queued
    operator, and headed work in flight.
  - **Serialization is required.** Serialized Camoufox (one live context per process,
    1–4 processes) had 0 failures. Without serialization:
    - create/navigate/close churn wedged a Camoufox process at 5 contexts (0/30
      restores), while it still reported connected;
    - a 30-context hold across 2 processes lost 75/90 navigations.

    That family stopped on this evidence; 40/50 were not run. Chrome passed every level
    to 50 contexts.
  - **Capacity consequence:** automatic Camoufox concurrency equals
    `CHROME_PROCESS_COUNT` (≤ 4). Locally, Camoufox restores took about 2.5–3× Chrome's
    latency, with higher per-context process-tree CPU and RSS.
  - **Prompt 5 fixes:**
    - process accounting is now browser-neutral;
    - manual `inspect_open`/`adopt_open` are deadline-bounded (an observed hang had
      blocked shutdown indefinitely);
    - the manual Camoufox pool uses one bounded process per window
      (`manual_pool_topology`). The unserialized manual pool from Prompt 4 failed
      concurrent-Open churn in 3/5 runs.
  - **Open for Prompt 6:**
    - there is no health watchdog for a wedged-but-connected Camoufox process;
    - Camoufox shutdown under load took 13.8 s against a 10 s per-stage timeout;
    - staging behavior is UNKNOWN.
- Phase 6 Prompt 4 runtime integration is complete. See
  `docs/phase6_camoufox_context_strategy.md` and `docs/phase6_runtime_integration.md`.
  - The current released package is Camoufox 0.5.6 (`Python >=3.10,<4.0`) and it
    requires `playwright<1.63`. The project deliberately changed its prior Playwright
    1.63.0 environment/declaration to exact Playwright 1.62.0 and added exact Camoufox
    0.5.6. `pip check` passes; no dependency constraints were bypassed.
  - The exact locally validated browser is official stable 152.0.4-beta.30. Current
    upstream also lists beta.31, but Quetty pins beta.30 because upstream explicitly
    records Playwright 1.61/1.62 compatibility for it and the local preflight passes.
    Setup is explicit with
    `camoufox fetch official/stable/152.0.4-beta.30`; runtime never downloads it.
  - Queue ID is the authoritative persisted external journey identity. Transfer URL
    and same-backend HYBRID storage state are restoration mechanisms. BrowserContexts
    are temporary, and Camoufox fingerprint/device properties may change after parking.
  - Stable complete fingerprint replay remains locally disproved. Across 20
    `AsyncNewContext(preset=same_dict)` cycles, core preset fields stayed fixed but
    observable canvas identity changed; a disk JSON round trip and full
    Playwright/Camoufox runtime restart also changed identity. Generated config/init
    scripts remain rejected persistence inputs. This is explicitly **not required** for
    Queue-session continuity and no longer blocks Phase 6.
  - Public `launch_options(env={})` replay through `from_options=` was deterministic
    across browser launches, but is process-scoped, installation-specific, and encodes
    identity in Camoufox-owned environment values. It would require a keyed browser
    process per concurrently active identity and is rejected as the persisted context
    strategy.
  - The actual local storage-state matrix is Camoufox→Camoufox PASS,
    Chrome→Camoufox PASS, Camoufox→Chrome FAIL (local storage restored but its insecure
    `SameSite=None` cookie was rejected), and Chrome→Chrome PASS. Queue-it
    cross-engine continuity remains UNKNOWN because no staging traffic was sent.
    Existing/legacy unprovenanced state remains Chrome/Chromium and is never silently
    migrated.
  - `run_config.browser_backend` and `queue_sessions.browser_backend` persist the
    minimum provenance. Legacy schema migration defaults to Chrome, restart uses the
    persisted run backend rather than a changed environment, and run/session mismatch
    fails before context creation and appears in the report-only consistency checker.
  - The browser seam is implemented: `BrowserManager` keeps slots, capacity, shared
    headed/automatic limits, bounded restart tasks, cleanup, and metrics;
    `ChromeBackend`/`CamoufoxBackend` delegate launch, context, connectivity, close,
    and diagnostics. Chrome is still the default via typed
    `BROWSER_BACKEND=chrome|camoufox`.
  - `queue-load-test-camoufox-preflight` sends no network traffic beyond a local
    `data:` URL and passed with package 0.5.6, Playwright 1.62.0, browser beta.30, one
    context, zero contexts after close, zero managed processes after shutdown, and no
    residual Camoufox OS process.
  - No fingerprint descriptor or format version is persisted. A production-path test
    passed 20/20 fresh-context restore/inspect/state-refresh/close cycles, including a
    full Camoufox process and repository restart. An intentional mismatch preserved the
    expected Queue ID.
  - Camoufox 0.5.6 repeated concurrent navigation waves on one process were unreliable
    locally. `CamoufoxBackend` therefore serializes live contexts per managed process,
    and the manual Camoufox pool uses one process per window (Prompt 5);
    fixed workers, the shared global coordinator, bounded processes, and all operation
    deadlines remain in force.
  - The complete controlled local application workflow passes on both Chrome and
    Camoufox, including setup/deficit acquisition, monitoring, pause/resume, Refresh,
    Add, create-first Replace, Delete, manual Open/Close/window loss, no-ID adoption,
    duplicate adoption rejection, restart, mismatch preservation, and Stop & Reset.
- Phase 5 remains complete. `docs/phase5_acceptance.md` records
  **PARTIAL: 108 PASS, 0 FAIL, 0 UNKNOWN** on local evidence. Eight separately listed
  Queue-it staging items (S1–S8) are **NOT RUN / UNKNOWN**; no Queue-it traffic was
  ever sent.
- **UI architecture:** localhost FastAPI + Jinja2 + vendored HTMX
  (`queue-load-test-ui`, `http://127.0.0.1:8000`). `ApplicationRunRuntime` composes the
  existing creation controller, scheduler/monitor/lifecycle evaluator, restorer,
  repository, and state store. It adds a fixed-size operator action pool and a bounded
  headed-browser manager. There is one process and one SQLite database, guarded by a
  single-instance `<database>.lock`.
- **Startup/run config:** first boot shows setup (protected staging URL plus requested
  count), then persists one immutable `run_config` row. A restart skips setup and
  resumes only the deficit to `requested + operator adjustment`. Existing identities
  are never retargeted. **Stop & Reset Run** on the dashboard starts over instead (see
  below). The setup page warns that the target URL is
  the protected destination: reaching it is `ADMITTED`, so the waiting-room URL must
  not be entered.
- **Pause/resume:** a single persisted `runtime_control` flag gates automatic claims.
  In-flight checks finish, the backlog stays visible, and the state survives restart.
  Acquisition, Refresh Now, and headed sessions are independent of it.
- **Manual browser:** Open restores the expected identity into a bounded headed pool
  under a persisted renewable lease that the scheduler skips. Close, window close, or a
  crash releases the lease, with a final evaluator pass when the page is still live.
  Restart clears stale ownership. `OPEN_IN_CHROME` is browser ownership, never
  `QueueStatus`.
- **Manual Open without a Queue ID:** rows without a Queue ID (failed creations:
  `FAILED`, empty transfer URL) now open at the run's protected staging URL, using
  HYBRID state if any. They no longer fail with `EXPECTED_IDENTITY_MISSING`. Opening
  records nothing.
  - **Adoption:** on each heartbeat and at close, `QueueSessionRestorer.adopt_open`
    looks for a live queue with a transfer identity. The manager then persists it the
    same way creation does: `FAILED → CREATING`, where the unique `queue_id` rejects a
    duplicate, then `PARKED` and due with the observed progress and HYBRID state. From
    then on the normal verified inspection applies.
  - **No ID or a duplicate:** closing before an ID appears leaves the row unchanged. A
    duplicate ID is logged (`manual_identity_adoption_conflict`), the newly saved state
    is discarded, and the row stays unchanged.
- **Stop & Reset Run:** `POST /run/reset` (with a confirmation dialog) resets the app
  to a fresh state.
  - **Order:** `ApplicationRunRuntime.reset()` runs the existing ordered `close()`,
    then discards the runtime components. Next, `SQLiteSessionRepository.reset_all()`
    deletes `run_config`, `queue_sessions` and `queue_progress` and resets
    `runtime_control` in one transaction. Finally `FileSystemStateStore.clear()`
    deletes the state documents and abandoned temporary writes.
  - **After a reset:** the UI stays up and redirects to `/setup` (`HX-Redirect`), and a
    restart also opens on setup. Nothing is cancelled at Queue-it.
  - **Failures and concurrency:** if the wipe fails, nothing is deleted and the run is
    restarted. Other mutations and a second reset are refused while a reset runs.
- **Operator actions:**
  - Refresh Now runs the existing monitor, including while paused.
  - Delete is local only (no Queue-it cancellation) and requires Close first.
  - Replace is create-first, preserving the old identity on failure.
  - Add creates exactly one visitor.
  - All actions are fenced per session, deduplicated by render token, and bounded by
    `OPERATOR_WORKERS`/`OPERATOR_QUEUE_CAPACITY`.
- **Acceptance evidence:** the backend-parameterized controlled workflow passed 61/61
  checks with the real app on installed Chrome and 61/61 on Camoufox against the local
  simulator. The real CLI was checked for default
  localhost bind, a Playwright double-click, SIGKILL-with-headed-window recovery, a
  refused second instance, and clean SIGTERM.
- **Current checks:** Prompt 4 focused local workflow and continuity tests pass; full
  non-staging suite 483 passed (4 staging deselected); Ruff and strict mypy pass;
  staging was not run. Earlier win32 mypy evidence
  reported 2
  pre-existing `signal.SIGKILL` errors in `harness/phase4_recovery.py`.
- **Known limitations:**
  - no staging validation;
  - harness CLIs do not take the instance lock;
  - action banners are process-local;
  - automatic checks do not renew their lease;
  - Open is synchronous;
  - no in-place retargeting (Stop & Reset Run wipes the database and starts over);
  - adoption is locally exercised through both installed backends and the simulator,
    but has not been exercised against a real Queue-it page.
- Phase 5 Prompt 5 (history): the localhost operator UI was hardened for
  refresh/double-click/restart/Chrome-crash/database-failure/shutdown. See
  `docs/phase5_ui_reliability.md`.
- Twelve defects were fixed:
  - The UI runtime no longer replaces uvicorn's SIGINT/SIGTERM handlers. Before, Ctrl+C
    stopped the runtime but left the web server running.
  - Shutdown is ordered, with a bounded drain for operator work.
  - Startup clears stale headed ownership and leases under a single-instance lock.
  - Operator and headed acquisition take over expired leases.
  - Lifecycle `CHECKING` and expired leases no longer appear as browser ownership.
  - HTMX poll/mutation races and double-submits are fixed (`hx-sync`,
    `hx-disabled-elt`, OOB feedback, per-render request tokens).
  - Add/Replace population accounting is crash-consistent.
  - Web failures are contained as sanitized 503 fragments.
  - The headed heartbeat is non-repairing and tolerates renewal errors.
  - The dashboard page query uses an ordering index.
  - HTMX is vendored.
- Ownership model: per-session persisted, fenced ownership in the row. There is exactly
  one of an automatic lease, an operator lease (taken before queueing), or a headed
  lease; there is no global lock. Live ownership is never overlapped. Expired ownership
  may be taken over, and stale writers are rejected by owner id. `queue-load-test-ui`
  holds an OS lock on `<database>.lock`, so startup treats every persisted owner as dead
  and clears it without touching identities.
- Shutdown order:
  1. reject new mutations;
  2. stop creation and claims;
  3. drain automatic checks;
  4. finish or cancel operator work within `SHUTDOWN_TIMEOUT_SECONDS`;
  5. final headed inspection, then close contexts and release ownership;
  6. close Chrome and SQLite.

  A real SIGINT exited in about 0.5 s with no Chrome processes and zero owners.
- 10,000-row synthetic dashboard (Apple M5 Pro, SQLite 3.50.4; not Queue-it evidence):
  - Page query p95: first 0.22 ms, last 1.22 ms.
  - Search p95: 1.9–2.1 ms. Status filter p95: 0.63 ms. Summary p95: 6.6 ms.
  - 2–4 statements per request.
  - Zero writes while polling; pause/resume write one row each.
  - No browser work and no task growth.
- Phase 5 Prompt 4 summary: a localhost FastAPI/Jinja2/HTMX operator UI provides
  persisted first-run setup, bounded acquisition/runtime startup, run recovery,
  aggregate status, and a safe paginated session dashboard.
- The SQLite schema now includes one immutable current `run_config` row. The run target
  and requested session count survive restart; the existing creation controller counts
  valid Queue IDs and resumes only the deficit. Setup refuses a database with legacy
  sessions but no run target, preventing unsafe identity retargeting.
  `reset_all()` deletes the run and every session, so setup can start a new run.
- Dashboard rows use a joined `LIMIT`/`OFFSET` projection of non-sensitive fields and a
  separate count query. The 50-row page works with 10,000 persisted sessions without
  calling `list()` or materializing the population. Search, lifecycle filtering,
  browser-ownership filtering, and two-second HTMX partial refresh are implemented.
- Automatic monitoring pause is persisted as the singleton `runtime_control` row and
  never written into `QueueStatus` or every session. SQLite claims atomically return no
  work while paused. The scheduler lets checks already classified as in flight finish,
  releases claimed-but-not-started leases, and wakes promptly on local resume without
  resetting `next_check_at` or materializing the due population.
- Browser ownership is represented by `BrowserRuntimeState` (`PARKED`, `CHECKING`, and
  `OPEN_IN_CHROME`) and remains separate from `QueueStatus`. Manual opens use persisted
  renewable ownership leases that atomically exclude automatic scheduler claims.
- Manual sessions use one lazily started headed pool, capped by
  `MAX_MANUAL_OPEN_SESSIONS` (default 5), and share the existing global context budget
  with headless automatic work.
  - Chrome shares one headed process between windows.
  - Camoufox uses a fixed pool of up to `MAX_MANUAL_OPEN_SESSIONS` processes with one
    window each (Phase 6 Prompt 5).
  - Neither launches one process per persisted row.
- Page/context/window closure, Chrome loss, explicit Close, and application shutdown
  release ownership. A still-live page is inspected through the existing extractor and
  lifecycle evaluator, with progress/scheduling and HYBRID state refreshed where
  possible; an already-lost page preserves its last persisted state.
- Refresh/Delete/Replace/Add requests run through a fixed operator worker pool and
  bounded queue. Per-session requests acquire fenced ownership before queueing,
  preventing scheduler/open races; manual refresh calls the existing monitor even while
  global automatic monitoring is paused.
- Delete requires Close first and removes the row, cascading progress, leases, transfer
  identity, and local state. Replace creates/persists a unique visitor before deleting
  the old row, so failed/duplicate acquisition leaves the old identity intact. Add
  creates exactly one independent visitor through the normal creator.
- `requested_sessions` remains the immutable initial target. A persisted signed
  operator population adjustment prevents restart acquisition from refilling a manual
  Delete or discounting a manual Add; Replace does not change it.
- Exact next task: **authorised Queue-it staging validation of the Patchright default** (Phase 7 complete)
- Current phase: Phase 4 is complete through the final acceptance report. Both the
  authorised 10,000-ID acquisition and browser-backed 10,000-session Queue-it
  monitoring run are **NOT RUN**.
- Last completed work: the final Phase 4 acceptance report,
  `docs/phase4_acceptance.md`.
- Phase 4 acceptance is **PARTIAL: 24 PASS, 0 FAIL, 16 UNKNOWN**. The bounded
  10,000-row persistence/scheduler architecture, local installed-Chrome recovery,
  SQLite, state storage, leasing, restart, and observability mechanisms passed. The
  end-to-end target remains UNKNOWN because 10,000 real Queue-it identities were not
  acquired or monitored.
- Phase 4 recovery: 15 controlled scenarios over 10,000 persisted sessions **all PASS as
  local evidence** (real SQLite/state files, SIGKILLed Chrome and worker processes,
  installed Chrome against a local simulator, no staging traffic). Distributed
  worker/node loss and real Queue-it recovery are **UNKNOWN**. Queue IDs were unchanged,
  only the 40 deliberate permanent failures became FAILED, no replacements were created,
  and no context, Chrome process, or scheduler lease leaked after any shutdown.
- Recovery measurements (local host): startup summary p95 6.9 ms, first claim p95 7.4
  ms, Chrome restart p50/max 1.16/1.53 s, 4,934 resumed browser checks at 12.5 checks/s,
  killed-worker leases recovered 10.16 s after kill with a 10 s lease, database outage
  resume 0.109 s.
- Prompt 7 fixed six real defects: hung Playwright calls after a Chrome kill (now
  deadline-bounded), a context-capacity leak on forced shutdown, transient state I/O
  errors becoming permanent FAILED identities, a refresh failure triggering redundant
  fallback navigation, cancellation-unsafe SQLite/state writes, and a scheduler/shutdown
  that stopped on database errors. `IDENTITY_REPLACEMENT_LIMIT` (default 0) now blocks
  mass replacement of identities that fail after acquisition.
- Acceptance result: **20 PASS, 1 FAIL, 11 UNKNOWN**. Local configuration,
  bounded-concurrency, SQLite query/lease/scheduler, state storage, restart recovery,
  and short installed-Chrome capacity mechanisms are supported by evidence. No Phase 3
  Queue-it staging benchmark ran, so real acquisition, restore, monitoring, and identity
  reliability remain unproven.
- Phase 2 acceptance remains **3 PASS, 2 FAIL, 15 UNKNOWN**. The missing measurements
  are carried as explicit blockers, not converted into Phase 3 scalability claims.
- Phase 4 monitoring: two actual local synthetic 10,000-row sweeps used 20 fixed
  workers, a 50-item queue, and 50-row claims. They drained 10,000 to zero in
  76.595/92.241 seconds (130.56/108.41 synthetic checks/s), with zero failures or lease
  conflicts. An adaptive production-policy simulation checked all 10,000, peaked at a
  2,885-row backlog and 26.599-second oldest-overdue age, then drained to zero. This
  supersedes the earlier linear projection but remains scheduler/SQLite evidence only;
  real browser-backed checks/s and cadence are UNKNOWN.
- During the monitoring run, application CPU averaged 59.00% and peaked at 81.8%; RSS
  averaged 68.08 MB and peaked at 76.54 MB. No Chrome process was launched, so Chrome,
  restore, navigation, identity, and context measurements remain UNKNOWN.
- Persistence decision: a 10,000-row local SQLite benchmark retained the ordered index
  and measured 0.940/0.999 ms due-count p50/p95, 0.538/0.691 ms claim-50, and
  1.550/1.667 ms scheduler-iteration latency. PostgreSQL remains optional and deferred;
  sustained SQLite writes and distributed PostgreSQL leasing remain UNKNOWN.
- A 2026-09-27 recheck on the current Windows/NTFS host repeated the 10,000-row
  benchmark twice. Due-count p95 was 4.309/4.601 ms, claim-50 p95 4.866/4.898 ms,
  update p95 3.326/3.971 ms, release p95 3.124/2.973 ms, and scheduler-iteration p95
  9.503/9.263 ms. The ordered index remained selected with no temporary sort. Seeding
  10,000 separately committed rows took about 33.39 seconds (~299 rows/s), highlighting
  slower durable commits but not a measured PostgreSQL trigger. No local project
  database exists in this checkout, so no on-machine migration is required.
- Lease updates are now owner-fenced. A stale worker cannot overwrite a row after an
  expired lease is reclaimed, and an unleased stale snapshot cannot clear a new lease.
- State storage decision: Phase 4 stays single-machine, so `FileSystemStateStore`
  remains the only state store and shared/object storage is deferred. At 10,000
  synthetic files it measured save p50/p95 0.179/0.252 ms, load 0.063/0.076 ms,
  about 7,600 saves/s at 20-way concurrency, 14.16 MB logical / 40.96 MB allocated,
  and a 0.41–0.45 s report-only consistency audit with zero findings.
- State files now embed `session_id` and a SHA-256 digest. Loads reject another
  session's document and detect tampering; older plain files still load and are
  counted as legacy by the audit.
- Distribution decision: **deferred; Phase 4 remains single-machine**. There is no
  measurement showing that one host misses a required creation or monitoring cadence,
  so the project rule forbids adding distributed execution. This is not proof that one
  host is sufficient: real Queue-it creation/check cadence, sustained CPU/RAM, Chrome
  stability, and network/page latency remain UNKNOWN.
- The design retains distribution-ready boundaries—repository protocol, unique
  scheduler ownership, bounded claims/queues, persisted leases, lease-expiry recovery,
  and owner fencing—but has no PostgreSQL backend, shared state store, cross-node state
  write fencing, or multi-controller target reservation. Distributed correctness is
  therefore not claimed.
- Phase 4 acquisition result: **NOT RUN / UNKNOWN**. No authorised staging URL or run
  gates were configured. A no-navigation local preflight proved SQLite/state writes,
  two-process installed-Chrome launch, one-context cleanup to zero, metrics, disk, and
  recovery mechanics using temporary data, but it did not contact Queue-it.
- The Phase 4 acquisition runner is resume-safe, enforces at most two Chrome processes,
  25 contexts per process, 50 globally, and 10 creation workers/queue slots, and emits
  aggregate JSON with post-run identity count, context, lease, and state-consistency
  verification. Transfer URLs and Queue IDs are absent from the report.
- Next work is evidence collection rather than new scaling architecture: run the gated
  authorised acquisition, restoration, lifecycle, and monitoring benchmarks to close
  the acceptance report's 16 UNKNOWN results.

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
- Local operator UI and runtime assembly:
  `src/queue_load_test/web/`.
- Single-UI-process database lock: `src/queue_load_test/utils/instance_lock.py`.
- Phase 5 10,000-row dashboard benchmark:
  `src/queue_load_test/harness/phase5_ui_benchmark.py`.
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
- Opt-in, bounded browser-network discovery and protected raw evidence:
  `src/queue_load_test/status_discovery/`.
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
`lease_until`, `manual_owner_id`, and `manual_lease_until`.

`QueueProgress` holds optional layout-dependent observations: `queue_number`,
`users_ahead`, `progress_percentage`, `estimated_wait_text`, `expected_service_time`,
`last_updated_at`, `queue_paused`, `first_in_line`, `serviced_soon`, `turn_started`,
`connection_lost`, `pre_queue`, `active_queue`, `manual_update_warning`, and compact
extraction diagnostics. All Queue-it layout fields can be `None`.

`RunConfig` holds the immutable current `run_id`, sensitive-in-logs `target_url`,
`requested_sessions`, independent `browser_backend`/`browser_build` provenance,
`monitoring_strategy`, `created_at`, and `ACTIVE` run status. `SessionSummary` is a
safe dashboard projection and never contains transfer URLs or browser-state paths.

Modes are `HYBRID` and `TRANSFER_ONLY`. Statuses are `NEW`, `CREATING`, `PRE_QUEUE`,
`ACTIVE_QUEUE`, `PARKED`, `CHECKING`, `PAUSED`, `SERVICED_SOON`, `TURN_STARTED`,
`READY`, `ADMITTED`, `CONNECTION_LOST`, `EXPIRED`, and `FAILED`.

## Persistence

- `SQLiteSessionRepository` uses the path from `DATABASE_URL`; the default is
  `sqlite:///queue_load_test.sqlite3`.
- The repository protocol exposes initialization, create, update, get, list,
  successful-ID count, progress operations, aggregate recovery summary, eligible
  due-session count, bounded due-session claims, lease release, and close. This is the
  boundary intended to permit a future PostgreSQL implementation.
- SQLite has separate `queue_sessions` and `queue_progress` tables, a unique nullable
  `queue_id`, and lightweight `worker_id`/`lease_until` fields. The due-session query
  uses a partial ordered expression index on
  `COALESCE(next_check_at, created_at), created_at, session_id` for monitorable states.
  Existing Phase 2 indexes are migrated in place during initialization.
- SQLite also has a singleton `run_config` table. Setup is insert-only; target, browser
  backend, and monitoring strategy are immutable provenance. Legacy strategy-less rows
  migrate to `headed_window`, their historical behavior.
- SQLite has a singleton `runtime_control` table containing `monitoring_paused` and the
  signed `operator_population_adjustment`. Both O(1) values survive restart; the claim
  transaction checks pause before selecting rows and startup applies the population
  adjustment without mutating immutable `requested_sessions`.
- Manual browser ownership is stored on the session as a separately fenced owner/lease.
  Manual acquisition rejects an automatic owner and enforces the manual-open limit in
  one `BEGIN IMMEDIATE` transaction. Due claims exclude live manual leases and can
  reclaim a row after that ownership expires.
- Successful-ID counting excludes `FAILED` rows. Duplicate non-null Queue IDs raise
  `QueueIdConflictError`.
- Updates from leased snapshots are conditional on the persisted `worker_id`. A stale
  owner raises `LeaseOwnershipError` after another worker reclaims an expired lease,
  preventing late browser work from overwriting newer ownership or session data.
- `FileSystemStateStore` defaults to `.browser-state/<session_id>.json`, validates safe
  session IDs, writes a temporary file, flushes/fsyncs it, applies restrictive file
  permissions, atomically replaces the destination, and fsyncs the directory. Save,
  load, and delete work is dispatched with `asyncio.to_thread`, so filesystem
  operations do not execute directly on the event loop.
- State documents are an envelope of `format`, `version`, `session_id`, `sha256`, and
  `state`. `load()` raises `StateSessionMismatchError` for another session's document,
  `StateCorruptError` for invalid JSON, format, or digest, and `StateUnreadableError`
  for I/O or permission failures (all `StateStoreError`). Plain pre-envelope
  `storage_state` files still load as legacy documents and are rewritten on the next
  HYBRID refresh. Save is idempotent and safe to retry; the store has no internal retry
  loop.
- `StateConsistencyChecker` performs an explicitly non-mutating database/filesystem
  audit in a worker thread. It reports missing, orphaned, corrupt, unreadable,
  mismatched, duplicate/conflicting, insecure-permission, and stale temporary state,
  plus a legacy-file count; cleanup is never automatic. Its optional recovery summary
  adds missing and unusable (corrupt, unreadable, or mismatched) counts to the database
  aggregates, while normal startup deliberately avoids this full directory scan.
- SQLite files, `.browser-state/`, and generated Phase 1 JSON reports are git-ignored.

## Browser Model

The Phase 3 readiness defaults are two Chrome processes, 25 contexts per browser, and
a 50-context global ceiling, with only one creation and one monitoring worker enabled
by default. The controlled local benchmark exercised 2/3/4 Chrome processes at 25
contexts each for 50/75/100 global ceilings. All three short in-memory-page cases
completed with exact per-process accounting, zero recorded failures, and full cleanup.
This is local installed-Chrome evidence, not a safe operating-point or Queue-it staging
claim; the current configuration rejects ceilings above 100. `TARGET_QUEUE_IDS=10000`
validates with the existing bounded process/context/worker/queue/claim/lease settings,
while the checked-in default remains 1,000. `BrowserManager` selects the least-loaded
connected process and rejects allocations above either capacity. `ChromeBackend`
preserves Chromium launch with `channel="chrome"`; `CamoufoxBackend` uses public async
`AsyncNewBrowser` and `AsyncNewContext` with exact installed browser beta.30. Browser
slot IDs remain stable when only a failed process is replaced. The manager reports
per-slot and aggregate capacity, and a context exposes the ID of its owning slot for
diagnostics.

A fresh context is created without storage state; state is supplied only for explicit
restoration. `OwnedBrowserContext`, the manager's async context manager, idempotent
close, and shutdown paths ensure tracked contexts are released. Capacity calls detect
disconnected browsers; restart immediately removes and invalidates lost contexts,
relaunches only the failed slot, preserves persisted session identity outside the
browser, and updates metrics. Relaunch runs outside the global manager lock, so a
healthy process can continue accepting contexts while another process restarts.

Phase 5 manual opens use a separate `headless=False`, `channel="chrome"` manager because
headed mode is a process launch option. It is one shared lazy process with up to five
contexts by default, not one process per session. A shared context-capacity coordinator
spans the headed and automatic managers so both count against `MAX_ACTIVE_CONTEXTS`.
Headed liveness checks use `capacity(repair=False)`. A crashed headed Chrome is
relaunched only by the next explicit Open, never by the heartbeat.

Queue ID acquisition (setup, Add, Replace) runs in visible Chrome by default:
`CREATION_HEADLESS=false`, independent of `HEADLESS=true` for monitoring. When the two
settings differ, `ApplicationRunRuntime` gives `QueueSessionCreator` its own manager:
one Chrome process with `CREATION_WORKERS + OPERATOR_WORKERS` contexts, sharing the
global context budget. `ApplicationRuntime` starts it after the automatic manager and
shuts it down just before. The creator closes its context once the Queue ID is
persisted, and headless monitoring then restores the session from its transfer URL or
state. A live context never moves between browsers. When the settings match, creation
reuses the automatic manager.

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

Manual Add and Replace call this same creator once from a fixed-size operator pool.
Failed action attempts are removed rather than shown as phantom managed sessions.
Replace deletes its leased old row only after the replacement has a valid persisted
identity. Delete and Add update the persisted population adjustment so later startup
does not undo explicit population changes.

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

`RunConfig.monitoring_strategy` selects one immutable run-level automatic-monitoring
strategy. `headed_window` is the existing browser restore/live-DOM path. `direct` is a
Phase 8 boundary and currently selects that same browser monitor as its fallback. When
explicitly enabled for a Direct run, a bounded page-level observer records protected
diagnostic evidence from requests the visitor page actually makes and correlates JSON
values with the final DOM extraction. It never supplies a monitoring result or replay.
The scheduler, leases, pause gate, manual/operator paths, and backend provenance are
unchanged.

The Phase 8 direct replay code is a separate opt-in harness, not a monitor selected by
`RunConfig`. It requires the Direct provenance only as an experiment gate. Normal
automatic checks for Direct runs still execute the same browser restore/live-DOM path.
The harness cannot create a URL or Queue ID: its recipe, session, persisted Queue ID,
and integrity-checked browser state must all match. Any refreshed HTTP cookies remain
in separate protected experimental state and never rewrite the browser-state document.

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

The Phase 4 benchmark in `src/queue_load_test/harness/phase4_monitoring.py` scales the
same bounded architecture to 10,000 synthetic rows and keeps deliberate all-due sweeps
separate from adaptive scheduling. The adaptive clock advances by measured batch time,
so due work can accumulate rather than being hidden while a batch drains. Its 10,000
scheduled rows had 9,801 distinct exact due times, a largest one-second bucket of 439,
a maximum backlog of 2,885, and a maximum oldest-overdue age of 26.599 seconds before
ending at zero. The fixed queue/workers peaked at 50/20. See
`docs/phase4_monitoring.md`; this is not a browser or Queue-it throughput result.

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

- Phase 4 additions: target/valid/remaining/lost Queue ID gauges, gauges for every
  lifecycle status, active/expired lease gauges, lease-recovery counter, checks/s and
  average-check gauges, worker configuration gauges, `repository_errors_total{operation}`,
  state-refresh failures, Chrome restart duration/failures/lost contexts, browser
  operation timeouts, startup recovery gauges, replacement-blocked gauge, and a fixed
  seven-range `queue_sessions_progress_bucket{bucket}` distribution. At 10,000 sessions
  the exposition had 242 series with only `bucket`, `operation`, and `le` labels.
  `docs/dashboards/queue_load_test_phase4.json` is an importable Grafana dashboard; no
  metrics stack is deployed. JSON logs redact every absolute URL and carry `run_id`.

- `JsonLogFormatter` emits timestamp, level, logger, message, and approved contextual
  fields (`session_id`, `queue_id`, status, worker/browser, attempt, restore method,
  duration, and error type). Transfer URLs are not approved log fields.
- `PrometheusMetrics` implements aggregate creation, lifecycle, browser, restore,
  identity, navigation, check, duration, and progress metrics without Queue ID or
  session ID labels. Creation telemetry includes attempts, acquired IDs, duplicates,
  transient/permanent failures, active workers, bounded queue depth, duration, and the
  current-run acquisition rate.
- Monitoring telemetry includes aggregate checks/rate/duration, active fixed workers,
  bounded queue depth, due/overdue unleased counts, oldest-overdue age, claimed
  sessions, and lease conflicts. Restore failures and identity mismatches remain
  separately counted. No Queue ID or session ID is used as a label.
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
- Latest result on 2026-09-27 (Windows): `python -m pytest -q` reported **362 passed,
  2 platform-specific skips, and 4 gated staging tests deselected in 383.87 seconds**.
  Ruff passes. Strict mypy currently reports two Windows-only `signal.SIGKILL`
  attribute errors in the Phase 4 recovery harness. The preceding macOS validation
  reported 364 passed, 4 deselected, and clean mypy.

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

- No real authorised staging run exists; all vendor-, theme-, event-timing-, and
  destination-specific acceptance remains unknown. Aggregate synthetic Prompt 6 data
  is checked in at `docs/results/phase4_monitoring_result.json`.
- Real transfer and storage-state restore rates, `lastUpdated` cadence, lifecycle timing,
  Queue-it CPU/RAM headroom, browser crash rate, and monitoring sweep time have not been
  measured.
- The synthetic 10,000-row scheduler drained all work, but the adaptive simulation
  reached a 2,885-row backlog and 26.599-second oldest-overdue age. Its lifecycle mix is
  not observed Queue-it data, and no numeric real-monitoring service level has been
  supplied. Single-machine live cadence therefore remains UNKNOWN.
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
  A shared store would need a URI/key instead of the `Path`-typed `state_path`,
  conditional (fenced) puts, and bounded transient-error retries; see
  `docs/phase4_state_storage_readiness.md` section 5.
- No failing ordinary tests or source TODO/FIXME markers were found during this handoff.
- Phase 2 acceptance is documented in `docs/phase2-acceptance.md`; the missing staging
  evidence is a blocker to selecting the Phase 3 50–100-context operating range.
- Runtime startup and `/status` now use aggregate recovery counts instead of loading
  all session rows. The Phase 2 restore harness still loads its full sample; an explicit
  state consistency audit also scans all rows/files. Their 10,000-row costs are not
  measured.
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
- HYBRID state saves are correctly offloaded to threads but perform a file fsync, atomic
  replacement, and directory fsync per session. Synthetic 20-way concurrency reached
  about 7,600 saves/s locally; real `storage_state` size, churn, and disk pressure under
  browser load remain unmeasured. No directory-wide scan occurs in the hot path.

## Important Files

- `README.md` — setup, scope, and harness usage.
- `.env.example` — Phase 3 readiness defaults and bounded queue/capacity settings.
- `pyproject.toml` — dependencies, scripts, pytest marker/default exclusion.
- `src/queue_load_test/config.py` — configuration and contradiction validation.
- `src/queue_load_test/models/` — identity/progress/lifecycle domain.
- `src/queue_load_test/browser/manager.py` — Chrome/context ownership and recovery.
- `src/queue_load_test/repository/sqlite.py` — persistence and leases.
- `src/queue_load_test/state/filesystem.py` — atomic, self-verifying JSON storage state.
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
- `docs/phase3-acceptance.md` — Phase 3 acceptance matrix and evidence limits.
- `docs/phase4_readiness.md` — Phase 4 capacity model, measured baselines, conditional
  persistence/distribution gates, and remaining unknowns.
- `docs/phase8_monitoring_strategy.md` — Phase 8 strategy values, persistence/migration
  rules, setup/dashboard behavior, browser fallback boundary, and Prompt 2 handoff.
- `docs/phase8_direct_replay.md` — Prompt 3 replay boundary, protected state,
  minimisation method, UNKNOWN findings, and Prompt 4 evidence gate.
- `docs/phase8_status_discovery.md` — protected network-observation mechanism,
  historical-versus-observed boundary, evidence questions, and Prompt 3 gate.
- `docs/phase4_postgresql_readiness.md` — 10,000-row SQLite results, repository and
  lease-fencing audit, PostgreSQL deferral, and future `SKIP LOCKED` design.
- `docs/phase4_state_storage_readiness.md` — 10,000-file local state results, state
  envelope and audit finding kinds, shared-storage deferral, and distribution
  prerequisites.
- `docs/phase4_distributed_worker_decision.md` — evidence-based single-machine decision,
  limiting-resource unknowns, distribution-readiness audit, and the gate for revisiting
  multi-node execution.
- `docs/phase4_acquisition.md` — gated 10,000-ID preflight/run/resume procedure, aggregate
  report fields, bounded profile, and current staging NOT RUN result.
- `docs/phase4_monitoring.md` and `docs/results/phase4_monitoring_result.json` — local
  10,000-row deliberate/adaptive scheduler measurements and the browser/staging UNKNOWN
  boundary.
- `docs/phase4_recovery.md` and `docs/results/phase4_recovery_result.json` — 15
  controlled 10,000-session recovery scenarios, defects fixed, measurements, and
  UNKNOWN boundary; `src/queue_load_test/harness/phase4_recovery.py` and
  `local_queue_simulator.py` are the harness and local page server.
- `docs/phase4_acceptance.md` — final Phase 4 matrix: 24 PASS, 0 FAIL, 16 UNKNOWN,
  with the bounded local evidence separated from unrun Queue-it staging claims.
- `docs/dashboards/queue_load_test_phase4.json` — Grafana dashboard definition.
- `src/queue_load_test/capacity.py` — pure theoretical-rate and observed-rate projection
  calculations with explicit utilization assumptions.
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

Phase 4 10,000-file state benchmark (dedicated empty paths; 20 concurrent operations):

```powershell
queue-load-test-phase3-storage --database phase4-storage-synthetic/sessions.sqlite3 --state-directory phase4-storage-synthetic/state --sessions 10000 --concurrency 20 --report phase4-storage-benchmark.json
```

Phase 4 acquisition preflight and authorised run/resume (use persistent dedicated
database/state paths and the bounded profile from `docs/phase4_acquisition.md`):

```powershell
queue-load-test-phase4-acquisition --preflight-only --preflight-report phase4-acquisition-preflight.json
$env:RUN_STAGING_TESTS = "1"
$env:RUN_PHASE4_ACQUISITION_BENCHMARK = "1"
queue-load-test-phase4-acquisition --confirm-authorized-staging --creation-timeout-seconds 86400 --report phase4-acquisition-benchmark.json
```

Local synthetic Phase 4 10,000-session monitoring benchmark (dedicated empty database):

```powershell
queue-load-test-phase4-monitoring --database phase4-monitoring-synthetic.sqlite3 --report phase4-monitoring-benchmark.json --population 10000 --workers 20 --queue-capacity 50 --batch-size 50 --sweeps 2 --check-delay-seconds 0.001 --sample-interval-seconds 1
```

Local controlled Phase 4 recovery scenarios (installed Chrome + local simulator; needs
the `benchmark` extra):

```powershell
queue-load-test-phase4-recovery --report phase4-recovery-benchmark.json
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

Phase 8 Prompt 3's experimental implementation is complete with conclusion
**UNKNOWN**. Next: run its explicit gates against genuine browser-observed evidence from
an authorised Queue-it staging event. **Phase 8 Prompt 4 — Direct-vs-Browser Observation
Equivalence** remains blocked until that evidence resolves the state-sufficiency
questions. Authorised Patchright staging validation also remains open; never claim
staging PASS without an authorised run.

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
