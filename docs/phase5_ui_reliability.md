# Phase 5 — UI Reliability and Recovery

Date: 2026-09-27. Scope: the localhost operator UI (`queue-load-test-ui`), its operator
actions, headed-Chrome sessions, restart/resume, shutdown, and failure containment.
No authorised Queue-it staging traffic was sent. Every measurement below is local; none
of it is Queue-it throughput or real browser-backed cadence evidence.

No new infrastructure was introduced. The system remains one process: FastAPI/uvicorn,
SQLite, the local state directory, installed Google Chrome, and asyncio worker pools.

## Defects Found and Fixed

| # | Defect | Effect before the fix | Fix |
|---|--------|-----------------------|-----|
| 1 | `ApplicationRuntime` installed its own SIGINT/SIGTERM handlers with `loop.add_signal_handler`, replacing uvicorn's. | Ctrl+C/`kill` stopped the scheduler, creation, and Chrome, but the web server kept serving; a second signal did the same. Reproduced against the previous commit: HTTP 200 four seconds after SIGINT. | `install_signal_handlers=False` for the UI runtime. Uvicorn owns signals and runs the lifespan shutdown, which calls the ordered runtime close. |
| 2 | Shutdown cancelled operator workers first, then closed headed Chrome, then stopped producers; the automatic runtime could shut Chrome down while operator work still used it. | An in-flight Refresh/Replace was killed mid-browser-call; a cancelled Replace could leave both old and new rows with no population accounting. | Ordered shutdown (below) with a bounded drain for running operator work and `before_browser_shutdown` hooks so Chrome stays usable until operator and headed work have finished or been cancelled. |
| 3 | Startup cleared only *expired* headed ownership; stale automatic/operator leases were never cleared at startup. | After a crash, a row stayed `OPEN_IN_CHROME` for up to 30 s. A leased row that the scheduler could not reclaim (monitoring paused, or a non-monitorable status) stayed un-openable, un-refreshable, and un-deletable forever. | Single-instance OS lock (`<database>.lock`, released by the OS on any exit). Holding it, startup clears every persisted manual owner and lease in one transaction — all belong to dead processes. Queue IDs, status, progress, and schedules are untouched. A second UI process on the same database is refused. |
| 4 | Operator and headed acquisition rejected any row with `worker_id` set, even with an expired lease. | An abandoned lease (release failed during a database outage) blocked operator actions until restart when the scheduler would not reclaim it. | Expired leases are taken over with the same owner fencing the scheduler uses; live leases are still rejected. Late writes from the stopped owner fail on `worker_id`. |
| 5 | Dashboard browser state treated Queue-it lifecycle `CHECKING` (insufficient page evidence) and expired leases as browser ownership. | Rows showed `CHECKING` with every action disabled although nothing owned them — a lifecycle/ownership conflation the invariants forbid. | `CHECKING` now means a *live* lease only; filters match the same rule. |
| 6 | The 2-second poll replaced `#session-results` (and `#summary`) while a POST was in flight. HTMX then swapped the POST response into a detached element. | Action feedback was lost; a click during a poll could be dropped; buttons stayed clickable, so double-clicks sent duplicate POSTs. | `hx-sync="closest …:replace"` (a click aborts an in-flight poll; polls queue behind mutations), `hx-disabled-elt="this"`, and a feedback region outside the polled container filled by an out-of-band swap. |
| 7 | Duplicate **+ New Session** submissions each created a visitor; a duplicate Replace after completion reported a confusing failure. | Double-submit could add two identities. | Every mutating button carries a per-render token; the manager returns the original action for a repeated token (bounded LRU of 256). Per-session pending actions are also deduplicated, and a *different* pending action on the same session is rejected with a clear message. |
| 8 | Add committed the new row and the population adjustment in separate commits (Prompt 4 known issue). A creator exception skipped cleanup entirely. | A crash between commits, or a raised creator error, left population accounting off by one, or an orphan `CREATING` row. | Add/Replace persist a +1 reservation *before* browser work; Replace deletes the old row with the matching −1 in the same transaction. Failure reverts the reservation and discards only non-valid rows; a valid identity committed before a failure/cancellation is kept and counted. |
| 9 | No exception containment in the web layer: a `sqlite3` error, template error, or invalid filter value produced a raw 500 that HTMX silently ignored. | The operator saw nothing; invalid `status`/`runtime_state` query values crashed the partial. | Middleware turns any route/template failure into a sanitized static fragment (503, `HX-Retarget` to action feedback for POSTs or a refresh-status line for polls). Invalid filters are ignored with a notice. Workers run as separate tasks and are unaffected. |
| 10 | The headed-session heartbeat called `capacity()` with repair enabled, and a single renewal error ended the watcher. | A crashed headed Chrome could be relaunched as an empty visible window; one transient database error closed the operator's window, while a release error escaped as an unobserved task exception. | Liveness uses `capacity(repair=False)` plus the owning slot's connection state; Chrome restarts lazily on the next Open. Renewal errors are tolerated until the lease could lapse, then the window is closed first. Cleanup closes the context before releasing ownership and contains release failures. |
| 11 | The dashboard page query sorted the whole population for every page (`USE TEMP B-TREE FOR ORDER BY`). | Every 2-second poll sorted 10,000 rows. | Index `idx_queue_sessions_dashboard_order (created_at, session_id)`; the plan is now a covering-index walk. |
| 12 | HTMX was loaded from the public unpkg CDN. | The UI did not work offline and depended on a third party. | HTMX 2.0.4 (0BSD) is vendored at `web/static/htmx.min.js`. |

