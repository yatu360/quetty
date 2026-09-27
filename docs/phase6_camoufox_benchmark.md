# Phase 6 Camoufox Queue-Session Recovery and Capacity Benchmark

**Date:** 2026-09-27
**Scope:** Phase 6 Prompt 5. Local simulator only; **no Queue-it or staging traffic**
**Harness:** `queue-load-test-phase6-camoufox-benchmark --headed --output ...`
(`src/queue_load_test/harness/phase6_camoufox_benchmark.py`)
**Aggregate result:** `docs/results/phase6_camoufox_benchmark_result.json`
**Outcome:** 22/22 scenarios PASS, 282/282 checks PASS, 0 leftover browser processes.
Camoufox stays **opt-in**.

## Evidence boundary

Everything below was measured against `LocalQueueSimulator` on 127.0.0.1.
- Local simulator checks/s is a local page rate. It is not Queue-it throughput.
- Local restoration is not Queue-it restore reliability.
- None of the tested context levels is known to be staging-safe.
- Nothing here says Camoufox is undetectable or better at anti-bot evasion. That was
  not measured and is not a goal.

The durable identity under test is the **persisted expected Queue ID**. Fresh contexts
may present different fingerprint or device characteristics, and the harness never
reads, compares, or persists canvas, WebGL, font, or other fingerprint values. A changed
Queue ID would be a failure. None occurred.

The report contains no Queue IDs, session IDs, transfer URLs, storage state, or
fingerprint data. Identity continuity is checked in memory against the persisted
`(session, Queue ID)` map.

## Host and versions

| Item | Value |
|---|---|
| Host | macOS 26.5.1 arm64, Apple M5 Pro, 15 logical CPUs, 24 GiB RAM |
| Python | 3.14.7 (project targets `>=3.12`) |
| Playwright | 1.62.0 |
| Camoufox package | 0.5.6 |
| Camoufox browser build | 152.0.4-beta.30 |
| Chrome (comparison) | installed Google Chrome 153.0.8010.54 |
| Run duration | 468 s total |

## Configuration

- **Capacity:**
  - context levels 5, 10, 20, 30, 40, 50;
  - shared-process cases use `ceil(contexts / 25)` processes;
  - per-process cases use 1–4 processes with one live context each;
  - 3 navigation waves per hold phase; 0.25 s resource sampling; 10 s navigation
    timeout;
  - one real-scheduler churn sweep over `max(8, 2 × contexts)` due sessions, with
    workers = contexts.
- **Production profile for recovery scenarios:**
  - 2 browser processes, 4 monitor workers, HYBRID mode;
  - Camoufox serialized to one live context per process;
  - 40-session population, 5 park/reopen sweeps, 3 full browser restart cycles,
    10 repeated kill cycles.
- **Manual pool:** visible headed browser. The operator runtime uses the Phase 5
  controlled settings (1 automatic process, `MAX_MANUAL_OPEN_SESSIONS=2`, 2 operator
  workers, 2 monitor workers, a 6 s manual lease, and `SHUTDOWN_TIMEOUT_SECONDS=10`).
- **Process accounting (browser-neutral):**
  - Camoufox tree = the top-level `camoufox` process plus its `plugin-container`
    children;
  - Chrome tree = processes named `*chrome*`;
  - "main" = the backend's top-level browser process.
  - Summed RSS may count shared pages more than once. CPU 100% = one logical core.

## 1–3. Capacity and same-host Chrome comparison

### Shipped profile: one live context per process (processes ≤ 4)

`CHROME_PROCESS_COUNT` is limited to 4, and Camoufox 0.5.6 is serialized to one live
context per process. Under the existing configuration, **the maximum number of
simultaneously live automatic Camoufox contexts is therefore 4**. Chrome was run with
the same processes, contexts, workers, duration, and sampling for a like-for-like
comparison.

