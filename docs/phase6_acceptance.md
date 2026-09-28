# Phase 6 Acceptance: Camoufox Browser Backend

> **Superseded (2026-09-28).** This is a historical Phase 6 record. Phase 7 moved the
> new-run default back to Chrome (Prompt 1) and then to **Patchright** (Prompt 6,
> `docs/phase7_acceptance.md`). Camoufox is now retained as a dormant/experimental,
> uncertified backend. Existing runs keep their persisted backend.

**Date:** 2026-09-27
**Status:** **Phase 6 ACCEPTED on local evidence.** The Queue-it staging boundary
remains **UNKNOWN / NOT RUN**.

## Scope

Phase 6 integrated Camoufox 0.5.6 into Quetty's bounded parked-session architecture
behind a browser-backend boundary. Camoufox is now the default backend for new runs,
and Chrome is kept as a supported fallback.

This report classifies each acceptance question as **PASS**, **FAIL**, or **UNKNOWN**,
using only evidence measured in this repository:
- source and tests;
- the controlled application workflow;
- the local recovery and capacity benchmark;
- the Phase 6 documents.

All browser evidence ran against `LocalQueueSimulator` on 127.0.0.1. **No Queue-it or
staging traffic was ever sent.** Local results are never treated as staging results.

The acceptance requirement is **persisted Queue-session continuity**, not browser
fingerprint continuity. A parked session passes when:
- it keeps its persisted expected Queue ID;
- it is reconstructed in a fresh Camoufox BrowserContext;
- its journey restores through a supported mechanism;
- the live observed Queue ID equals the persisted expected Queue ID;
- progress and lifecycle are inspected;
- refreshed state is persisted;
- the context closes again.

Fingerprint characteristics may differ between reconstructions, and that is not a
failure. Nothing here claims Camoufox is undetectable, stealthy, or able to bypass
anti-bot systems.

## Exact versions and environment

| Item | Value |
|---|---|
| Host | macOS 26.5.1 arm64 (Apple M5 Pro), 15 logical CPUs, 24 GiB RAM |
| Python | 3.14.7 (project requires ≥ 3.12) |
| Playwright | 1.62.0 (exact pin) |
| Camoufox Python package | 0.5.6 (exact pin) |
| Camoufox browser | `official/stable/152.0.4-beta.30` (exact pin; launched by exact selector, never fetched) |
| Chrome (fallback) | installed Google Chrome 153.0.8010.54 (`channel="chrome"`) |
| Dependency health | `pip check`: no broken requirements. `queue-load-test-camoufox-preflight`: PASS |

## Tested configurations

- **Application defaults:**
  - `BROWSER_BACKEND=camoufox` for new runs; `CHROME_PROCESS_COUNT=2`,
    `MAX_CONTEXTS_PER_BROWSER=25`, `MAX_ACTIVE_CONTEXTS=50`, `MAX_MANUAL_OPEN_SESSIONS=5`;
  - Camoufox serialized to one live context per process;
  - the manual Camoufox pool uses one process per window;
  - a connected Camoufox process is restarted after 3 consecutive navigation timeouts or
    one unstoppable call.
- **Controlled workflow** (`queue-load-test-phase5-workflow`): 1 automatic process,
  `MAX_ACTIVE_CONTEXTS=5`, `MAX_MANUAL_OPEN_SESSIONS=2`, and 2 creation, 2 monitor, and 2
  operator workers. HYBRID mode. Camoufox ran with a visible headed pool.
- **Benchmark** (`queue-load-test-phase6-camoufox-benchmark --headed`):
  - capacity levels 5–50 in shared-process families, and 1–4 per-process;
  - 3 navigation waves and 0.25 s sampling per case;
  - production profile of 2 processes and 4 workers;
  - 40 sessions × 5 sweeps, 3 full browser restarts, and 10 repeated kills;
  - the real operator runtime for the manual, pause, and shutdown scenarios.

Final evidence files:
- `docs/results/phase6_acceptance_benchmark_result.json`
- `docs/results/phase6_acceptance_workflow_camoufox_result.json`
- `docs/results/phase6_acceptance_workflow_chrome_result.json`

Prompt 5 evidence remains in `docs/results/phase6_camoufox_benchmark_result.json`.

## Prompt 3 fingerprint finding and why it is not a blocker

