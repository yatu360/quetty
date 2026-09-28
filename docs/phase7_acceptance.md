# Phase 7 Acceptance: Patchright Default Backend

**Date:** 2026-09-28
**Overall result:** **Phase 7 ACCEPTED on controlled local evidence.** Patchright is the
default backend for **new** runs. Queue-it staging is **NOT RUN / UNKNOWN**.
**Final acceptance result:** `docs/results/phase7_acceptance_result.json`
(`queue-load-test-phase7-acceptance`)

## Final backend policy

| Backend | Role | Status |
|---|---|---|
| **Patchright** | **Default for new runs** (`BROWSER_BACKEND=patchright`, also the default when unset) | Accepted by Phase 7 on local evidence |
| Chrome | Supported fallback/control (`BROWSER_BACKEND=chrome`) | Fallback regression PASS |
| Camoufox | Retained, dormant/experimental (`BROWSER_BACKEND=camoufox`) | **Not certified** by Phase 7, not recommended, excluded from Phase 7 tests and benchmarks, kept for possible future investigation |

**Migration rule:** changing the default never migrates an existing run. An existing
run restarts with its persisted `run_config.browser_backend`:
- Chrome → Chrome;
- Camoufox → Camoufox;
- Patchright → Patchright.

Legacy rows without provenance are still Chrome. Switching backends still requires
**Stop & Reset Run** and a new run. No automatic migration exists.

The prompt asked whether Patchright should *replace Camoufox* as the default. In fact,
Phase 7 Prompt 1 had already moved the default from Camoufox back to Chrome. The
effective change made here is **Chrome → Patchright** for new runs.

Camoufox's backend implementation, config value, persisted provenance, schema
compatibility, preflight, and restart path are all unchanged.

## Versions

| Item | Value |
|---|---|
| Host | macOS 26.5.1 arm64 (Apple M5 Pro), 15 logical CPUs, 24 GiB RAM |
| Python | 3.14.7 (project `>=3.12`) |
| Patchright | 1.63.0 (exact pin), async API, `channel="chrome"` |
| Browser | installed Google Chrome 153.0.8010.54, used by both Patchright and the Chrome fallback |
| Playwright | 1.62.0 (Chrome fallback; also required by retained Camoufox 0.5.6) |
| Camoufox | 0.5.6 / build 152.0.4-beta.30, retained and not exercised |

The installed Chrome build is provenance, not an immutable pin. A new Patchright run
records the Chrome version that its preflight observed.

## What changed in this prompt

- **Defaults:** `Settings.browser_backend` now defaults to `patchright`, and
  `.env.example` now sets `BROWSER_BACKEND=patchright`. `.env.example` also documents
  Chrome as the fallback and Camoufox as uncertified.
- **Harness default:** the Phase 5/7 workflow CLI default follows the application
  default.
- **Setup page:** it now states the backend role, i.e. "default; standard Chrome is the
  fallback", "supported fallback", or "retained experimental backend; not certified by
  Phase 7".
- **Tests:**
  - default Settings → Patchright;
  - a new run with defaults runs the Patchright preflight and records the observed
    build;
  - an explicit Chrome fallback runs no backend preflight and records no build;
  - existing Chrome, Camoufox, and Patchright runs restart unchanged under the
    Patchright default.

  Unit tests use a fake Patchright preflight, so no real browser launches.
- **Acceptance runner:** added `queue-load-test-phase7-acceptance`. It composes the
  existing workflow and benchmark harnesses and adds a Chrome fallback launch check.
- **Docs:** README, operational docs, and project handoff updated. The Phase 6
  default-decision documents are marked superseded.

No identity, resource, recovery, or restoration code changed.

## Decision gates (evidence from Prompts 1–6)