| Case | Main processes (observed peak) | Tree processes (peak) | Restore p50 / p95 / max (s) | Local checks/s | Browser CPU avg / peak % | Browser tree RSS avg / peak MB | Failures |
|---|---:|---:|---|---:|---|---|---:|
| Camoufox p1 | 1 | 6 | 0.328 / 0.348 / 0.349 | 2.90 | 138 / 219 | 1061 / 1610 | 0 |
| Camoufox p2 | 2 | 12 | 0.406 / 0.423 / 0.424 | 4.81 | 247 / 532 | 2015 / 3447 | 0 |
| Camoufox p3 | 3 | 18 | 0.461 / 0.476 / 0.479 | 5.47 | 299 / 694 | 2531 / 4546 | 0 |
| Camoufox p4 | 4 | 24 | 0.529 / 0.553 / 0.553 | 6.77 | 352 / 860 | 3143 / 5710 | 0 |
| Chrome p1 | 1 | 7 | 0.133 / 0.136 / 0.136 | 7.00 | 79 / 155 | 646 / 983 | 0 |
| Chrome p2 | 2 | 15 | 0.146 / 0.158 / 0.160 | 10.84 | 123 / 278 | 1049 / 1956 | 0 |
| Chrome p3 | 3 | 21 | 0.168 / 0.175 / 0.176 | 12.31 | 140 / 356 | 1226 / 2882 | 0 |
| Chrome p4 | 4 | 28 | 0.218 / 0.229 / 0.231 | 14.41 | 174 / 499 | 1579 / 3305 | 0 |

Every case above reached and held its configured contexts and restored 8/8 sessions.
There were zero Queue ID mismatches, crashes, operation timeouts, and cleanup failures,
and each case ended with zero contexts and zero browser processes.

Context-creation p95 was ≤ 0.18 s for Camoufox and ≤ 0.006 s for Chrome. Allocation
lock-wait p95 was ≤ 0.011 s for both. Application RSS was about 150–180 MB throughout.
The 8-session sweeps are short, so checks/s is noisy.

### Shared processes (many contexts per process)

| Case | Processes | Achieved / peak contexts | Hold navigation p50 / p95 (s) | Hold nav failures | Churn sweep restores | Restore p50 / p95 (s) | Browser tree RSS avg / peak MB | Browser CPU avg / peak % |
|---|---:|---|---|---:|---|---|---|---|
| Camoufox unserialized c5 | 1 | 5 / 5 | 0.050 / 0.079 | 0 | **0 / 30** | 10.55 / 10.66 (timeouts) | 2314 / 2505 | 41 / 455 |
| Camoufox unserialized c10 | 1 | 10 / 10 | 0.076 / 0.127 | 0 | skipped | n/a | 1549 / 3145 | 173 / 424 |
| Camoufox unserialized c20 | 1 | 20 / 20 | 0.158 / 0.235 | 0 | skipped | n/a | 2324 / 3888 | 199 / 440 |
| Camoufox unserialized c30 | 2 | 30 / 30 | 10.00 / 10.02 | **75 / 90** | skipped | n/a | 6711 / 7280 | 155 / 791 |
| Chrome c5 | 1 | 5 / 5 | 0.042 / 0.057 | 0 | 10 / 10 | 0.540 / 0.570 | 1451 / 2335 | 138 / 238 |
| Chrome c10 | 1 | 10 / 10 | 0.076 / 0.102 | 0 | 20 / 20 | 1.076 / 1.401 | 2363 / 4025 | 175 / 292 |
| Chrome c20 | 1 | 20 / 20 | 0.172 / 0.203 | 0 | 40 / 40 | 2.270 / 3.713 | 4863 / 7682 | 205 / 280 |
| Chrome c30 | 2 | 30 / 30 | 0.253 / 0.277 | 0 | 60 / 60 | 2.156 / 2.703 | 6829 / 11415 | 331 / 459 |
| Chrome c40 | 2 | 40 / 40 | 0.378 / 0.438 | 0 | 80 / 80 | 2.874 / 4.582 | 9277 / 14182 | 347 / 479 |
| Chrome c50 | 2 | 50 / 50 | 0.515 / 0.617 | 0 | 100 / 100 | 3.274 / 5.718 | 10216 / 16259 | 349 / 450 |

**Why the Camoufox unserialized cases stopped:**
- **Churn at 5 contexts.** When fixed workers repeatedly create, navigate, and close
  fresh contexts on one unserialized Camoufox process, the process wedged. After the
  first stalled navigation, every later navigation hit the 10 s timeout, and 0/30
  restores succeeded. The process still reported `is_connected()`, so the manager
  never restarted it.
- **Reproduced in isolation.** A standalone probe reproduced this with 4 workers and 12
  items per sweep: 11/12, then 0/12, then 0/12. Plain simultaneous, staggered, and
  sequential creation without continuous churn did not stall.
- **Churn sweeps skipped above 5.** The churn sweep was not repeated at larger
  unserialized levels.