Prompt 3 established that Camoufox 0.5.6 has **no supported, complete, stable
per-context fingerprint descriptor**:
- a reused `preset=` still changed observable canvas identity over 20 contexts and after
  a runtime restart;
- normal contexts share the launch identity;
- launch-option replay is process-scoped and is not a per-session contract.

That result stands as **FAIL / unsupported**.

It does not block Phase 6. Quetty's durable identity is the Queue-it journey and its
persisted Queue ID, not a device fingerprint.
- Quetty persists no fingerprint data.
- It does not read or compare fingerprint values.
- A changed fingerprint has **no authority** to replace or overwrite a Queue ID.

## Queue identity model

- **Queue ID is authoritative.** Each persisted session has one expected Queue ID,
  enforced unique by SQLite. It is never overwritten by an observation.
- **Every restore verifies.** The live observed Queue ID must equal the expected one. A
  mismatch fails the restore, keeps the expected value, and follows normal
  failure/retry semantics. It never creates a replacement identity.
- **Provenance is persisted.** `browser_backend` is stored on the run and on every
  session. `run_config.browser_build` records the pinned Camoufox build. Legacy rows are
  backfilled as Chrome. Provenance is not fingerprint data.

## Restoration model

- **Automatic checks:** each claims a session, opens a fresh context in a bounded shared
  process, restores through the supported transfer URL first, verifies, inspects,
  refreshes HYBRID state, closes, releases the lease, and parks. No BrowserContext
  survives parking.
- **HYBRID fallback:** a HYBRID session falls back to storage state only when the
  session backend equals the run backend.
- **TRANSFER_ONLY:** these sessions need no long-term storage state.
- **Backend mismatch:** fails with `BACKEND_MISMATCH` before any context opens. The state
  file is not read or rewritten.

## Storage compatibility matrix (Prompt 3, local cookie/localStorage only)

| Source → destination | Local result | Runtime use |
|---|---|---|
| Camoufox → Camoufox | **PASS** | used (same backend) |
| Chrome → Camoufox | **PASS locally**; Queue-it **UNKNOWN** | **not used**, rejected by provenance |
| Camoufox → Chrome | **FAIL locally** (insecure `SameSite=None` cookie rejected) | **not used**, rejected by provenance |
| Chrome → Chrome | **PASS** | used (same backend) |

## Park/reopen results

| Evidence | Camoufox | Chrome |
|---|---|---|
| Continuity test: 20 fresh-context cycles with a full process/repository restart at cycle 10 | **20/20**; Queue ID and provenance unchanged | n/a |
| Production-profile sweeps, 40 sessions × 5 | **200/200** verified, 0 mismatches, 0 state-refresh failures; p50 0.78–0.82 s, p95 0.81–1.31 s; sweeps 8.0–9.1 s; 0 contexts and 0 leases after each sweep | 200/200; p50 0.29 s; sweeps 3.0–3.1 s |
| Full browser-process restart ×3 | 40/40 each cycle; launch about 1.04 s for 2 processes | 40/40; about 0.41 s |
| Application restart with 5 stranded leases and a foreign-backend row | 5 expired leases recovered; 40/40 verified; foreign row rejected, state byte-identical | same |

## Workflow results (controlled Phase 5 workflow, final run)

**Camoufox, visible headed pool: 63/63 PASS. Chrome: 63/63 PASS.** The workflow covers:
- **Setup:** setup validation, bounded acquisition, and restart deficit acquisition.
- **Monitoring and operator actions:** dashboard, pause/resume, Refresh while paused,
  Add (exactly one despite duplicate submit), create-first Replace, and Delete with
  progress and state cleanup.
- **Manual windows:** headed Open restoring the same Queue ID, scheduler exclusion,
  Close, window loss, no-ID Open and adoption, duplicate adoption rejection, and
  identity-mismatch preservation.
- **Failure:** `SIGKILL` browser crash during monitoring (every row checked again, 0
  identity change).
- **Lifecycle:** app restart and Stop & Reset Run.

## Capacity and resource results (final benchmark)

The shipped Camoufox profile is **one live context per process**. With
`CHROME_PROCESS_COUNT` ≤ 4, at most 4 automatic Camoufox contexts can be live at once.
Configured limits and observed peaks are listed separately.