| # | Gate | Result | Evidence |
|---:|---|---|---|
| 1 | Dependency/install compatibility | **PASS** | Exact pins `patchright==1.63.0`, `playwright==1.62.0`, `camoufox==0.5.6` coexist; `pip check` clean (P1, P6) |
| 2 | Successful local preflight | **PASS** | `queue-load-test-patchright-preflight --context-cycles 5`: PASS, 5/5, 0 contexts and 0 processes after (P6) |
| 3 | Temporary BrowserContext architecture works, no redesign | **PASS** | `TEMPORARY_CONTEXTS_PASS`; persistent profiles not needed (P3) |
| 4 | Repeated park/reopen preserves expected Queue ID | **PASS** | 20/20 cycles (P3); 300/300 restores (P5); 90/90 restores in 3 sweeps (P6) |
| 5 | Browser process restart preserves recoverability | **PASS** | full restart 3 × 50/50 (P5); P3 restart cycle |
| 6 | Application restart preserves recoverability | **PASS** | stranded leases recovered and all sessions verified (P5, P6); paused restart and stale ownership (P4, P6 workflow) |
| 7 | Transfer/state/HYBRID restoration correct | **PASS** | transfer, state, HYBRID fallback, missing/corrupt/unavailable state (P3); fault matrix (P5, P6) |
| 8 | Identity mismatch never silently replaces a Queue ID | **PASS** | `IDENTITY_MISMATCH` keeps the expected ID (P3–P6 workflow and matrix) |
| 9 | Full dashboard/runtime workflow | **PASS** | 74/74 headed (P4); 74/74 headed again (P6) |
| 10 | Manual Open/Close ownership | **PASS** | workflow; manual failure 14/14 (P5) |
| 11 | Monitoring pause/resume | **PASS** | workflow; pause-under-failure 7/7 (P5) |
| 12 | Process crash recovery | **PASS** | kills during monitoring and manual work (P4, P6 workflow); 29 kills incl. 20 repeated (P5); crash during monitoring (P6) |
| 13 | Context/navigation failures bounded and observable | **PASS** | stuck-navigation deadlines, timeouts counted, rows retryable (P5, P6); context-creation failure (P3) |
| 14 | Concurrency stable within selected bounds | **PASS** | 1–25 contexts per process and 50 on 2 processes healthy with churn and reacquisition (P5); 50-context default ceiling again (P6) |
| 15 | Shutdown bounded | **PASS** | five-case shutdown matrix incl. 90 s stuck pages, all ≤ 45 s bound (P5, P6) |
| 16 | No context/process/lease leaks | **PASS** | zero after every scenario and run; 0 leftover processes (P5, P6) |
| 17 | Resource use acceptable for the tested profile | **PASS** | about 10–30% higher local restore latency and about 8% lower throughput than Chrome; RSS comparable (P5) |
| 18 | Chrome fallback functional | **PASS** | fallback preflight, 74/74 workflow, 102/102 scenario checks (P6) |

All 18 gates pass, so the default changed to Patchright.

## Final Patchright acceptance run

`queue-load-test-phase7-acceptance` took 456 s. The Patchright manual window was a
visible headed window. Everything targeted `LocalQueueSimulator` on 127.0.0.1.

| Part | Patchright | Chrome fallback |
|---|---|---|
| Preflight | PASS: 1.63.0, Chrome 153.0.8010.54, 5/5 cycles, 0 contexts and 0 processes after | PASS: launch → `data:` context → close; 0 contexts and 0 processes after |
| Application/dashboard workflow (Phase 7 extended) | **74/74** (headed) | **74/74** (controlled headless) |
| Restoration/recovery scenarios | **6/6, 102/102 checks** | **6/6, 102/102 checks** |
| Default-ceiling concurrency (2 × 25 = 50 contexts) | healthy: 150/150 churn restores, 0 failures | healthy: 150/150 |
| Leftover browser processes | 0 | 0 |
| **Decision** | **PASS** | **PASS** |

That is 352/352 checks across both backends.

How the required acceptance items were covered:

