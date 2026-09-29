# Phase 8 Prompt 7 — Direct vs Headed Monitoring Benchmark

**Date:** 2026-09-29

**Local controlled benchmark:** RUN (LocalQueueSimulator on 127.0.0.1, Patchright)

**Authorised Queue-it staging benchmark:** **NOT RUN.** No authorised staging
environment, authorised discovery evidence, or reviewed `authorized_queue_it_staging`
response schema is configured. Every Queue-it result is **UNKNOWN** and is not
estimated from the simulator.

**Default strategy:** unchanged. Headed Window Strategy stays the default for new runs.

Machine-readable results: `docs/results/phase8_monitoring_benchmark_result.json`
(aggregate only: no session IDs, Queue IDs, URLs, or secrets).

## Harness

```text
queue-load-test-phase8-monitoring-benchmark --mode local --backend patchright \
    [--sessions 12] [--window-seconds 30] [--output docs/results/...json]

RUN_STAGING_TESTS=1 RUN_PHASE8_MONITORING_BENCHMARK=1 \
queue-load-test-phase8-monitoring-benchmark --mode staging --confirm-authorized-staging
```

- **Sequential and equivalent.** Headed Window runs first, then Direct. Each gets a
  fresh population with the same count and stage layout, on the same host, backend,
  workers (2), queue (4), claim batch (4), polling policy, and window. The two
  strategies never observe the same visitors at the same time.
- **Lifecycle layout.** It uses only legal transitions. Visitors join in PRE_QUEUE.
  For the first half, a third stay PRE_QUEUE and the rest are ACTIVE_QUEUE, with a
  checkpoint at mid-window. Then PRE → ACTIVE and a third go ACTIVE → SERVICED_SOON,
  and the final state is checked.
- **Measured.** Attempts; successful observations; checks/s; p50/p95/max check
  durations (all, direct-only, browser-only); first full sweep; due backlog; oldest
  overdue; BrowserContext peak and mean; app and browser-process CPU and RSS (psutil,
  0.5 s sampling); direct success and fallback rates; restores avoided; fallback
  reasons (network, schema, identity); disagreements; state-refresh failures;
  visitor-status requests per session per minute; per-session direct request gaps;
  response poll hints.
- **Recovery segment** (after the window, per strategy):
  - pause for 3 s (no checks or requests allowed), then resume latency;
  - Manual Open of one session (fenced from direct polling, others continue);
  - SIGKILL of the browser processes during a check (for Direct, a forced fallback
    check);
  - direct-state expiry (the server rejects only replays, then recipe refresh and
    direct resumption, with a 2 s re-adoption cooldown);
  - application restart from persisted state.
- **Staging mode** needs all three gates plus: `STAGING_URL`; status discovery with
  `authorized_queue_it_staging` scope and confirmation; a reviewed schema of that
  scope (`staging_readiness`). It uses the **configured production cadence** (no
  benchmark speed-up), observes passively, injects no faults, and skips the destructive
  recovery segment. It acquires one population per strategy (2 × sessions visitors).
  It has not been run against Queue-it.

## Polling safety

Both strategies use the same adaptive `PollingPolicy`. Direct is never scheduled more
often because it is cheaper. The local profile uses a short, identical 2–3 s interval
for both strategies against the simulator only. Staging mode refuses to shorten the
configured cadence. The simulator's status responses carry `pollAfterSeconds=2.5`.
It is parsed, counted (`poll_hints_observed`), and compared against the actual gap. It
is never used to poll faster (`direct_faster_than_response_hint` is reported).

## Local results (Patchright, 12 sessions per strategy, 30 s window)

| Measure | Headed Window | Direct |
|---|---|---|
| Monitoring attempts / successful observations | 126 / 126 | 132 / 132 |
| Checks/s | 4.20 | 4.40 |
| Check duration p50 / p95 (s) | 0.242 / 0.299 | 0.0014 / 0.0023 |
| First full sweep (s) | 3.04 | 2.77 |
| Due backlog max / mean | 2 / 0.47 | 6 / 0.95 |
| Oldest overdue max (s) | 0.18 | 0.35 |
| BrowserContext peak / mean | 2 / 0.83 | 0 / 0.00 |
| Browser-process CPU mean / p95 (%) | 84.1 / 126.4 | 0.24 / 1.8 |
| Browser-process RSS peak (MiB) | 999.8 | 996.0 |
| Application CPU mean (%) / RSS peak (MiB) | 7.25 / 135.7 | 6.83 / 172.5 |
| Direct success rate / fallback rate | — | 1.00 / 0.00 |
| Browser restores avoided | — | 132 |
| Direct/network errors, schema failures, identity mismatches | — | 0 / 0 / 0 |
| Direct/browser disagreements | — | 0 |
| State-refresh failures | 0 | 0 |
| Visitor-status requests per session per minute | 21.0 (page polls) | 22.0 (direct) |
| Page navigations per session per minute | 21.0 | 0 |
| Per-session direct request gap min / p50 (s) | — | 2.63 / 2.65 |
| Direct warm-up (one browser discovery pass per session) | — | 7.0 s |

Interpretation, limited to this controlled environment:

- Direct replaces a browser restore (about 0.24 s here against a trivial local page;
  real Queue-it pages are heavier) with a millisecond-scale HTTP request. It holds no
  BrowserContext and uses almost no browser CPU during steady state.
- Throughput (checks/s) is roughly equal **by design**: the schedule, not capacity,
  bounds it, and both strategies share the same polling policy. The capacity benefit
  appears as headroom (duration, contexts, CPU), not as more polling.
- Browser RSS is unchanged, because the automatic and headed browser processes stay
  running for fallback and Manual Open. Application RSS was about 37 MiB higher under
  Direct (HTTP client and bookkeeping). This comes from one sample run.
- Due backlog bursts were larger under Direct (max 6 vs 2). With jitter set to 0,
  sessions checked in lockstep come due together, and direct checks finish fast enough
  to line up exactly. The oldest-overdue age stayed sub-second. Worth re-measuring
  with production jitter.
- Visitor-status traffic per session is essentially equal (22.0 vs 21.0 per minute),
  so Direct did not increase request frequency.

## Recovery results (local)

| Scenario | Headed Window | Direct |
|---|---|---|
| Pause: no checks or requests for 3 s | yes | yes |
| Resume to first check (s) | 0.32 | 0.10 |
| Manual Open: open session not directly polled / others checked | yes / 11 of 11 | yes / 11 of 11 |
| Browser SIGKILL during a (fallback) check: recovery (s), identity preserved | 10.8, yes | 11.8, yes |
| Direct-state expiry: detected / recipe refreshed (s) / direct resumed | — | yes / 5.3 / yes |
| Restart: first check (s) / first direct request (s) | 0.63 / — | 0.32 / 0.32 |
| Restart: strategy kept / identities preserved / page navigations needed | yes / yes / 4 | yes / yes / 0 |

## Lifecycle evidence

| Stage | Headed (local) | Direct (local) | Queue-it staging |
|---|---|---|---|
| PRE_QUEUE | PASS | PASS | UNKNOWN (NOT RUN) |
| ACTIVE_QUEUE | PASS | PASS | UNKNOWN (NOT RUN) |
| Paused | UNKNOWN (simulator has no paused stage) | UNKNOWN | UNKNOWN (NOT RUN) |
| SERVICED_SOON | PASS | PASS | UNKNOWN (NOT RUN) |
| TURN_STARTED | UNKNOWN | UNKNOWN | UNKNOWN (NOT RUN) |
| Admission/redirect | UNKNOWN (Direct by design always defers to the browser) | UNKNOWN | UNKNOWN (NOT RUN) |

## Benchmark gates (reported from measurements, not predetermined)

| Gate | Local result | Basis | Queue-it |
|---|---|---|---|
| Direct identity safety | **PASS** | 0 Queue IDs changed and 0 reacquired across window, recovery, and restart, both strategies | UNKNOWN |
| Observation equivalence | **PASS** (simulator stages only) | every observable stage matched in both strategies; 0 disagreements; paused, turn-started, and admission UNKNOWN | UNKNOWN |
| Fallback reliability | **PASS** | expiry and browser-failure fallbacks recovered (0 window fallbacks in steady state) | UNKNOWN |
| Direct request stability | **PASS** | 132/132 direct successes, 0 errors | UNKNOWN |
| Restart continuity | **PASS** | both resume; Direct resumes direct in 0.32 s with 0 rediscovery navigations | UNKNOWN |
| Throughput improvement | **IMPROVED** (per-check cost) | p95 0.299 s → 0.0023 s; checks/s bounded by the shared schedule | UNKNOWN |
| Browser-context reduction | **IMPROVED** | mean 0.83 → 0.00 | UNKNOWN |
| CPU/RAM effect | **REDUCED** browser CPU | browser CPU 84% → 0.24%; browser RSS unchanged; app RSS +37 MiB | UNKNOWN |
| Request-cadence safety | **PASS** | min per-session gap 2.63 s ≥ 2.0 s policy floor; not faster than the 2.5 s hint; per-session status traffic about equal | UNKNOWN |
| Sensitive-data safety | **PASS** | seeded fake secret absent from logs, metrics, dashboard, sessions, SQLite, and the report | UNKNOWN |

These are local mechanism results. They show the integration behaves as designed
against a controlled page. They say nothing about Queue-it response semantics,
cookie/state lifetime, cadence guidance, or server behavior under direct replay.

## Tests

- `tests/unit/test_phase8_monitoring_benchmark.py` (23, deterministic):
  - percentile and summary maths;
  - healthy report gates;
  - each gate's regression result;
  - missing data → UNKNOWN (never PASS);
  - poll-hint reporting;
  - staging NOT RUN/UNKNOWN;
  - JSON serialization without identities;
  - direct delta and gap maths;
  - every staging readiness gate, and the CLI's staging refusal.
- `tests/integration/test_phase8_benchmark_workflow.py`: the quick profile (Chrome,
  6 sessions, 14 s window) asserts that the safety gates PASS and that performance
  gates are reported.
- Found and fixed during this prompt: direct observations did not update
  `last_checked_at` (a Prompt 5 gap), so dashboards and the recovery timings could not
  see direct checks. `apply_direct_observation` now stamps it (unit-tested).

## Next task

**Phase 8 Prompt 8 — Phase 8 Acceptance and Operational Decision.**