| Case | Processes (configured) | Contexts (configured / achieved / observed peak) | Context creation p50 / p95 (s) | Navigation p95 (s) | Restore p50 / p95 (s) | App CPU avg / peak % | Browser CPU avg / peak % | App RSS avg MB | Browser tree RSS avg / peak MB | Failures |
|---|---:|---|---|---:|---|---|---|---:|---|---|
| Camoufox serialized p1 | 1 | 1 / 1 / 1 | 0.148 / 0.148 | 0.023 | 0.335 / 0.776 | 13 / 75 | 144 / 230 | 178 | 1175 / 1836 | 0 |
| Camoufox serialized p2 | 2 | 2 / 2 / 2 | 0.082 / 0.151 | 0.434 | 0.399 / 0.413 | 17 / 90 | 233 / 473 | 180 | 1953 / 3106 | 0 |
| Camoufox serialized p3 | 3 | 3 / 3 / 3 | 0.005 / 0.163 | 0.477 | 0.452 / 0.994 | 19 / 91 | 331 / 705 | 181 | 2833 / 4567 | 0 |
| Camoufox serialized p4 | 4 | 4 / 4 / 4 | 0.004 / 0.201 | 0.591 | 0.540 / 0.563 | 20 / 90 | 362 / 987 | 169 | 3177 / 5762 | 0 |
| Camoufox unserialized c5 (unsupported mode) | 1 | 5 / 5 / 5 | 0.096 / 0.145 | 0.091 | 1.77 / 10.69 | 13 / 91 | 55 / 367 | 146 | 1777 / 2432 | 11 transfer/state failures of 21 restores; 2 unresponsive-process restarts; family stopped |
| Chrome p1–p4 | 1–4 | 1–4 / same / same | ≤ 0.006 | ≤ 0.057 | 0.13–0.21 / 0.14–0.36 | 21–26 / ≤ 89 | 80–141 / ≤ 332 | 144 | 686–2160 / ≤ 3942 | 0 |
| Chrome shared c5 → c50 | 1–2 | 5–50 / same / same | ≤ 0.071 / ≤ 0.088 | 0.05 → 0.58 | 0.51 → 3.52 / 0.60 → 5.62 | 17–26 / ≤ 91 | 130–349 / ≤ 487 | 144–150 | 1233 → 9781 / 2282 → 15917 | 0 |

Notes:
- **Cleanup and crashes:** cleanup failures, crashes, and operation timeouts were 0 in
  every supported case.
- **Unserialized family:** in Prompt 5 the unserialized family also wedged under churn
  (0/30) and failed 75/90 hold navigations at 30 contexts. Serialization is required.
- **Units:** CPU 100% = one logical core. Summed RSS may count shared pages more than
  once.
- **Scope of throughput figures:** simulator checks per second are local page rates, not
  Queue-it throughput.
- **Descriptive comparison:** Camoufox restores took about 2.5–3× Chrome's local latency
  and used more process-tree CPU and RSS per live context. This is not a ranking.

## Recovery results (final benchmark, both backends, 22/22 scenarios, 282/282 checks)

| Scenario | Camoufox |
|---|---|
| `SIGKILL` with 0 / 1 / 3 active contexts | Disconnect detected in 0.05 s; lost contexts counted 0/1/3; capacity released; slot replaced (about 0.45 s restart); 1 process after |
| Two slots, kill one | Only the failed slot replaced; the healthy slot kept serving |
| Repeated kill ×10 | 10/10 recovered; restart p50 0.445 s, max 0.474 s; 0 restart failures, 0 process leaks, 0 context leaks |
| Kill during a monitoring sweep | Sweep completed 40/40 verified within the restore deadline; leases released; no row FAILED; 0 new identities |
| Restoration faults | Transfer 503 → same-backend state fallback **restored**; missing/corrupt state, provenance mismatch, navigation failure, and identity mismatch all failed without changing the expected Queue ID |
| Headed manual failure | Close, window loss, headed-pool kill, shutdown with a window open, reopen after restart, and 10 concurrent open/close cycles: all pass |
| Pause under failure | 0 claims while paused after an automatic-browser kill; Refresh and Open still work; resume continues |
| Shutdown under load | Automatic checks, running and queued operator actions, and a headed window in flight: 0 contexts, processes, leases, and owners after; identities intact; Camoufox 10.4 s (Chrome 3.4 s) |