| Required item | Where it was exercised (Patchright) | Result |
|---|---|---|
| New run, acquisition, backend provenance | workflow setup, bounded acquisition, provenance and build recorded | PASS |
| Park, repeated restore | workflow monitoring; `park_reopen` 3 sweeps × 30 = 90 verified restores | PASS |
| Monitoring, progress updates | workflow auto-refresh and monitored progress | PASS |
| Pause, resume | workflow pause/in-flight drain/backlog/resume | PASS |
| Refresh Now | workflow, incl. while paused | PASS |
| Add, Replace, Delete | workflow: Add once despite duplicate submit; create-first Replace; Delete idempotent | PASS |
| Headed Open, headed Close, user window close | workflow (visible window); window-loss release | PASS |
| Browser process crash, browser recovery | workflow manual crash (3 processes) and automatic crash; `failure_during_monitoring` (2 kills, 30/30 verified) | PASS |
| Navigation timeout/failure | `stuck_navigation` (deadlines fired, 10 repeated timeouts, process healthy); `restoration_failures` navigation failure | PASS |
| State failure | `restoration_failures`: missing and corrupt state, HYBRID fallback | PASS |
| Identity mismatch | workflow and `restoration_failures` | PASS, expected ID kept |
| Application restart, stale lease recovery, monitoring restart | workflow paused restart with stale manual/monitor leases; `application_restart` with 5 stranded leases | PASS |
| Stop & Reset | workflow reset, then backend change only via a new run | PASS |
| Clean shutdown | workflow final shutdown (0.228 s); `shutdown_matrix` 5 cases | PASS |

**Invariants verified:**
- expected Queue IDs unchanged;
- zero silent replacements (the explicit create-first Replace is the only intentional
  replacement);
- zero leaked contexts;
- zero leaked managed processes;
- zero scheduler-owned leases after shutdown;
- every browser operation bounded;
- no application shutdown hang.

## Identity continuity

Across Phase 7 there were **zero** unexpected Queue ID changes and **zero** automatic
replacement identities. Mismatches were always rejected with the expected ID kept.
Provenance is enforced both ways before any context or state access. The
Patchright ↔ Chrome pairing was exercised in P5/P6; Camoufox provenance is preserved by
unchanged code and tests.

## Restoration

- **Transfer-first HYBRID:** falls back to same-backend storage state after a transfer
  503 or after a crash.
- **State faults:** missing, corrupt, and unavailable state fail without inventing an
  identity.
- **Cross-backend state:** never used.
- **State refresh:** refreshed state is saved after a verified restore.

## Concurrency

Patchright uses the ordinary Chrome model: multiple isolated temporary contexts share a
bounded process. There is no Camoufox-style serialization.

Healthy levels were 1–25 contexts per process and 50 across the default 2 processes.
The shipped ceilings (`CHROME_PROCESS_COUNT ≤ 4`, `MAX_CONTEXTS_PER_BROWSER ≤ 25`,
`MAX_ACTIVE_CONTEXTS ≤ 100`) are unchanged. No Patchright-specific limit was needed.

## Monitoring

With 100 sessions offered 20 checks/s, Patchright ran about 12 checks/s and Chrome
about 13 (P5). Backlog, queue, and contexts stayed bounded, with 0 failures. Throughput
is limited by per-process page throughput, which is the same for both backends; this
informs worker sizing.

## Manual browser

Open/Close ownership, window loss, headed-process kill, shutdown with a window open,
and concurrent Open/Close churn all passed. The manual pool is one bounded headed
process with `MAX_MANUAL_OPEN_SESSIONS` contexts, the same as Chrome.

## Recovery

- **Detection and replacement:** a disconnect is detected in 0.05 s and the slot is
  replaced about 0.2 s after the kill.
- **Scope:** only the failed slot is replaced.
- **Repeated kills:** restart p95 was 0.185 s with no degradation over 20 kills (P5).
- **Crash during monitoring:** completed with every session verified.
- **Health heuristic:** none. Patchright relies on disconnect and abandoned-operation
  replacement only, with no consecutive-timeout restart.

## Shutdown

Idle shutdown took about 0.06–0.23 s. Under load the stage-composed bound applies:

| Condition | Duration |
|---|---|
| Slow pages or a browser restart in flight | about 10 s |
| Operator work plus a headed window | about 7 s |
| 90 s stuck pages | 30.1 s |

