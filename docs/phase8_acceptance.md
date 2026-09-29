# Phase 8 Acceptance and Operational Decision

**Date:** 2026-09-29

**Phase 8 result:** **PARTIAL.** The interchangeable monitoring-strategy architecture
is accepted. Direct Monitoring Strategy remains **experimental**.

**Authorised Queue-it staging:** **NOT RUN** for every Phase 8 prompt.

Machine-readable matrix: `docs/results/phase8_acceptance_result.json`.

## Decision

| Item | Decision |
|---|---|
| Run-level strategy architecture | **ACCEPTED**: persisted, immutable, routed at runtime assembly, with the same scheduler, leases, pause, Manual Open, and recovery for both strategies |
| Headed Window Strategy | **ACCEPTED**, unchanged from Phase 7, and the **default for new runs** |
| Direct Monitoring Strategy | **EXPERIMENTAL / selectable.** Safe to select because it cannot make a direct request to a real event without authorised evidence, and every uncertainty falls back to the Headed browser path. **Not production-ready**: no Queue-it evidence exists |
| Disabled strategies | none; no strategy failed an identity, security, or fallback gate |
| Existing runs | never migrated: each restarts with its persisted strategy and browser backend |
| Browser backend policy | unchanged: Patchright default, Chrome supported fallback, Camoufox dormant (Phase 7 acceptance not modified) |

Local simulator success is **not** treated as Queue-it success. The results below
keep FAIL separate from UNKNOWN. There is no FAIL. Every unresolved item is UNKNOWN
because authorised Queue-it evidence is absent.

## Evidence classes

1. **Local deterministic.** Unit and integration tests with fakes, `httpx`
   MockTransport, SQLite, and filesystem stores. This covers classification,
   fencing, persistence, secrecy, report and gate maths, and staging gates.
2. **Local simulator/browser.** Real Chrome and Patchright driving the operator app
   against `LocalQueueSimulator` on 127.0.0.1. Its page-polled JSON status endpoint
   and schema are the simulator's own. This is mechanism evidence only.
3. **Authorised Queue-it staging: NONE.** No authorised target, genuine Prompt 2
   discovery artifact, successful Prompt 3 replay, Prompt 4 comparison, reviewed
   `authorized_queue_it_staging` schema, or Prompt 7 staging benchmark exists. Every
   Queue-it field, lifecycle, state, and cadence question is **UNKNOWN**.
4. **Historical Glastonbury observation.** A saved 2025 page contained JavaScript
   resembling a status path built from a prefix, `customerId`, `eventId`, and
   `queueId`. It is a clue only. It was never used to construct a URL, classifier rule,
   body, or replay contract (`docs/phase8_status_discovery.md`).
5. **Queue-it Swagger / customer API.** The operator's local `que-it-swagger.json`
   ("Queue-it v1") describes an API-key-authenticated **customer management API**
   (`/api/queue/queueitem/...`, `/api/queue/customdata/...`, including a queue-item
   `cancel` operation). It is **not** the visitor-status mechanism. It was not used for
   monitoring, not integrated, and has no credentials in this workspace. Its cancel
   operation is never called, and Delete still never cancels a Queue-it visitor.

## Acceptance matrix

Column key: *Local* is evidence classes 1–2; *Queue-it* is class 3.