### Defects found and fixed during acceptance

The first acceptance run **hung** during Camoufox "shutdown under load": application
shutdown blocked for more than 20 minutes. It had been seen once in Prompt 5 as well.
Local diagnosis found four causes, all fixed and regression-tested:

1. **Bounding by cancellation was not a real bound.**
   - **Cause:** Playwright 1.62 answers the first cancellation by sending an abort and
     waiting, without a limit, for the browser to acknowledge it. `asyncio.timeout` and
     `wait_for` cancel only once.
   - **Fix:** `await_bounded` (`utils/asyncio_tools.py`) runs the call as a task. It
     re-cancels until the call stops, and abandons it if needed with a strong reference
     kept, with late results such as contexts discarded. It replaces `asyncio.timeout`
     and `wait_for` at every browser call site: restorer attempts, manual inspection and
     adoption, creator attempts, manager launch/context creation/restart
     close/shutdown, and the preflight.
2. **A stuck Playwright driver.** If `Playwright.stop()` misses its deadline, the driver
   is killed. Playwright then fails every pending call, so neither the event loop nor
   uvicorn can wait forever at exit. This is a last resort that uses private Playwright
   1.62 attributes defensively.
3. **Prompt 4 regression: create/close overlap.** The per-process lease was released
   when a close *started*, which allowed exactly the overlapping create/close that
   wedges Camoufox. The lease is now held until the close finishes. A close that misses
   its deadline marks the process for replacement, and the replacement gets a fresh
   lease.
4. **Latent scheduler bug.** When the runtime deadline cancelled the scheduler, the
   following shutdown step re-raised the workers' `CancelledError` and aborted the
   ordered shutdown. It now gathers with `return_exceptions` and sends stop sentinels
   only to live workers.

**Evidence of the fix:**
- A clean 10-run loop of the manual/pause/shutdown group (5 headless, 5 headed, both
  backends; 60 scenario runs, 20 shutdown-under-load) had **0 hangs, 0 crashes, and 0
  failures**, with 0 leftover processes. Camoufox shutdown took 10.4–20.5 s and Chrome
  3.3–6.4 s.
- In that loop the unresponsive-restart and driver-kill safety nets never had to fire.
  The lease fix alone removed the overlap.
- The final full benchmark and both workflows then passed.

## Chrome regression status

**PASS.** Chrome passed:
- the controlled workflow (63/63);
- all 11 Chrome recovery scenarios;
- the capacity comparison to 50 contexts with 0 failures;
- the existing Chrome integration tests.

Chrome remains a first-class, tested fallback.

## Default-backend decision

**Camoufox is the default backend for new runs** (Prompt 6,
`docs/phase6_operational_migration.md`).
- **Basis:** every functional gate passed on local evidence, and that remains true after
  the acceptance fixes.
- **Existing runs** keep the backend they were created with.
- **Chrome** is retained via `BROWSER_BACKEND=chrome`.
- **Switching backends** requires Stop & Reset Run.

## PASS / FAIL / UNKNOWN matrix

### Dependencies / installation

| Question | Result | Evidence |
|---|---|---|
| Supported Python/Playwright/Camoufox combination installed? | **PASS** | Exact pins; `pip check` clean; preflight PASS |
| Python version recorded? | **PASS** | 3.14.7 (benchmark `environment`) |
| Playwright version recorded? | **PASS** | 1.62.0 |
| Camoufox package version recorded? | **PASS** | 0.5.6 |
| Camoufox browser build recorded? | **PASS** | 152.0.4-beta.30 (pinned and observed) |
| Installation deterministic/documented? | **PASS** (version) / **UNKNOWN** (artifact checksum) | Exact `camoufox fetch official/stable/152.0.4-beta.30`; README and migration doc |
| Missing installation fails clearly? | **PASS** | `BrowserBackendSetupError` names the fetch command; setup returns 503 and persists nothing (unit tests) |
| Startup never silently downloads/updates? | **PASS** | Exact selector launch only; no fetch call in the runtime |

### Browser backend

