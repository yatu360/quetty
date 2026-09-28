# Phase 7 Prompt 5 — Patchright Recovery, Concurrency, Capacity, and Resource Benchmark

**Date:** 2026-09-28
**Decision:** **READY_FOR_DEFAULT_DECISION**
**Scope:** controlled local simulator only (`LocalQueueSimulator` on 127.0.0.1)
**Staging:** **NOT RUN / UNKNOWN**
**Harness:** `queue-load-test-phase7-patchright-benchmark --headed --output ...`
(`src/queue_load_test/harness/phase7_patchright_benchmark.py`)
**Aggregate result:** `docs/results/phase7_patchright_benchmark_result.json`

## Outcome

| | Patchright (candidate) | Chrome (control) |
|---|---:|---:|
| Scenarios | 15/15 PASS | 15/15 PASS |
| Scenario checks | 247/247 | 247/247 |
| Concurrency cases (all healthy) | 6/6, up to 25 contexts/process and 50 on 2 processes | 6/6, same levels |
| Queue ID changes / replacement identities | 0 / 0 | 0 / 0 |
| Leftover browser processes after the run | 0 | 0 |

The first full run scored 492/494. Both failures were the same inherited harness check,
one per backend (see [Defect found and fixed](#defect-found-and-fixed)). The check was
corrected and that scenario was re-run for both backends. The result file keeps the
original failing values under `corrections`.

The default backend was **not** changed. Camoufox is still in the codebase and got no
benchmark effort in this prompt.

## Evidence boundary

- Every browser call targeted the local simulator. Checks/s is a local page rate, not
  Queue-it throughput, and no tested concurrency level is known to be staging-safe.
- The durable identity under test is the persisted expected Queue ID. Identity continuity
  is compared in memory against the persisted `(session, Queue ID)` map.
- The report contains no Queue IDs, session IDs, transfer URLs, storage state, or
  fingerprint values. A scan of the result file for synthetic IDs, transfer URLs,
  cookies, and state keys matched only check names.
- Nothing here measures or claims anti-detection behaviour.
- Results apply to this host and these populations only. Do not extrapolate them.

## Host, versions, and profiles

| Item | Value |
|---|---|
| Host | macOS 26.5.1 arm64 (Apple M5 Pro), 15 logical CPUs, 24 GiB RAM |
| Python | 3.14.7 (project targets `>=3.12`) |
| Patchright | 1.63.0, async API, `channel="chrome"` |
| Playwright (control) | 1.62.0 |
| Browser (both) | installed Google Chrome 153.0.8010.54 |
| psutil | 7.2.2 |
| Run time | 1,189 s for the full run; failure-during-monitoring re-run separately |

Both backends ran one after the other on the same host, with the same profiles and the
same simulator. Each profile stayed inside the existing architectural limits
(`CHROME_PROCESS_COUNT ≤ 4`, `MAX_CONTEXTS_PER_BROWSER ≤ 25`, `MAX_ACTIVE_CONTEXTS ≤ 100`):

- **Concurrency (B):** 1 process at 1, 5, 10, 20, and 25 contexts. Then 2 processes
  × 25 contexts (50), which is the shipped default ceiling
  (`CHROME_PROCESS_COUNT=2`, `MAX_CONTEXTS_PER_BROWSER=25`, `MAX_ACTIVE_CONTEXTS=50`).
  Each case ran: 3 navigation waves, a 5 s hold, a final wave, close-all, reacquisition
  of the same count, a production scheduler churn sweep over `max(10, 3 × contexts)`
  sessions with workers = contexts, a fresh-context health probe, and shutdown.
- **Churn (C):** one long-lived process with 1, 5, 10, and 20 workers, 300
  create/page/navigate/inspect/close cycles each (1,200 per backend). A single browser
  step unsettled after 15 s counts as stuck.
- **Park/reopen and recovery (A, E, F):** 2 processes and 4 monitor workers in HYBRID
  mode:
  - a 50-session population with 6 park/reopen sweeps;
  - 3 full browser restarts;
  - an application restart;
  - 20 repeated kill cycles.
- **Monitoring (D, K):** 100 persisted sessions, each re-checked every 5 s with ±0.5 s
  jitter (20 checks/s offered). Profiles were 2 processes × 4 workers and 2 × 8, run for
  60 s each and sampled every 1 s.
- **Stuck navigation (G):** 10 sessions with 4 made slow (the simulator delays them
  30 s). Navigation timeout 2 s, attempt deadline 6 s, 2 workers. Then 15 repeated
  timeouts interleaved with healthy probes on one process.
- **Operator runtime (H, I, J):** the real `ApplicationRunRuntime` with a **visible headed
  manual pool** for both backends. Settings: 1 automatic process, 5 contexts, 2 monitor
  workers, 2 operator workers, `MAX_MANUAL_OPEN_SESSIONS=2`,
  `SHUTDOWN_TIMEOUT_SECONDS=10`.

Resource accounting: the browser tree is the processes named `*chrome*` that descend
from the benchmark process. Summed RSS may double-count shared pages. CPU 100% means one
logical core.

## B. Context concurrency: Patchright does not need one context per process

This was measured, not assumed. Every level was healthy on both backends, so no family
stopped early.

| Case | Contexts held (manager / browser-reported) | After close | Reacquire failures | Hold nav p50 / p95 (s) | Churn restores | Churn restore p50 / p95 (s) | Local checks/s | Browser RSS avg / peak MB | Browser CPU avg / peak % |
|---|---|---:|---:|---|---|---|---:|---|---|
| Patchright p1-c1 | 1 / 1 | 0 | 0 | 0.013 / 0.019 | 10/10 | 0.151 / 0.162 | 5.98 | 1040 / 1193 | 27 / 107 |
| Patchright p1-c5 | 5 / 5 | 0 | 0 | 0.046 / 0.064 | 15/15 | 0.621 / 0.744 | 7.59 | 2030 / 2479 | 54 / 171 |
| Patchright p1-c10 | 10 / 10 | 0 | 0 | 0.091 / 0.112 | 30/30 | 1.343 / 1.642 | 7.14 | 3020 / 4178 | 93 / 229 |
| Patchright p1-c20 | 20 / 20 | 0 | 0 | 0.234 / 0.318 | 60/60 | 2.925 / 3.828 | 6.68 | 5071 / 7310 | 137 / 283 |
| Patchright p1-c25 | 25 / 25 | 0 | 0 | 0.305 / 0.352 | 75/75 | 3.735 / 5.501 | 6.59 | 6624 / 8946 | 152 / 314 |
| Patchright p2-c50 | 50 / 50 | 0 | 0 | 0.519 / 0.627 | 150/150 | 4.749 / 6.278 | 10.44 | 11800 / 15594 | 283 / 487 |
| Chrome p1-c1 | 1 / 1 | 0 | 0 | 0.011 / 0.015 | 10/10 | 0.130 / 0.139 | 7.04 | 782 / 926 | 25 / 106 |
| Chrome p1-c5 | 5 / 5 | 0 | 0 | 0.039 / 0.048 | 15/15 | 0.554 / 0.582 | 8.67 | 1680 / 2207 | 49 / 147 |
| Chrome p1-c10 | 10 / 10 | 0 | 0 | 0.072 / 0.094 | 30/30 | 1.179 / 1.514 | 8.32 | 2999 / 3879 | 87 / 211 |
| Chrome p1-c20 | 20 / 20 | 0 | 0 | 0.179 / 0.239 | 60/60 | 2.638 / 4.036 | 7.47 | 5304 / 7000 | 127 / 246 |
| Chrome p1-c25 | 25 / 25 | 0 | 0 | 0.232 / 0.277 | 75/75 | 3.396 / 5.314 | 7.29 | 6714 / 8753 | 143 / 256 |
| Chrome p2-c50 | 50 / 50 | 0 | 0 | 0.429 / 0.582 | 150/150 | 3.693 / 5.564 | 11.79 | 10270 / 14641 | 265 / 387 |

Every case recorded zero of each of the following:
- context-creation failures, navigation failures, and identity mismatches;
- crashes, operation timeouts, and cleanup failures;
- leaked contexts, and new browser processes after shutdown.

Context-creation p95 was ≤ 0.11 s for both backends. The health probe after each churn
sweep took 0.09–0.17 s. Shutdown took ≤ 0.22 s. Application RSS stayed around
145–177 MB.

Checks/s peaks at 5 contexts per process and then falls. Per-restore latency grows
roughly linearly with live contexts on both backends, so one Chrome process's page
throughput is the shared ceiling. More contexts add latency, not throughput. This
applies equally to Patchright and Chrome; it is not a Patchright limit. Patchright's
local restore latency was about 10–30% higher than Chrome's. Its tree RSS was up to
about 35% higher at low context counts and within about ±10% from 10 contexts up.

## C. Creation/navigation/close churn

| Workers | Backend | Cycles/s | Cycle p50 / p95 / max (s) | Close p95 (s) | Peak browser contexts | Contexts after | Failures / stuck calls | Health probe (s) | Restarts |
|---:|---|---:|---|---:|---:|---|---|---:|---:|
| 1 | Patchright | 10.12 | 0.097 / 0.111 / 0.152 | 0.005 | 1 | 0 | 0 / 0 | 0.109 | 0 |
| 5 | Patchright | 8.98 | 0.557 / 0.599 / 0.735 | 0.073 | 5 | 0 | 0 / 0 | 0.104 | 0 |
| 10 | Patchright | 8.13 | 1.233 / 1.268 / 1.835 | 0.089 | 9 | 0 | 0 / 0 | 0.112 | 0 |
| 20 | Patchright | 7.94 | 2.536 / 2.625 / 3.123 | 0.113 | 10 | 0 | 0 / 0 | 0.109 | 0 |
| 1 | Chrome | 10.70 | 0.092 / 0.102 / 0.135 | 0.004 | 1 | 0 | 0 / 0 | 0.109 | 0 |
| 5 | Chrome | 9.08 | 0.548 / 0.602 / 0.688 | 0.074 | 5 | 0 | 0 / 0 | 0.106 | 0 |
| 10 | Chrome | 8.73 | 1.143 / 1.207 / 1.447 | 0.105 | 6 | 0 | 0 / 0 | 0.102 | 0 |
| 20 | Chrome | 8.70 | 2.292 / 2.423 / 2.532 | 0.104 | 6 | 0 | 0 / 0 | 0.109 | 0 |

- **No wedge:** after each level the process stayed connected, and a fresh context
  navigated in about 0.1 s. The wedged-but-connected failure that Phase 6 found for
  unserialized Camoufox at 5 contexts **did not occur** for Patchright at 1–20 workers.
- **No growth or leaks:** the context count never exceeded the worker count, and both
  manager and browser-reported contexts returned to 0 after every level. One process
  lasted all 1,200 cycles with 0 restarts and 0 cleanup failures.
- **Shutdown:** 0.073 s for Patchright and 0.057 s for Chrome.
- **Throughput:** cycles/s does not scale with workers on either backend. Context
  creation runs under the manager's allocation lock and page creation is bounded by the
  browser, so extra workers mainly add latency.

## A. Repeated park/reopen

A 50-session HYBRID population was created with the real creator. Each sweep then ran
this path through the real scheduler and restorer:

claim → fresh context → restore → verify → inspect → save → close → park

| Backend | Creation | Sweeps | Verified restores | Mismatches | Restore p50 / p95 / max (s) | Sweep duration (s) | Peak contexts | Contexts / leases after each sweep | Browser restarts |
|---|---|---:|---:|---:|---|---|---:|---|---:|
| Patchright | 50/50 in 4.16 s | 6 | **300/300** | **0** | 0.32 / 0.33–0.35 / 0.36 | 4.17–4.20 | 4 | 0 / 0 | 0 |
| Chrome | 50/50 in 3.75 s | 6 | 300/300 | 0 | 0.29 / 0.31 / 0.32 | 3.75–3.78 | 4 | 0 / 0 | 0 |

- **Same managed process:** as above; no new identities appeared during the sweeps.
- **Browser process restart:** each of 3 cycles closed every process, launched 2 new
  ones, restored all 50 sessions, and shut down.

  | Backend | Launch (s) | Restore all 50 (s) | Verified per cycle | Restore p50 / p95 (s) | Contexts / processes left |
  |---|---|---|---|---|---|
  | Patchright | 0.44–0.51 | 4.58–4.73 | 50/50 | 0.18 / 0.23 | 0 / 0 |
  | Chrome | 0.43–0.49 | 4.00–4.02 | 50/50 | 0.16 / 0.17–0.18 | 0 / 0 |

- **Application/repository restart:** the harness stranded 5 leases with a "crashed"
  worker and added a row whose persisted provenance is the other backend.
  - After a restart on new repository, state, and browser objects, all 5 expired leases
    were detected and recovered, and all 50 sessions verified.
  - Startup to browser-ready took 0.47 s on both backends.
  - The foreign row was rejected with `BACKEND_MISMATCH` before any context opened. Its
    state file stayed byte-identical and its Queue ID and provenance were kept.
  - The reverse direction was also rejected without launching a browser.
  - Pairing: Patchright ↔ Chrome.

Across all of A, persisted Queue IDs were identical before and after every sweep and
restart, the simulator issued no new identity, and there were 0 context or process
leaks.

## D + K. Monitoring-shaped workload (production scheduler/restorer)

100 persisted sessions (20 checks/s offered). All sessions start due, which is an
initial burst; "steady" excludes the first 5 s.

| Metric | Patchright 2×4 | Chrome 2×4 | Patchright 2×8 | Chrome 2×8 |
|---|---:|---:|---:|---:|
| Checks in 60 s | 724 | 784 | 736 | 800 |
| Checks/s (steady) | 12.07 (12.15) | 13.07 (13.16) | 12.27 (12.36) | 13.33 (13.45) |
| Check p50 / p95 / max (s) | 0.327 / 0.355 / 0.412 | 0.301 / 0.334 / 0.399 | 0.638 / 0.726 / 0.879 | 0.591 / 0.665 / 0.849 |
| Restore p50 / p95 (s) | 0.325 / 0.352 | 0.299 / 0.332 | 0.636 / 0.723 | 0.590 / 0.664 |
| Context acquisition p50 / p95 (s) | 0.050 / 0.103 | 0.049 / 0.101 | 0.157 / 0.306 | 0.152 / 0.324 |
| Allocation-lock wait p95 (s) | 0.082 | 0.081 | 0.254 | 0.210 |
| Active-context peak | 4 | 4 | 8 | 8 |
| Queue depth avg / peak (cap) | 7.2 / 8 (8) | 7.5 / 8 (8) | 15.1 / 16 (16) | 15.2 / 16 (16) |
| Due backlog avg / peak / final | 29 / 80 / 28 | 24 / 77 / 27 | 16 / 68 / 12 | 11 / 68 / 11 |
| Oldest overdue avg / peak (s) | 2.5 / 8.1 | 1.9 / 7.1 | 1.5 / 7.1 | 0.9 / 6.0 |
| Failures / identity mismatches | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Restarts / op timeouts / nav failures / ctx failures | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Drain after stop (s) | 0.87 | 0.79 | 1.35 | 1.57 |
| App CPU avg / peak %, RSS MB | 19 / 22, 150 | 21 / 24, 162 | 22 / 28, 150 | 25 / 31, 162 |
| Browser CPU avg / peak % | 238 / 274 | 230 / 248 | 258 / 299 | 257 / 270 |
| Browser tree RSS avg / peak MB | 2127 / 3072 | 1925 / 2578 | 2964 / 4372 | 2627 / 3221 |
| Browser tree processes avg / peak | 16 / 22 | 15 / 18 | 22 / 30 | 20 / 23 |
| Managed / OS main processes | 2 / 2 | 2 / 2 | 2 / 2 | 2 / 2 |

- **Capacity below demand:** on this host both backends deliver about 12–13 checks/s
  against 20/s offered.
- **Backlog stays bounded:** it drained from the initial burst to a stable level. The
  queue never exceeded its capacity, and contexts never exceeded the worker count.
- **More workers, same throughput:** going from 4 to 8 workers roughly doubled
  per-check latency without adding throughput on either backend. This is the same
  process-throughput ceiling seen in B and C.
- **Candidate vs control:** Patchright's throughput was about 8% lower than Chrome's,
  and its latency and RSS were a little higher. There was no qualitative difference.
- **Scope:** these numbers are for a 100-session population on this host only.

## E. Browser process failure

All kills are real `SIGKILL`s of a managed top-level Chrome process.

| Scenario | Patchright | Chrome |
|---|---|---|
| Kill with 0 / 1 / 3 active contexts | detected 0.05 s; replaced 0.20–0.22 s after kill (restart 0.15–0.17 s); lost contexts counted 0 / 1 / 3; capacity released; exactly 1 process after | detected 0.05 s; replaced 0.20–0.21 s (restart 0.15–0.16 s); same accounting |
| Two slots, kill one | only the failed slot replaced (0.15 s); healthy slot navigated during the failure; 2 processes after | same (0.17 s) |
| Two kills during a monitoring sweep (pages slowed 1 s, queue depth peak 8 = queued work present) | 50/50 verified in 25.6 s; 2 restarts; 1 lost context; 3 restores fell back TRANSFER → STORAGE_STATE; 0 leases after; no row FAILED | 50/50 in 25.7 s; same counts |

In every scenario the expected Queue IDs were unchanged, the simulator issued no
replacement identity, and a post-recovery restore verified the expected Queue ID.

In the monitoring-kill scenario, the in-flight attempts were absorbed by the HYBRID
fallback and monitor retries. So no row needed a later retry sweep (0 rows were
`CONNECTION_LOST`). Retryable status after a kill is covered by G and by the manual-kill
check (H).

## F. Repeated process kills

20 kill cycles on one slot, each with a live context:

| Backend | Recovered | Restart p50 / p95 / max (s) | Restart failures | Processes per cycle | Context leaks | Sampled restores |
|---|---:|---|---:|---|---:|---|
| Patchright | 20/20 | 0.154 / 0.185 / 0.186 | 0 | exactly 1 every cycle | 0 | all verified |
| Chrome | 20/20 | 0.150 / 0.177 / 0.182 | 0 | exactly 1 every cycle | 0 | all verified |

Restart time did not trend upward over the cycles, so there was no degraded-restart
behaviour.

## G. Stuck/slow navigation

The simulator delayed responses for 4 of 10 synthetic identities by 30 s. That is far
beyond the 2 s navigation timeout and the 6 s attempt deadline.

| Check | Patchright | Chrome |
|---|---|---|
| Sweep with 4 stuck + 6 healthy sessions (2 workers) | completed in 27.9 s; 6/6 healthy verified | completed in 27.5 s; 6/6 |
| Deadlines fired | 12 TRANSFER `NAVIGATION_FAILED` + 12 STORAGE `STATE_CONTEXT_FAILED` (3 monitor attempts × 4 sessions) | identical |
| Max restore vs bound (2 attempts × 6 s + 1 s) | 4.32 s ≤ 13 s | 4.32 s ≤ 13 s |
| Leases after / stuck rows | 0 / all `CONNECTION_LOST` (retryable, not FAILED, unowned) | same |
| 15 repeated timeouts on one process, each with a concurrent healthy probe | 15/15 timeouts fired; healthy probes p50 0.187 s; process connected; 0 restarts | 15/15; p50 0.176 s; 0 restarts |
| Retry after pages recover | 4/4 verified the same expected Queue IDs | 4/4 |
| Identity | 0 mismatches, 0 replacement identities | same |
| Shutdown after timeouts | 0.044 s | 0.043 s |

**Health heuristic decision:** repeated navigation timeouts never made a Patchright
process unhealthy. Healthy work on the same process kept completing in about 0.19 s, and
a fresh probe succeeded. So the existing generic recovery stays unchanged:
- replacement on disconnect;
- replacement on an abandoned (cancellation-ignoring) operation;
- no consecutive-timeout restart threshold for Patchright
  (`unresponsive_restart_threshold = None`).

Copying Camoufox's 3-timeout restart would have discarded healthy in-flight contexts
with no evidence of benefit.

## H. Manual headed workflow under load

This used the real operator runtime with a visible headed Patchright window while
automatic monitoring ran. Both backends passed 14/14 checks:

- **Open and Close:** Open restored the persisted identity, and explicit Close released
  ownership.
- **Window loss:** closing the page released ownership.
- **Process kill:** a headed-pool `SIGKILL` released ownership, kept the Queue ID, and
  left a retryable status. The next Open rebuilt the journey.
- **Shutdown with a window open:** everything was released, including all processes.
  Patchright took 0.46 s, Chrome 0.28 s. After restart, Open restored the same Queue ID.
- **Concurrency and churn:** two concurrent Opens and Closes worked. 10 concurrent
  open/close churn cycles had 0 open failures and 0 stalled closes (Patchright Open p50
  0.29 s, Close p50 0.06 s).
- **Identity:** the manual paths created no identities.

## I. Pause/resume under failure

With monitoring paused, the automatic browser was killed. Both backends passed 7/7:

- there were no automatic claims and no rows were checked while paused;
- Refresh Now succeeded after the kill, relaunched the automatic slot, and kept the
  Queue ID;
- manual Open worked while paused;
- after resume, every row was checked again within 2.01 s on both backends.

## J. Shutdown under load

| In flight at shutdown | Patchright (s) | Chrome (s) |
|---|---:|---:|
| Idle | 0.07 | 0.04 |
| Automatic monitoring on 3 s pages | 9.78 | 9.71 |
| Automatic + 2 running / 1 queued operator actions + headed window | 6.74 | 6.46 |
| Automatic + browser kill (restart in flight) | 9.94 | 9.68 |
| Stuck pages + automatic + operator work | 30.09 | 30.10 |
| Phase 6 scenario (automatic 2, operator 2 running / 1 queued, headed 1) | 6.73 | 3.39 |

The stuck-page case uses a 90 s simulated page, longer than the application's own 30 s
navigation timeout. A pilot run also measured 30.1 s at both 30 s and 90 s page delays.
So shutdown is capped by the application's deadlines, not by the page:
- the shutdown stages (tasks, then operator work, then headed windows and browsers) are
  each capped by `SHUTDOWN_TIMEOUT_SECONDS=10`;
- **total** shutdown is therefore bounded by about the number of stages ×
  `SHUTDOWN_TIMEOUT_SECONDS`, as Phase 6 documented.

Every combination ended with zero browser processes, zero automatic leases, zero manual
owners, identical persisted Queue IDs, and no identities created. All were within the
45 s bound (`3 × SHUTDOWN_TIMEOUT_SECONDS + 15`). No browser call made shutdown
unbounded.

## Queue ID continuity and provenance

Across 300 park/reopen restores, 150 restart restores, the application restart, 29
process kills per backend, monitoring (about 1,460–1,580 checks per backend), stuck navigation, the manual
workflow, and every shutdown:
- **zero** persisted Queue ID changes;
- **zero** replacement identities;
- **zero** unexpected identity mismatches. The only mismatch was the intentional
  negative test in the restoration-failure matrix, and it was rejected without touching
  the expected ID.

That matrix (transfer 503 → storage fallback; missing, corrupt, and foreign-provenance
state; navigation failure; identity mismatch) behaved identically on both backends.
Backend provenance was enforced in both directions before any context was created.

## Defect found and fixed

**The inherited restore-deadline check was too tight.**

- **Where:** the Phase 6 failure-during-monitoring scenario, reused unchanged.
- **Problem:** it required a whole restore to finish within **one** attempt deadline.
  A HYBRID restore makes up to two independently bounded attempts (TRANSFER, then
  STORAGE_STATE).
- **What happened:** in both backends, 3 restores whose TRANSFER attempt was in flight
  on the killed process waited until the 23 s attempt deadline cancelled them. They then
  restored through storage state in about 1.2 s, for a total of 24.18 s on Patchright
  and 24.16 s on Chrome.
- **Why it's correct behaviour:** the call against the killed process stayed pending
  until the application's deadline, which is the documented Playwright-family behaviour
  that the bounded-call layer exists for. The deadline fired as designed.
- **Fix:** `RecordingRestorer` now records the attempt count, and the check bounds each
  restore by `attempts × attempt deadline + 1 s`. The scenario was re-run for both
  backends and passed. It reproduced the same 24.18 s / 24.16 s maxima, which are within
  the corrected bound.
- **Not Patchright-specific:** Chrome behaves identically.
- **Possible follow-up (backend-independent):** after a detected disconnect, cancel an
  in-flight attempt instead of waiting out its deadline.

Supporting harness changes:
- Phase 6 `make_backend` now constructs `PatchrightBackend`.
- Provenance checks accept an explicit foreign backend, so Phase 7 pairs
  Patchright ↔ Chrome and never touches Camoufox.

No production code changed in this prompt.

## Patchright-specific limits

**None required.** The evidence does not justify any Patchright-specific concurrency
bound, serialization, per-process context limit, or health heuristic:
- 25 contexts per process and 50 across the default 2 processes were healthy under hold,
  reacquisition, and churn;
- 1,200 churn cycles on one process produced no wedge;
- repeated timeouts left the process healthy.

Patchright therefore keeps the ordinary Chrome concurrency model and existing ceilings.
The practical capacity limit on this host is shared with Chrome: per-process page
throughput peaks around 5 contexts per process for this local workload. That is a sizing
input for worker defaults, not a Patchright defect.

## PASS / FAIL / UNKNOWN

| Finding | Result |
|---|---|
| Queue ID never silently changes (A, D, E, F, G, H, I, J) | **PASS** (0 changes, 0 replacements) |
| Sessions park and restore (300 same-process, 150 across restarts, app restart) | **PASS** |
| Bounded queues, workers, contexts, and processes; no population-sized allocation | **PASS** |
| No context or process leaks | **PASS** |
| Every browser call bounded (incl. killed-process and 90 s stuck pages) | **PASS** |
| Process crash/restart recoverable, incl. 20 repeated kills | **PASS** |
| Persisted sessions survive browser and application restart | **PASS** |
| Backend provenance enforced both ways | **PASS** |
| Multiple live Patchright contexts per process (1–25; 50 on 2 processes) | **PASS** |
| Wedged-but-connected Patchright process under churn | **Not observed** (1,200 cycles, 1–20 workers) |
| Manual headed ownership under load | **PASS** |
| Pause/resume under failure | **PASS** |
| Bounded shutdown under load | **PASS** (stage-composed bound, as in Phase 6) |
| Levels above 25 per process / above 50 total | **NOT RUN** (architectural limits; not needed for the default) |
| Queue-it staging restore, admission, throughput, safe concurrency | **NOT RUN / UNKNOWN** |
| Anti-detection/fingerprint behaviour | **Not measured, not claimed** |
| Other hosts/OSes | **UNKNOWN** |

## Decision gate

**READY_FOR_DEFAULT_DECISION.**

On controlled local evidence Patchright met every core invariant:
- Queue ID continuity;
- park/restore;
- bounded resources with no leaks;
- bounded browser calls;
- crash/restart recovery;
- persistent-session survival;
- correct provenance.

It did so across all benchmark families, matching the Chrome control on every check.
Its cost relative to Chrome is modest: about 10–30% higher local restore latency, about
8% lower monitoring throughput, and slightly higher tree RSS. No Patchright-specific
limit is required.

This clears the default-backend **decision**. The decision itself, and any migration, is
Phase 7 Prompt 6. Chrome stays the default until then, and Queue-it staging remains
**NOT RUN / UNKNOWN**.

## Validation

- Full Patchright + Chrome benchmark (headed manual pool): 30/30 scenarios and 494/494
  checks after the corrected failure-during-monitoring re-run (first run 492/494; see
  above). 0 leftover browser processes.
- Focused recovery, backend, and Patchright tests: `test_phase7_patchright_benchmark`,
  `test_phase6_camoufox_benchmark`, `test_browser_manager`, `test_browser_backend`,
  `test_restoration`, `test_phase4_resilience`, the Patchright
  preflight/identity/runtime units, installed-Patchright and Patchright
  queue-continuity integration, and Chrome UI recovery integration: **122 passed**.
- Full non-staging suite: **540 passed, 4 staging deselected**. One earlier full-suite
  pass hit a timing flake in
  `test_operator_fencing.py::test_chrome_loss_while_open_is_detected_without_relaunch`.
  It passed 3/3 in isolation, passed on the unmodified tree, and passed in the clean
  re-run; that test does not import the changed harness modules.
- `ruff check src tests`: PASS. `mypy src` (strict, 76 files): PASS. `pip check`: PASS.

## Next task

**Phase 7 Prompt 6 — Patchright Default Migration and Final Acceptance.**