- **Hold-phase failure at 30.** At 30 contexts across 2 processes, 75/90 hold-phase
  navigations timed out. This is objective stability evidence, so the family stopped
  and 40/50 were **not run**.

Chrome reached 50 contexts with no failures. Its summed tree RSS peak (16.3 GB) stayed
below the 80% host-RAM stop threshold, subject to the shared-page double-counting
caveat. Chrome was not increased further, and 75/100 were not run.

These are descriptive results, not a winner. The practical consequence for Quetty is
that Camoufox's usable automatic concurrency is bounded by process count, not by
contexts per process.

## 4. Queue-session park/reopen (production profile)

The real creator acquired 40 sessions with HYBRID state. Each of 5 sweeps then ran the
real scheduler and monitor:

claim → fresh context → restore → observe → verify → inspect → save → close → release
→ park

| Backend | Creation | Sweeps (5) | Verified restores | Mismatches | State-refresh failures | Restore p50 / p95 / max (s) | Sweep duration (s) | Peak contexts | Contexts / leases after each sweep |
|---|---|---|---:|---:|---:|---|---|---:|---|
| Camoufox | 40/40 in 8.4 s (p50 0.78 s) | all complete | **200 / 200** | **0** | 0 | 0.79–0.82 / 0.89–1.03 / ≤ 1.38 | 8.44–8.97 | 2 | 0 / 0 |
| Chrome | 40/40 in 2.9 s (p50 0.28 s) | all complete | 200 / 200 | 0 | 0 | 0.27–0.29 / 0.28–0.31 / ≤ 0.32 | 2.75–3.05 | 4 | 0 / 0 |

Across every sweep, the Queue IDs before and after were identical, and the simulator
created no new identities. Camoufox's peak of 2 live contexts reflects the per-process
lease: 4 workers share 2 processes.

## 5. Full browser process restart

Three cycles each closed every browser process, launched a new manager and processes,
restored all 40 parked sessions in fresh contexts, and closed again.

| Backend | Launch (2 processes) | Restore all 40 | Verified | Contexts after | Processes after shutdown |
|---|---|---|---:|---:|---:|
| Camoufox | 1.03–1.05 s | 7.68–7.78 s | 40/40 each cycle | 0 | 0 |
| Chrome | 0.41–0.42 s | 3.02–3.08 s | 40/40 each cycle | 0 | 0 |

## 6. Application restart

Before stopping the runtime, repository, and browser, the harness set up two conditions:
- **Stranded leases:** it claimed 5 sessions with a fake "crashed" worker and a 1 s
  lease.
- **Foreign-backend row:** it added a session from the other backend, with its own
  storage-state file.

After restart on new objects over the same files:
- **Lease recovery:** the 5 expired leases were reported at startup and reclaimed by the
  scheduler, and all 40 same-backend sessions verified their expected Queue ID.
- **No identity changes:** persisted Queue IDs were unchanged, and the simulator
  created no identities.
- **Foreign row rejected, state untouched:** the foreign-backend row failed with
  `BACKEND_MISMATCH` on every attempt (3, with retries) before any context opened. Its
  Queue ID and provenance were kept, and its state file was byte-identical afterwards.
- **Reverse direction:** a restorer for the other backend rejected this backend's
  session without launching a browser, and left its state and Queue ID untouched.
- **Startup time:** startup to browser-ready was 1.08 s for Camoufox and 0.43 s for
  Chrome.

## 7–10. Browser process failure

All kills are real `SIGKILL`s of the backend's top-level browser process.

| Scenario | Camoufox | Chrome |
|---|---|---|
| Kill with 0 / 1 / 3 active contexts | detected 0.05 s; replaced 0.49–0.50 s after kill (restart 0.44 s); lost contexts counted 0 / 1 / 3; capacity released; 1 main process after | detected 0.05 s; replaced 0.20 s (restart 0.15 s); same accounting |
| Two slots, kill one | only the failed slot replaced (restart 0.46 s); healthy slot navigated during the failure; 2 main processes after | same (restart 0.14 s) |
| Repeated kill ×10 | 10/10 recovered; restart p50 0.444 / p95 0.460 / max 0.466 s; 0 restart failures; exactly 1 main process every cycle; 0 context leaks | 10/10; p50 0.148 / p95 0.160 s |
| Kill twice during a monitoring sweep (pages slowed 1 s) | sweep completed 40/40 verified in 34.3 s; max restore 9.56 s vs 23 s attempt deadline; 1 transfer attempt fell back to same-backend storage state; 2 restarts; 0 leases after; no row FAILED | 40/40 in 12.5 s; 4 transfer attempts fell back to storage state |