| Question | Result | Evidence |
|---|---|---|
| Clean backend boundary? | **PASS** | `BrowserBackend` / `ChromeBackend` / `CamoufoxBackend`; orchestration is browser-agnostic |
| Chrome remains supported? | **PASS** | 63/63 workflow; all recovery scenarios |
| Camoufox launches through the async architecture? | **PASS** | `AsyncNewBrowser` inside `BrowserManager` |
| Multiple bounded contexts without one process per session? | **PASS** (sequential sharing) | 40 sessions on 2 processes. Concurrent live contexts are limited to 1 per process, because unserialized churn wedges |
| Per-browser limits enforced? | **PASS** | Manager slot limits; per-process lease; unit tests |
| Global context capacity enforced? | **PASS** | Shared `BrowserContextCapacity`; capacity released after kills |
| Browser recovery tasks bounded? | **PASS** | One restart task per slot; bounded launch/close via `await_bounded` |
| Failed processes replaced without multiplication? | **PASS** | Exactly 1 main process per slot across 10 kills; only the failed slot replaced |
| Shutdown returns context ownership to zero? | **PASS** | Every scenario and workflow ends with 0 contexts, leases, owners, and processes |

### Queue session identity

| Question | Result | Evidence |
|---|---|---|
| Queue ID authoritative? | **PASS** | SQLite-unique expected Queue ID; verified on every restore |
| Expected ID immutable during restore failures? | **PASS** | Six fault cases on both backends |
| Expected ID immutable during identity mismatch? | **PASS** | Workflow, continuity test, and benchmark |
| Zero silent Queue ID replacement shown locally? | **PASS** | 0 changes and 0 replacements across all Phase 6 evidence |
| Fingerprint change has no authority over Queue identity? | **PASS** | No fingerprint is read, compared, or persisted; only the Queue ID is verified |
| Fingerprint continuity documented as NOT REQUIRED? | **PASS** | This report; strategy, runtime, and migration docs |
| Backend provenance persisted where required? | **PASS** | Run and session `browser_backend`; run `browser_build` |
| Legacy Chrome sessions identified safely? | **PASS** | Additive migration backfills `chrome`; tested on a real pre-Phase-6 schema |

### Park / reopen

| Question | Result | Evidence |
|---|---|---|
| New Camoufox session parks by fully closing its context? | **PASS** | 0 contexts after creation and after every sweep |
| Later reopens in a fresh context? | **PASS** | 200/200 plus 20/20 |
| Observed ID matches expected after reopen? | **PASS** | 0 mismatches |
| Parks again? | **PASS** | Rescheduled, 0 contexts and 0 leases after |
| Repeated cycles actually exercised? | **PASS** | 5 sweeps × 40; 20 continuity cycles |
| Queue IDs unchanged through cycles? | **PASS** | Identity map identical before and after |
| Full browser process restart preserves continuity? | **PASS** | 3 cycles × 40/40 |
| Full application/repository restart preserves continuity? | **PASS** | App restart scenario; workflow restart |

### Restoration

| Question | Result | Evidence |
|---|---|---|
| Camoufox transfer restoration works locally? | **PASS** | Transfer-first on every restore |
| Camoufox state restores into Camoufox? | **PASS** | Transfer outage fell back to same-backend state and restored |
| Transfer-first HYBRID preserved? | **PASS** | Attempt order recorded in results |
| Storage fallback works where compatible? | **PASS** | As above |
| TRANSFER_ONLY functional without long-term state? | **PASS** | `test_camoufox_transfer_only_restores_without_persisted_storage_state` |
| Provenance mismatches detected? | **PASS** | `BACKEND_MISMATCH`; state checker `backend_provenance_mismatch` |
| Unsafe cross-engine restoration rejected, not attempted? | **PASS** | Rejected before any context; state byte-identical, both directions |
| Chrome→Camoufox reported per Prompt 3? | **PASS** | Local PASS, Queue-it UNKNOWN, not used |
| Camoufox→Chrome reported per actual evidence? | **PASS** | Local FAIL, not used |

### Creation