| # | Question | Result | Local | Queue-it | Basis |
|---|---|---|---|---|---|
| 1 | Setup selects Headed Window | **PASS** | PASS | n/a | `test_setup_persists_each_monitoring_strategy_independently_of_browser_backend`; Headed workflows |
| 2 | Setup selects Direct | **PASS** | PASS | n/a | same test; Direct workflow `direct_run_persists_strategy` |
| 3 | Strategy immutable per run | **PASS** | PASS | n/a | `run_config` has no strategy update path; restart and env-default tests |
| 4 | Legacy runs keep Headed behavior | **PASS** | PASS | n/a | `test_monitoring_strategy_round_trips_and_legacy_run_migrates_to_headed` |
| 5 | Restart preserves strategy | **PASS** | PASS | n/a | `test_restart_uses_persisted_strategy_when_environment_default_changes`; Direct workflow restart with Headed env default |
| 6 | Stop & Reset permits a new strategy | **PASS** | PASS | n/a | `test_stop_and_reset_allows_a_different_monitoring_strategy` |
| 7 | Headed unchanged from Phase 7 | **PASS (local)** | PASS | UNKNOWN | selector routes Headed to the same `QueueSessionMonitor`; behavior-preserving refactor; Phase 5/7 workflows pass (below) |
| 8 | Direct creation uses the normal browser path | **PASS** | PASS | UNKNOWN | `creation_is_browser_based_and_unchanged`; direct is never an acquisition path |
| 9 | Queue ID authoritative under both | **PASS (local)** | PASS | UNKNOWN | parser/client identity checks, fenced records, 0 Queue ID changes in every workflow and benchmark |
| 10 | Direct request discovered, not constructed | **PASS (mechanism)** | PASS | UNKNOWN | harvester adopts only same-session, post-fallback, accepted-scope browser exchanges whose captured response passes the reviewed schema; no URL construction exists. Whether Queue-it's page exposes such a request is UNKNOWN |
| 11 | Event/session/transient values understood | **UNKNOWN** | — | UNKNOWN | Prompt 3 stability classes all UNKNOWN without genuine captures |
| 12 | Persisted state sufficient across restart | **UNKNOWN** | PASS | UNKNOWN | local restart resumes direct with 0 rediscovery; Prompt 3 storage outcomes A–D UNKNOWN |
| 13 | Direct agrees with browser/DOM | **UNKNOWN** | PASS | UNKNOWN | simulator: 0 disagreements, stages matched; no Queue-it comparison |
| 14 | PRE_QUEUE evidence | **UNKNOWN** | PASS | UNKNOWN | benchmark checkpoint, both strategies |
| 15 | ACTIVE_QUEUE evidence | **UNKNOWN** | PASS | UNKNOWN | benchmark, both strategies |
| 16 | Paused-queue evidence | **UNKNOWN** | UNKNOWN | UNKNOWN | simulator has no paused stage |
| 17 | SERVICED_SOON evidence | **UNKNOWN** | PASS | UNKNOWN | benchmark, both strategies |
| 18 | TURN_STARTED evidence | **UNKNOWN** | UNKNOWN | UNKNOWN | not simulated |
| 19 | Admission/redirect safe | **PASS (by design)** | PASS | UNKNOWN | direct never persists ADMITTED/EXPIRED; redirect → `unsupported_admission` → browser verification via `AdmissionDetector` |
| 20 | Unexpected direct behavior falls back | **PASS** | PASS | UNKNOWN | 20+ classified reasons; unit matrix and 12 live fault classes in the Direct workflow |
| 21 | Identity mismatch preserves Queue ID | **PASS** | PASS | UNKNOWN | mismatch/ambiguity are hard failures; the expected ID is never replaced |
| 22 | Schema changes fall back | **PASS** | PASS | UNKNOWN | wrong type, non-object, missing ID, malformed, content type |
| 23 | Expired/corrupt state falls back | **PASS** | PASS | UNKNOWN | expired cookies, rejected state, corrupt browser state, corrupt record (report-only), corrupt cookies |
| 24 | Recipe refresh only from legitimate observation | **PASS** | PASS | UNKNOWN | stale, other-identity, schema-failing, wrong-scope, and remote-URL evidence refused; cooldown bounded |
| 25 | Pause stops direct and browser automatic checks | **PASS** | PASS | UNKNOWN | scheduler gate; 0 requests while paused in the workflow and benchmark |
| 26 | Resume continues safely | **PASS** | PASS | UNKNOWN | resume to first check 0.10–0.32 s from persisted due state |
| 27 | Manual Open browser-based and fenced | **PASS** | PASS | UNKNOWN | open session not polled directly; others continue |
| 28 | Add/Replace/Delete correct | **PASS** | PASS | UNKNOWN | Headed and Direct workflows; direct records cleaned up |
| 29 | Restart/recovery bounded | **PASS (local)** | PASS | UNKNOWN | bounded shutdown; browser SIGKILL recovery about 11 s; restart first check < 1 s |
| 30 | Workers/queues/leases bounded | **PASS** | PASS | n/a | unchanged scheduler; no transaction held across I/O |
| 31 | Secrets absent from logs | **PASS** | PASS | UNKNOWN | seeded-secret tests; the `httpcore` header-trace leak was found and fixed in Prompt 6 |
| 32 | Secrets absent from dashboard/metrics | **PASS** | PASS | UNKNOWN | seeded-secret tests; closed-enum metric labels |
| 33 | Aggregate reports safe | **PASS** | PASS | n/a | results contain no IDs, URLs, or secrets (checked) |
| 34 | Direct reduces restores/contexts | **PASS (local)** | PASS | UNKNOWN | 132 restores avoided; contexts 0.83 → 0; browser CPU 84% → 0.24% |
| 35 | Direct improves useful throughput/cadence | **NOT DEMONSTRATED** | PARTIAL | UNKNOWN | checks/s equal (4.2 vs 4.4) because the shared schedule bounds it; only per-check headroom improved. Not a FAIL: cadence is intentionally not increased |
| 36 | Direct preserves safe Queue-it cadence | **UNKNOWN** | PASS | UNKNOWN | local gap ≥ policy floor and equal per-session traffic; the real Queue-it cadence was never observed |
| 37 | Chrome fallback works | **PASS (local)** | PASS | UNKNOWN | Headed Chrome workflow; Direct Chrome workflow and benchmark |
| 38 | Patchright works as default backend | **PASS (local)** | PASS | UNKNOWN | Phase 7 extended Headed workflow; Direct workflow and benchmark on Patchright |
| 39 | Existing runs never silently migrated | **PASS** | PASS | n/a | persisted strategy and backend provenance; no migration path |
| 40 | Reasons Direct should stay experimental | **YES** | — | — | see below |