For every scenario, persisted Queue IDs were unchanged after recovery, and a
post-recovery restore verified the expected Queue ID. The 3-context case ran
unserialized, because only that mode lets one Camoufox process hold several contexts.

In the monitoring-kill scenario, the in-flight contexts were closed by their own failing
restore attempts before the restart ran, so `lost_contexts` there was 0. The attempts
were absorbed by the HYBRID fallback and monitor retries, so no row needed a later retry
sweep. Retryable status after a kill is covered separately by the manual-kill check.

## 11. Restoration failure cases

Each case ran on dedicated sessions created by the backend itself. The expected Queue ID
was preserved in **every** case, and no identities were created.

| Case | Attempts | Final result |
|---|---|---|
| Transfer failure (HTTP 503) | TRANSFER `HTTP_FAILURE` → STORAGE_STATE success | **restored** via same-backend state |
| Missing storage state | TRANSFER `HTTP_FAILURE` → `STATE_MISSING` | failed, retryable |
| Corrupt storage state | TRANSFER `HTTP_FAILURE` → `STATE_CORRUPT` | failed (permanent classification) |
| Backend provenance mismatch | `BACKEND_MISMATCH` before any context | rejected, no browser work |
| Navigation failure (connection closed) | TRANSFER `NAVIGATION_FAILED` → `STATE_CONTEXT_FAILED` | failed, retryable |
| Identity mismatch | TRANSFER and STORAGE_STATE `IDENTITY_MISMATCH` | failed; expected ID kept |

Results were identical for Chrome and Camoufox.

## 12. Headed/manual failure (visible headed pool)

These ran through the real operator runtime (`ApplicationRunRuntime`). Camoufox passed
14/14 checks and Chrome 14/14.
- **Open and Close:** Open restored the persisted identity, and explicit Close released
  ownership.
- **Window close:** closing the page released ownership.
- **Process kill:** `SIGKILL` of the headed pool (2 Camoufox processes, 1 Chrome) also
  released ownership. The row kept its Queue ID and a non-FAILED status, and the next
  Open reconstructed the journey.
- **Shutdown with a window open:** application shutdown released everything and left
  zero processes (Camoufox 0.69 s). After restart, Open reconstructed the same Queue ID.
- **Concurrent churn:** 10 cycles of two concurrent Opens then two concurrent Closes had
  0 open failures and 0 stalled closes. Camoufox Open p50 was 0.50 s and Close p50
  0.06 s.

## 13. Pause/resume under failure

With automatic monitoring paused, the harness killed the automatic browser.
- **No automatic work while paused:** there were zero new automatic claims and no row
  was checked.
- **Operator paths kept working:** Refresh Now succeeded while paused and after the kill,
  relaunching the automatic slot, and kept the Queue ID. Manual Open worked while
  paused.
- **Resume:** after resume, every row was checked again within 2.26 s (Camoufox) and
  2.01 s (Chrome).

## 14. Shutdown under load

At the moment of shutdown, work was in flight in every category:

| In flight at shutdown | Camoufox | Chrome |
|---|---:|---:|
| Automatic checks | 2 | 1 |
| Operator actions running | 1 | 1 |
| Operator actions queued | 2 | 2 |
| Headed windows open | 1 | 1 |
| Shutdown duration | 13.8 s | 3.4 s |

Pages were slowed to 3 s. Both backends finished with zero contexts, zero unintended
browser processes, zero automatic leases, zero manual owners, persisted identities
intact, and no identities created.

Camoufox shutdown exceeded the configured 10 s `SHUTDOWN_TIMEOUT_SECONDS`. That timeout
bounds each shutdown stage rather than the total, and serialized contexts drain one per
process. Shutdown is bounded, but total shutdown time is not capped by that single
setting.

## 15. Cross-engine storage provenance

Prompt 3's matrix stands, and no cross-engine storage restoration is used as a recovery
fallback. Provenance is enforced before any context is created, in both directions
(§6, §11). Cross-backend *transfer* restoration is also refused by the runtime, so it
was not evaluated. Queue-it cross-engine behavior remains **UNKNOWN**.

## Defects found and fixed in this prompt