| Question | Result | Evidence |
|---|---|---|
| Initial Camoufox acquisition works (simulator)? | **PASS** | Workflow; 40/40 benchmark creation |
| Automatic deficit acquisition works? | **PASS** | `partial_restart_resumes_only_deficit` |
| Add creates exactly one session? | **PASS** | `add_creates_exactly_one_despite_duplicate_submit` |
| Replace remains create-first? | **PASS** | `replace_creates_new_independent_identity_then_removes_old` |
| Failed replacement preserves the original? | **PASS** (backend-agnostic unit tests) | `test_replace_keeps_old_on_failure...`, `test_failed_replacement_that_raises_keeps_old_identity` |
| Successful replacement produces a new unique ID? | **PASS** | Workflow Replace check |
| Duplicate Queue IDs rejected by SQLite? | **PASS** | Repository uniqueness tests; `test_duplicate_replacement_is_not_success_and_keeps_old` |

### Monitoring

| Question | Result | Evidence |
|---|---|---|
| Reconstructs sessions in fresh Camoufox contexts? | **PASS** | Scheduler sweeps |
| Verifies expected Queue ID? | **PASS** | 0 mismatches |
| Inspects live progress/lifecycle? | **PASS** | `auto_refresh_shows_monitored_progress`; lifecycle filter |
| Persists refreshed state? | **PASS** | 0 HYBRID state-refresh failures |
| Closes/reparks after checks? | **PASS** | 0 contexts and 0 leases after sweeps |
| Workers fixed/bounded? | **PASS** | Fixed worker pool; scheduler tests |
| Queue bounded? | **PASS** | Queue depth peak = configured capacity (8) |
| Leases prevent overlapping ownership? | **PASS** | `test_active_lease_prevents_claim_until_expiry`; `scheduler_skips_open_session` |

### Phase 5 operator workflow

| Question | Result | Evidence |
|---|---|---|
| Refresh Now through Camoufox? | **PASS** | Workflow |
| Refresh usable while paused? | **PASS** | `refresh_now_works_while_paused`; pause-under-failure scenario |
| Headed Open through Camoufox? | **PASS** | Visible headed workflow and benchmark |
| Headed Open restores same Queue ID? | **PASS** | `open_in_browser_restores_existing_identity` |
| Headed Open excludes automatic monitoring? | **PASS** | `scheduler_skips_open_session` |
| Explicit Close releases ownership? | **PASS** | Workflow and benchmark |
| Window close releases/recovers ownership? | **PASS** | `manual_window_loss_releases_ownership_and_parks` |
| Headed browser failure releases/recovers ownership? | **PASS** | Headed-pool `SIGKILL` scenario |
| Manual headed sessions bounded? | **PASS** | `MAX_MANUAL_OPEN_SESSIONS`; per-window process pool; `manual_contexts_bounded` |
| Manual and automatic share global capacity? | **PASS** | Shared `BrowserContextCapacity` (runtime composition) |
| No-Queue-ID Open works? | **PASS** | `no_id_open_adopts_first_unique_queue_identity` |
| Opening alone assigns no Queue ID? | **PASS** | `test_manual_open_without_queue_id_opens_staging_without_recording` |
| Adoption works when a live identity appears? | **PASS** | Workflow; `test_session_without_queue_id_adopts_identity_on_heartbeat` |
| Duplicate adoption rejected safely? | **PASS** | Workflow; `test_adopting_duplicate_queue_id_is_rejected_and_state_discarded` |
| Delete removes local browser state? | **PASS** | `delete_removes_session_progress_and_state` |
| Pause/resume keeps Phase 5 semantics? | **PASS** | Workflow and benchmark |
| Stop & Reset fully clears state and returns to setup? | **PASS** | `stop_and_reset_*` checks |

### Recovery

| Question | Result | Evidence |
|---|---|---|
| Browser operation deadlines effective with Camoufox? | **PASS** (after acceptance fix) | `await_bounded`; regression tests reproduce the Playwright abort wait; 10-run loop clean |
| Actual Camoufox process failure injected? | **PASS** | Real `SIGKILL` |
| Lost contexts detected/accounted? | **PASS** | `browser_contexts_lost_total` 0/1/3 |
| Browser replacement observed? | **PASS** | Restart durations recorded |
| Repeated failure tested? | **PASS** | ×10 |
| Monitoring interrupted by browser failure tested? | **PASS** | Kill-mid-sweep scenario |
| Expired leases recoverable? | **PASS** | 5/5 recovered after restart |
| Shutdown with active work tested? | **PASS** | Shutdown-under-load scenario |
| Shutdown with manual headed sessions tested? | **PASS** (after acceptance fix) | Shutdown with an open window; 10-run loop |
| Zero unwanted owners/leases after recovery? | **PASS** | Every scenario |
| All recovery cases preserve expected Queue IDs? | **PASS** | Identity map unchanged in every scenario |