## Ownership and Fencing Model

Per-session exclusion is persisted in the session row, never a global lock:

| Owner | Columns | Renewal | Excludes |
|-------|---------|---------|----------|
| Automatic check | `worker_id=monitor-…`, `lease_until` | `MONITOR_LEASE_SECONDS` (claim) | headed open and operator actions while live |
| Operator action (Refresh/Delete/Replace) | `worker_id=operator-request-…`, `lease_until` | heartbeat every `OPERATOR_LEASE_SECONDS/3` | scheduler claims, headed open, other operator actions |
| Headed Chrome | `manual_owner_id`, `manual_lease_until` | heartbeat every `MANUAL_OPEN_LEASE_SECONDS/3` | scheduler claims and operator actions |

Every acquisition is one `BEGIN IMMEDIATE` transaction that checks and sets ownership
atomically. An operator lease is taken *before* the work is queued. All subsequent writes
from a holder are fenced on its owner id, so a stopped owner cannot overwrite a session
after takeover. Expired ownership may be taken over; live ownership never is.

| Pair on one session | Outcome |
|---------------------|---------|
| Open + automatic check | whichever commits first wins; the other is busy/skipped |
| Open + Refresh/Delete/Replace | whichever commits first wins; the other reports busy |
| Refresh + Refresh, Replace + Replace, Delete + Delete | same pending action returned (no second run) |
| Refresh + Delete, Refresh + Replace, Delete + Replace | second is rejected: “Refresh is already running for this session” |
| Open + Open | the in-flight open is shared; an open session reports “already open” |
| Actions on different sessions | concurrent up to `OPERATOR_WORKERS` (default 2) and `MAX_MANUAL_OPEN_SESSIONS` |

## Restart and Recovery

- A valid active `RunConfig` skips setup; `/` and `/setup` redirect to the dashboard.
- Startup acquires `<database>.lock`, initializes SQLite, clears every persisted owner,
  loads the run, and starts the runtime. The acquisition target is
  `requested_sessions + operator_population_adjustment`; the existing bounded controller
  counts valid persisted Queue IDs and creates only the deficit. A complete target
  creates nothing.
- Monitoring RUNNING resumes due checks. PAUSED stays paused (no claims, no browser
  checks) while acquisition still fills any deficit.
- Stale `OPEN_IN_CHROME`: the old Chrome is never assumed to exist. Ownership is cleared,
  the Queue ID and last persisted observation are kept, and the session is immediately
  eligible for Open, Refresh, and automatic monitoring.
- Without the lock (programmatic embedding), only expired ownership is recovered, which
  is the safe choice when another live process might own a window.

## Chrome Crash Behavior