All cases were within the 45 s check bound. Total shutdown is roughly the number of
stages × `SHUTDOWN_TIMEOUT_SECONDS`, not one timeout.

## Resources (tested local profile)

At the 50-context default ceiling in the final run, the Patchright tree RSS peaked at
14.6 GB and Chrome at 14.1 GB (summed RSS, which may double-count shared pages).
Browser CPU averaged 282% vs 265%. Application RSS stayed around 150–180 MB throughout
Phase 7.

In the P5 monitoring profiles, Patchright's tree used 2.1–3.0 GB on average. Levels
above the shipped ceilings were not tested.

## Known issues

1. **A crashed restore attempt waits out its deadline.** On both backends, an attempt in
   flight on a killed browser waits until its attempt deadline before the HYBRID
   fallback runs (about 24 s total locally). This is bounded and backend-independent. A
   possible improvement is to cancel in-flight attempts when the disconnect is
   detected.
2. **Total shutdown is not capped by one timeout.** It is bounded per stage by
   `SHUTDOWN_TIMEOUT_SECONDS`, as documented since Phase 6.
3. **Patchright has a modest local cost vs Chrome:** about 10–30% higher restore
   latency and about 8% lower throughput.
4. **The Chrome build is not pinned.** A new run records the observed version, but
   external Chrome updates can change the browser under future new runs.
5. **An intermittent unit-test timing failure.**
   `test_operator_fencing.py::test_chrome_loss_while_open_is_detected_without_relaunch`
   fails occasionally under the full suite. It uses fakes (no real browser) and is
   unrelated to the backend default. It did not reproduce in isolation, under a CPU
   stress test, or in integration-then-unit ordering. See Validation.

## Staging (NOT RUN / UNKNOWN)

No authorised Queue-it staging run was performed in Phase 7. Controlled simulator PASS
is mechanism evidence only. The following are **NOT RUN / UNKNOWN** for Patchright:

- real Queue-it acquisition and Queue ID issuance;
- transfer-URL and storage-state restoration against Queue-it;
- PRE_QUEUE/ACTIVE_QUEUE continuity and admission to the protected destination;
- Queue-it-side behaviour toward Patchright-driven Chrome, including any bot or WAF
  response;
- staging-safe concurrency, throughput, and cadence;
- cross-engine behaviour on Queue-it.

## Unresolved risks

- Every Phase 7 result is local simulator evidence on one macOS arm64 host. Other OSes
  and hosts are **UNKNOWN**.
- Patchright is a third-party patched Playwright distribution. Upgrading it or Chrome
  requires re-running the preflight, identity, workflow, and acceptance harnesses.
- Nothing establishes anti-detection behaviour, and none is claimed.
- Camoufox is uncertified. Existing Camoufox runs remain openable, but Phase 7 did not
  re-validate them.

## Final validation commands and results

| Command | Result |
|---|---|
| `queue-load-test-phase7-acceptance --output docs/results/phase7_acceptance_result.json` | Patchright **PASS**, Chrome fallback **PASS**; 352/352 checks; 0 leftover processes |
| `queue-load-test-patchright-preflight --context-cycles 5` | **PASS**: 1.63.0, Chrome 153.0.8010.54, 5/5, 0 contexts and 0 processes after |
| Chrome fallback preflight (inside the acceptance run) | **PASS** |
| `python -m pytest -q` | **542 passed, 4 staging deselected** (final run). One earlier full run on the same code had 1 failure: the intermittent operator-fencing timing test (Known issue 5) |
| `python -m ruff check src tests` | **PASS** |
| `python -m mypy src` (strict) | **PASS**, 77 source files |
| `python -m pip check` | **PASS**, no broken requirements |

Prompt 5 benchmark evidence (`docs/phase7_patchright_benchmark.md`): 30/30 scenarios and
494/494 checks.

## Next

Phase 7 is complete. The remaining open item is an **authorised Queue-it staging
validation** of the Patchright default, which is required before any staging PASS can
be claimed.