### Capacity / resources

| Question | Result | Evidence |
|---|---|---|
| Real local Camoufox capacity benchmark run? | **PASS** | Final and Prompt 5 runs |
| Exact tested context counts recorded? | **PASS** | Per case |
| Configured limits distinguished from observed peaks? | **PASS** | Separate fields |
| Application CPU recorded? | **PASS** | |
| Camoufox/browser CPU recorded? | **PASS** | Browser-neutral process tree |
| Application RSS recorded? | **PASS** | |
| Browser/process-tree RSS recorded? | **PASS** | With the shared-page caveat |
| Context creation latency recorded? | **PASS** | |
| Navigation latency recorded? | **PASS** | |
| Restoration latency recorded? | **PASS** | |
| Browser failures recorded? | **PASS** | |
| Cleanup failures recorded? | **PASS** | |
| Same-host Chrome comparison performed? | **PASS** | Same host, cases, and sampling |
| Comparisons descriptive only? | **PASS** | No ranking claims |

### Default / operations

| Question | Result | Evidence |
|---|---|---|
| Backend persisted in run configuration? | **PASS** | `run_config.browser_backend` |
| Existing Chrome run stays Chrome after the default changes? | **PASS** | `test_existing_chrome_run_restarts_as_chrome_after_default_change` |
| Legacy runs handled safely? | **PASS** | Chrome backfill; build NULL |
| Unsafe backend switching during a run prevented? | **PASS** | Runtime builds pools from the persisted run backend only |
| Stop & Reset documented as the way to change backend? | **PASS** | README; migration doc |
| Camoufox default supported by functional evidence? | **PASS** | Gate table (Prompt 6) plus this report |
| Chrome retained as fallback? | **PASS** | |
| Chrome rollback documented? | **PASS** | Migration doc |
| Camoufox upgrade procedure documented? | **PASS** | Migration doc |
| Version policy documented? | **PASS** | Exact three-layer pins; upgrade between runs; changed build logged and shown |

### Observability / data safety

| Question | Result | Evidence |
|---|---|---|
| Browser metrics semantically correct for Camoufox? | **PASS** | Backend-aware process tree; browser-neutral names. Historical `chrome_*` Python fields documented |
| Backend information visible safely? | **PASS** | `browser_backend_info{backend,browser_build}`; dashboard backend and build |
| Metrics low-cardinality? | **PASS** | `test_prometheus_counters_gauges_and_histograms_have_bounded_labels`; one-series info metric |
| Transfer URLs absent from ordinary logs? | **PASS** | `test_structured_logs_redact_urls_and_keep_correlation_fields`; allow-listed fields |
| storage_state absent from logs? | **PASS** | Never logged; allow-listed fields |
| Fingerprint data absent from logs/reports/UI? | **PASS** | Never read; result files scanned |
| Queue IDs absent from Prometheus labels? | **PASS** | `FORBIDDEN_LABEL_NAMES`; observability test |

### Quality

| Question | Result | Evidence |
|---|---|---|
| Full non-staging suite passes? | **PASS** | **513 passed**, 4 staging deselected |
| Ruff passes? | **PASS** | |
| mypy passes (apart from documented platform issues)? | **PASS** on macOS (70 files) | Earlier win32 `signal.SIGKILL` reports are **UNKNOWN**, not re-run on Windows |
| Chrome regression workflow passes? | **PASS** | 63/63 |
| Camoufox controlled workflow passes? | **PASS** | 63/63 (headed) |
| Phase 5 behavior preserved? | **PASS** | All Phase 5 checks retained and passing; only neutral wording, configuration, and bounded-shutdown fixes |
| Persisted population independent of live context count? | **PASS** | 40 sessions on 2 processes; parked sessions hold no context |
| No browser process per Queue session? | **PASS** | Fixed process pools (manual Camoufox pool ≤ `MAX_MANUAL_OPEN_SESSIONS`) |
| Bounded parked-session architecture intact? | **PASS** | Fixed workers, bounded queues, shared capacity, bounded operations |

### Staging boundary