1. **Resource accounting was Chrome-only.** `PsutilProcessResourceProbe` counted only
   processes named `*chrome*`, so Camoufox reported zero browser CPU/RSS. The probe is
   now backend-aware.
   - A Camoufox tree is the `camoufox` root plus its descendants. The probe also reports
     top-level processes and exposes browser-neutral `browser_*` properties.
   - The historical `chrome_*` fields are kept for report comparability and documented
     as historical.
2. **Manual final inspection and adoption were not deadline-bounded.**
   - **Observed:** in a controlled shutdown, a watcher waited forever inside the final
     live-page inspection, and application shutdown hung.
   - **Fix:** `QueueSessionRestorer.inspect_open` and `adopt_open` now use the same
     attempt deadline as restores.
   - **On timeout:** `inspect_open` returns a transient `NAVIGATION_FAILED`, which keeps
     the expected Queue ID and leaves the row retryable. `adopt_open` reports "nothing
     seen" and restores its in-memory session fields.
3. **The unserialized manual Camoufox pool was unreliable.** This was introduced in
   Prompt 4.
   - **Observed:** repeated concurrent manual Opens failed the churn check in 3 of 5
     runs.
   - **Fix:** the manual Camoufox pool now gives each window its own bounded process
     (`manual_pool_topology`: Camoufox `MAX_MANUAL_OPEN_SESSIONS` × 1, Chrome 1 × N).
     The manager picks a free slot without waiting under its lock. When every slot is
     busy it raises the existing `BrowserCapacityError`, which Open already reports.
   - **Result:** 5/5 loop runs (50 churn cycles) and the final run passed with 0
     failures.
   - **Cleanup:** the unused `create_browser_backend(..., long_lived_contexts=)` flag
     was removed. `CamoufoxBackend(serialize_contexts=False)` remains, for measurement
     only.

## PASS / FAIL / UNKNOWN

| Finding | Result |
|---|---|
| Queue ID preserved across 200 park/reopen restores, 3 full browser restarts, app restart, 10+ kills, and all failure cases | **PASS** (0 changes, 0 replacements) |
| Expected-ID mismatch never overwrites the persisted ID | **PASS** |
| Browser provenance enforced both directions, state untouched | **PASS** |
| Disconnect detection, lost-context accounting, capacity release, single-slot replacement, no process multiplication | **PASS** |
| Browser failure never creates a replacement identity | **PASS** |
| Leases recovered after restart; zero leases/owners/contexts/processes after every scenario | **PASS** |
| Headed manual Close / window loss / process kill / shutdown / reopen | **PASS** |
| Pause blocks claims only; Refresh/Open work while paused; resume continues | **PASS** |
| Bounded shutdown with automatic, operator (running + queued), and headed work | **PASS** (Camoufox 13.8 s > 10 s stage timeout; see §14) |
| Serialized Camoufox capacity 1–4 processes | **PASS** locally, 0 failures |
| Unserialized Camoufox under create/navigate/close churn | **FAIL** (process wedges at 5 contexts, still reports connected) |
| Unserialized Camoufox hold at 30 contexts / 2 processes | **FAIL** (75/90 navigation timeouts) |
| Camoufox 40/50-context cases | **NOT RUN** (stopped on evidence) |
| Detection of a wedged-but-connected Camoufox process | **UNKNOWN / not implemented** (not observed under serialization) |
| Camoufox fingerprint continuity | **Not required, not measured** |
| Queue-it staging restore, admission, throughput, safe concurrency | **UNKNOWN** (no staging traffic) |
| Visible headed Camoufox on hosts other than this macOS arm64 machine | **UNKNOWN** |

## Implications for Prompt 6

The browser decision rests on four things, and on all four Camoufox was correct locally:
correct Queue-session restoration, Queue ID preservation, bounded resources and
recovery, and Phase 5 workflow compatibility (61/61).

Its costs differ from Chrome's:
- **Process-bound concurrency:** with the current `CHROME_PROCESS_COUNT` ≤ 4, at most 4
  automatic Camoufox contexts are live at once.
- **Per-restore cost:** Camoufox restores took about 2.5–3× Chrome's local latency, and
  per-live-context process-tree memory and CPU were higher.
- **Wedge detection:** a wedged-but-connected Camoufox process is not detected today.

Before Camoufox becomes the default, Prompt 6 must decide:
- the process ceiling and worker defaults for Camoufox runs;
- whether a per-process health watchdog is needed;
- how shutdown-stage timeouts should compose.