A crash during Open closes what was created, releases ownership, and shows a sanitized
error; the Queue ID is unchanged. A crash while open is detected by page/context close
events or the non-repairing heartbeat. There is no final inspection because the page is
gone, so the last persisted state is kept, ownership is released, and the context budget
returns. The next explicit Open relaunches the headed pool and restores the same identity.

## Shutdown

On SIGINT/SIGTERM, uvicorn stops accepting connections and finishes in-flight requests.
The lifespan shutdown then runs these steps in order:

1. Mutating routes return “Application is shutting down”; operator and headed managers
   reject new requests. Close remains available.
2. Creation and automatic claims stop.
3. In-flight automatic checks drain within `SHUTDOWN_TIMEOUT_SECONDS`; queued automatic
   leases are released.
4. Queued operator work is cancelled and its leases released. Running operator work gets
   `SHUTDOWN_TIMEOUT_SECONDS` to finish and persist, then is cancelled. Its cleanup
   releases the lease, reverts unfulfilled reservations, and closes contexts.
5. Headed sessions still inspectable get one final evaluator pass, which persists
   progress/state. Then contexts are closed, ownership is released, and headed Chrome
   stops.
6. Automatic Chrome, then the repository, shut down; the instance lock is released.

Shutdown never deletes a session and never replaces an identity. In the end-to-end
check, SIGINT exited in about 0.5 s, left no Chrome processes, and left zero persisted
owners.

## Fault Tests

`tests/unit/test_ui_reliability.py` runs the real `ApplicationRunRuntime` and web app, with
only Chrome and Queue-it faked. `tests/unit/test_operator_fencing.py` covers fencing,
duplicates, and failures. `tests/integration/test_ui_chrome_recovery.py` uses real
installed Chrome against `LocalQueueSimulator`.

| Scenario | Test |
|----------|------|
| Restart, active RunConfig / partial / complete target | `test_restart_with_partial_target_resumes_only_deficit`, `test_restart_with_completed_target_creates_nothing` |
| Restart while RUNNING / PAUSED | `test_restart_while_running_resumes_due_checks`, `test_restart_while_paused_keeps_pause_and_still_acquires_deficit` |
| Stale `OPEN_IN_CHROME` and leases | `test_restart_clears_stale_manual_and_lease_ownership_under_instance_lock`, `test_exclusive_startup_recovery_clears_every_owner_but_no_identity`, `test_second_ui_instance_is_refused_until_first_exits` |
| Chrome crash during open / while open | `test_chrome_crash_during_manual_open_releases_ownership`, `test_chrome_loss_while_open_is_detected_without_relaunch`, `test_chrome_kill_while_open_releases_ownership_and_reopens` (real Chrome, SIGKILL) |
| Shutdown with headed browser open / during operator work | `test_shutdown_with_manual_browser_open_releases_everything`, `test_shutdown_during_operator_work_releases_ownership`, `test_close_*`, `test_mutations_are_refused_once_shutdown_begins` |
| Database error during action / poll | `test_database_error_during_ui_action_is_contained`, `test_poll_failure_is_retargeted_and_later_poll_recovers` |
| State load / save failure | `test_state_failures_during_refresh_preserve_identity`, `test_refresh_with_failing_state_save_keeps_identity_and_no_context_leak` (real Chrome) |
| Duplicate submission | `test_duplicate_add_submission_with_same_token_creates_one`, `test_duplicate_replace_and_delete_and_refresh_submissions` |
| Conflicting / separate-session actions | `test_conflicting_actions_on_one_session_are_fenced`, `test_actions_on_separate_sessions_run_concurrently` |
| Failed replacement / add | `test_failed_replacement_that_raises_keeps_old_identity`, `test_add_failure_*`, `test_failed_add_leaves_no_orphan_or_population_drift` |
| Route/template exception | `test_template_exception_is_contained_and_state_unchanged`, `test_invalid_filters_are_ignored_not_crashed` |
| Ownership/context leaks, Queue IDs unchanged | asserted at the end of each scenario above |

An end-to-end smoke test also ran the real `queue-load-test-ui` process with installed
Chrome against the local simulator, driven by Playwright:

- setup acquired 3 identities;
- a double-click on **+ New Session** sent one POST and added exactly one;
- action feedback survived several polls;
- Refresh succeeded;
- a second UI process on the same database was refused;
- SIGINT shut everything down cleanly.

## 10,000-Row UI Sanity Benchmark

Command: `queue-load-test-phase5-ui-benchmark --iterations 100`. Raw result:
`docs/results/phase5_ui_benchmark_result.json`. Host: Apple M5 Pro, macOS (Darwin 25.5),
SQLite 3.50.4, local SSD, synthetic 10,000-row temporary database with 5 lifecycle
statuses, about 2/3 of rows with progress, 104 live leases, and 5 headed owners. Page
size is 50.

| Query (repository) | p50 ms | p95 ms | SQL statements |
|--------------------|-------:|-------:|---------------:|
| First page | 0.195 | 0.219 | 2 |
| Middle page (100) | 0.681 | 0.703 | 2 |
| Last page (200) | 1.173 | 1.219 | 2 |
| Search, exact Queue ID | 2.004 | 2.111 | 2 |
| Search, substring | 1.878 | 1.929 | 2 |
| Search, miss | 1.548 | 1.608 | 2 |
| Status filter | 0.605 | 0.629 | 2 |
| Browser-state filter (PARKED) | 0.665 | 0.691 | 2 |
| Summary aggregates | 6.467 | 6.647 | 4 |

| HTTP partial (routing + template) | p50 ms | p95 ms | SQL statements |
|-----------------------------------|-------:|-------:|---------------:|
| `/partials/sessions` first page | 0.989 | 1.072 | 3 |
| `/partials/sessions` search | 2.405 | 2.564 | 3 |
| `/partials/sessions` status filter | 1.409 | 1.483 | 3 |
| `/partials/sessions` last page | 1.964 | 2.075 | 3 |
| `/partials/summary` | 6.886 | 7.134 | 5 |

Before the dashboard-order index, the first page took 1.86 ms p95 and the last page
4.36 ms p95, because the page query sorted the whole population. With the index, the
plan is `SCAN s USING COVERING INDEX idx_queue_sessions_dashboard_order`.

Other results:

- **Polling.** One summary poll plus one sessions poll costs about 6.9 ms p95. That is
  a 0.69% duty cycle for one tab at 1 s and 0.34% at 2 s.
- **No writes from reads.** 100 polls wrote 0 rows. Pause and resume each wrote exactly
  one row (`runtime_control`).
- **No browser work.** Dashboard reads made 0 browser, context, or operator calls.
- **No task growth.** The asyncio task count was unchanged across polling.

Verified structurally, and asserted in `tests/unit/test_phase5_ui_benchmark.py`:

- no list-all followed by a Python slice;
- no N+1 progress loading (progress is joined inside the one page query);
- no browser or context creation;
- no O(10,000) writes per refresh or on pause/resume;
- no population-sized task creation.

Search uses `LIKE '%…%'` and the summary uses full aggregates. Both are O(rows) scans
that remain a few milliseconds at 10,000 rows. They would need revisiting only if a
measured population or host made the 2-second poll expensive.

## Remaining Known Issues

- Real Queue-it Open/Refresh/Add/Replace behavior, long operator dwell, and headed
  restoration are unverified without authorised staging traffic.
- The instance lock protects UI processes only. Harness CLIs pointed at the same
  database do not take it, so do not run them against a live UI database.
- Operator action history (requested/running/success/failed banners and request tokens)
  is process-local and bounded. Persisted effects survive restart; the banners do not.
- Automatic checks do not renew their lease. A check that ran longer than
  `MONITOR_LEASE_SECONDS` could be taken over by the scheduler, an operator action, or
  an Open. Writes are fenced, but two browsers could briefly drive one identity. Checks
  are deadline-bounded well under the 120 s default.
- If reverting an Add/Replace reservation fails during a database outage, the +1 stays
  persisted. The next restart then creates the one visitor the operator asked for;
  no existing identity is touched.
- The benchmark is a single local run on one host. It is not evidence for another
  host's disk or for concurrent heavy scheduler writes.