## Why Direct stays experimental

1. No authorised Queue-it evidence exists for any direct question (items 10–18, 36).
2. Replay state sufficiency (cookies, tokens, rotation, lifetime) is UNKNOWN.
   Replay-refreshed cookies do not flow back into browser `storage_state`.
3. Paused, TURN_STARTED, and admission behavior were never observed, even locally.
4. The benefit shown is per-check headroom, not more useful monitoring at the same safe
   cadence. Browser RSS is unchanged while fallback and Manual Open browsers stay
   running.
5. Schema review is a documented procedure, not tooling.
6. Direct showed larger due-backlog bursts with zero jitter; this has not been
   re-measured with production jitter.

Direct remains safe to select: without an accepted authorised schema and
authorised discovery evidence, a Direct run against a real event makes **no** direct
request and monitors entirely through the Headed browser path, with classified
`schema_unavailable` or `discovery_required` fallbacks.

## Final validation (2026-09-29)

| Check | Command | Result |
|---|---|---|
| Headed Window application workflow, Patchright with Phase 7 restart/recovery | `queue-load-test-phase5-workflow --backend patchright --phase7-recovery` | **74 passed, 0 failed** |
| Headed Window application workflow, Chrome fallback | `queue-load-test-phase5-workflow --backend chrome` | **67 passed, 0 failed** |
| Direct application workflow, Patchright (includes restart, recovery, and seeded-secret checks) | `queue-load-test-phase8-direct-runtime --backend patchright` | **42 passed, 0 failed** |
| Security, redaction, recovery, and fencing suites | `pytest tests/unit/test_direct_security.py test_direct_monitor.py test_phase8_monitoring_benchmark.py test_observability.py test_operator_fencing.py test_phase4_recovery.py test_phase3_recovery.py` | **144 passed** |
| Full non-staging suite (includes the Chrome/Camoufox/Patchright Headed workflows, the Chrome Direct workflow, and the quick benchmark) | `python -m pytest` | **719 passed, 4 staging deselected** (366.69 s) |
| Lint | `ruff check src tests` | **PASS** |
| Strict typing | `mypy src` | **PASS** (102 source files) |
| Authorised Queue-it staging | any gated staging harness | **NOT RUN**: not authorised or configured |

Local benchmark evidence is from Prompt 7 (`docs/results/phase8_monitoring_benchmark_result.json`).

## Prompt summary

| Prompt | Result | Evidence boundary |
|---|---|---|
| 1 Strategy boundary and setup UI | COMPLETE | local deterministic |
| 2 Browser-observed status discovery | COMPLETE (mechanism); Queue-it UNKNOWN | local fixture and browser |
| 3 Direct replay experiment | COMPLETE (mechanism); Queue-it UNKNOWN | local deterministic |
| 4 Observation equivalence | COMPLETE (mechanism); conclusion UNKNOWN | local deterministic |
| 5 Direct runtime integration | COMPLETE (local); Prompt 4 gate made optional by the operator | local deterministic and simulator |
| 6 Security, observability, hardening | COMPLETE (local) | local deterministic and simulator |
| 7 Direct vs Headed benchmark | COMPLETE (local); staging NOT RUN | local simulator |
| 8 Acceptance | **PARTIAL**: architecture accepted, Direct experimental, Headed default | all of the above; no Queue-it evidence |

## Next justified task

Authorised Queue-it staging validation of Direct Monitoring Strategy. On an authorised
event with deliberate confirmation, run:

- Prompt 2 discovery;
- Prompt 3 replay;
- Prompt 4 shadow comparison;
- schema review;
- `queue-load-test-phase8-monitoring-benchmark --mode staging`.

Then revisit this decision. This is not Phase 9, and nothing here begins it.