| Question | Result |
|---|---|
| Real authorised Queue-it staging tested with Camoufox? | **UNKNOWN (NOT RUN)** |
| Real Queue-it acquisition reliability known? | **UNKNOWN** |
| Real Queue-it transfer restoration reliability known? | **UNKNOWN** |
| Real Queue-it Camoufox storage restore reliability known? | **UNKNOWN** |
| Real Queue-it monitoring cadence known? | **UNKNOWN** |
| Real Queue-it lifecycle behavior known? | **UNKNOWN** |
| Real Queue-it resource usage known? | **UNKNOWN** |
| All unrun staging questions retained as UNKNOWN? | **PASS** |

### Upstream risk

| Question | Result | Evidence |
|---|---|---|
| Camoufox maturity/development risk documented? | **PASS** | Readiness doc: upstream says it is under development and may not suit stable production use |
| Playwright compatibility risk documented? | **PASS** | Camoufox 0.5.6 requires `playwright<1.63`; the last-resort driver kill relies on private 1.62 internals, so it is part of upgrade validation |
| Camoufox browser-version compatibility risk documented? | **PASS** | beta.29 fails and beta.30 passes on Playwright 1.61/1.62; beta.31 not adopted |
| Rollback to validated Chrome possible? | **PASS** | `BROWSER_BACKEND=chrome` plus Stop & Reset |

### Final decision

| Question | Result |
|---|---|
| Camoufox accepted as a supported backend? | **PASS** |
| Park/reopen through fresh Camoufox contexts locally proven? | **PASS** |
| Queue ID continuity across repeated reconstruction proven locally? | **PASS** |
| … across browser process restart? | **PASS** |
| … across application restart? | **PASS** |
| … through browser failure/recovery? | **PASS** |
| Camoufox accepted as the default backend? | **PASS** (new runs; local evidence) |
| Chrome retained as fallback? | **PASS** |
| Phase 6 complete without requiring fingerprint continuity? | **PASS** |
| Phase 6 complete without weakening Phase 5 ownership/persistence invariants? | **PASS** |

## Staging UNKNOWN boundary

Nothing in this report is Queue-it staging evidence. All of the following remain
**UNKNOWN / NOT RUN**:
- real Queue-it acquisition, transfer, and storage restore reliability for Camoufox;
- real monitoring cadence, lifecycle, admission, and no-ID adoption;
- real resource usage;
- any staging-safe concurrency level;
- cross-engine Queue-it behavior.

The Phase 5 staging items S1–S8 also remain UNKNOWN.

## Known issues

1. **Camoufox concurrency is process-bound:** one live context per process, and at most
   4 automatic contexts under the current `CHROME_PROCESS_COUNT ≤ 4`. The unserialized
   mode is unsupported and exists only for measurement.
2. **Camoufox is slower and heavier locally:** about 2.5–3× Chrome's restore latency,
   with higher per-context CPU and RSS.
3. **Shutdown under load is bounded but can exceed one stage timeout.** Camoufox took
   10.4–20.5 s against a 10 s `SHUTDOWN_TIMEOUT_SECONDS`, because each shutdown stage is
   bounded separately.
4. **Private Playwright internals:** the last-resort driver kill reads private
   Playwright 1.62 attributes. It is defensive (a no-op if they are absent), but it must
   be re-validated on any Playwright upgrade.
5. **Metric overlap:** unresponsive-process restarts are also counted in
   `browser_crashes_total` (restart path). `browser_unresponsive_restarts_total`
   distinguishes them.
6. **Download integrity:** whether `camoufox fetch` verifies artifact integrity is
   UNKNOWN.
7. **Other hosts:** visible headed Camoufox was validated only on this macOS arm64 host.
8. **Windows mypy:** the earlier win32 `SIGKILL` reports were not re-checked on Windows.

## Rollback path

- **Back to Chrome for new work:** use Stop & Reset Run, set `BROWSER_BACKEND=chrome`
  (with Chrome installed via `python -m playwright install chrome`), and create a new
  run. Existing Chrome runs never need action.
- **Camoufox version rollback:** revert the pins, reinstall, run
  `camoufox fetch official/stable/152.0.4-beta.30`, and run the preflight.
- **Code rollback:** the Phase 6 commits are sequential and revertible. Chrome paths
  remain covered by tests.

Full procedures are in `docs/phase6_operational_migration.md`.
