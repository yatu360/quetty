# Phase 5 Acceptance Report

**Date:** 2026-09-27  
**Scope:** Phase 5 — local operator UI (Prompts 1–5)  
**Overall result:** **PARTIAL.** The required matrix is 108 PASS, 0 FAIL, 0 UNKNOWN on
local evidence. Real Queue-it staging validation is **NOT RUN / UNKNOWN**: the 8 items
in [Remaining UNKNOWNs](#remaining-unknowns) are listed separately and not counted.  
**Authorised Queue-it staging run:** **NOT RUN.** No staging URL, gates, or confirmation
flags were configured, and no Queue-it traffic was sent.

## Executive Summary

The complete operator workflow runs end to end on the real application with installed
Google Chrome:

1. first boot, setup, and a persisted run;
2. bounded acquisition, with the dashboard filling in as sessions arrive, and
   auto-refresh;
3. pause/resume;
4. Open in **headed** Chrome, with the scheduler skipping that session; then Close, and
   the session returns to monitoring;
5. Refresh Now, Add, Replace, and Delete;
6. graceful shutdown, then restart with recovery of run, sessions, pause state, and
   population accounting;
7. an interrupted initial acquisition that resumes only its deficit.

The reproducible harness is `queue-load-test-phase5-workflow --headed`. It recorded
**56/56 checks passed** in `docs/results/phase5_workflow_result.json`, and the same run
is a regression test in `tests/integration/test_phase5_workflow.py`.

Process-level checks ran the real `queue-load-test-ui` CLI:

- default bind to `127.0.0.1:8000`;
- a Playwright/Chrome browser drove the real HTMX dashboard: a double-click Add sent
  one POST and added exactly one visitor, feedback survived polling, and Refresh
  succeeded;
- SIGKILL of the UI with a headed session open, then restart cleared the stale
  ownership immediately;
- a second UI instance was refused;
- SIGINT/SIGTERM shut down cleanly with zero Chrome processes and zero persisted owners.

The target in all of this was `LocalQueueSimulator` on 127.0.0.1, a Queue-it-*like*
page server with synthetic identities. It is **not** Queue-it. These results prove the
application's mechanics: persistence, ownership, bounded browser work, identity
preservation, and recovery. They do not prove Queue-it behavior. Queue status in every
run came from the existing lifecycle evaluator; users-ahead behavior is not claimed.

## Scope

Phase 5 added a lightweight local operator UI over the Phase 4 single-machine runtime:

| Prompt | Delivered |
|---|---|
| 1 | Setup and persisted immutable `RunConfig`; bounded startup; paginated dashboard |
| 2 | Persistent pause/resume of automatic monitoring |
| 3 | Open an existing session in headed Chrome with persisted ownership |
| 4 | Refresh Now, Delete, Replace, Add; population adjustment |
| 5 | Reliability: fencing, restart recovery, shutdown order, failure containment |
| 6 | This acceptance report: evidence, small fixes, handoff |

Out of scope: retargeting a run (**Start New Run** is a disabled placeholder), remote or
multi-user access, and any Phase 6 architecture.

## Final Architecture

```text
Operator's browser (localhost only)
        │  GET polls every 2 s (read-only) · POST actions (fenced, tokened)
        ▼
FastAPI + Jinja2 + vendored HTMX  (web/app.py, templates/)
        │  failure-containment middleware · single-instance lock <db>.lock
        ▼
Operator service layer  (web/service.py ApplicationRunRuntime)
   ├─ OperatorActionManager   fixed OPERATOR_WORKERS, bounded queue
   └─ ManualChromeSessionManager   ≤ MAX_MANUAL_OPEN_SESSIONS headed contexts
        ▼
Existing domain services
   RunConfig · SessionRepository (SQLite) · FileSystemStateStore
   SessionCreationController → QueueSessionCreator
   ParkedSessionScheduler → QueueSessionMonitor → lifecycle evaluator
   QueueSessionRestorer (transfer-first / HYBRID storage_state)
        ▼
BrowserManager (headless, automatic)  +  BrowserManager (headed, operator)
   one shared BrowserContext budget (MAX_ACTIVE_CONTEXTS)
        ▼
installed Google Chrome (channel="chrome")
        ▼
authorised target  (staging: NOT RUN; acceptance used the local simulator)
```

Everything is one process with SQLite and local state files. There is no PostgreSQL,
Redis, broker, distributed worker, Node runtime, or React.

## Startup / Run Configuration

- **First boot:** with no active run, `/` redirects to `/setup`, which asks for **Target
  URL** and **Number of sessions** (W `first_boot_shows_setup`).
- **Target URL:** must be an absolute HTTP(S) URL without credentials. It is the
  authorised *protected* staging page; reaching it is recorded as `ADMITTED`. The setup
  page now says this explicitly (see [Defects Fixed](#defects-fixed-during-acceptance)).
- **Requested count:** a whole number from 1 to `MAX_MANUAL_REQUESTED_SESSIONS`
  (default 10,000). Invalid input returns 422 and persists nothing (W `invalid_*`,
  `zero_count_rejected`, `non_integer_count_rejected`, `over_limit_count_rejected`,
  `invalid_setup_persists_nothing`).
- **Persistence:** one immutable `run_config` row. Setup delegates to the existing
  bounded creation controller; it adds 4 asyncio tasks, not one per requested session,
  and uses one headless Chrome process (W `setup_tasks_bounded`,
  `chrome_processes_bounded_during_acquisition`).
- **Restart:** a valid run skips setup (W `restart_skips_setup`). The acquisition target
  is `requested_sessions + operator_population_adjustment`, counted against valid
  persisted Queue IDs:
  - An interrupted initial acquisition (3 of 6 at shutdown) resumed to exactly 6, with
    the original 3 Queue IDs unchanged (W `partial_restart_resumes_only_deficit`).
  - A complete population creates nothing (W `restart_creates_no_refill_after_add_and_delete`).
- **No retargeting:** a setup POST with a different URL after a run exists redirects to
  the dashboard and leaves `run_config` unchanged (W
  `target_cannot_be_changed_for_existing_population`). Setup also refuses a database
  holding sessions but no run (U `test_new_run_is_refused_when_legacy_sessions_have_no_target`).

## Dashboard

- **Fields:** `session_id`, `queue_id`, persisted Queue-it lifecycle status, progress
  percentage (`—` when absent), last Queue-it update, last checked, next check, and the
  separate browser/runtime state (`PARKED`, `CHECKING`, `OPEN IN CHROME`), plus
  per-row actions.
- **Summary:** target URL, creation state, monitoring state, Requested Sessions, Valid
  Managed Sessions, Remaining To Initial Target, total persisted, due backlog, active
  and maximum BrowserContexts, and managed Chrome processes. Every value matched SQLite
  (W `summary_aggregates_match_database`).
- **Pagination:** 50 rows per page from one `LIMIT/OFFSET` query plus one `COUNT`. The
  repository rejects page sizes above 100.
- **Search and filters:** `session_id`/`queue_id` substring search, lifecycle filter,
  and browser-state filter (W `search_by_session_and_queue_id`, `lifecycle_filter`).
  Unknown filter values are ignored with a notice.
- **Auto-refresh:** HTMX `every 2s` GETs of the summary and the visible page.
  Monitored progress appeared on the dashboard 0.3 s after it was persisted (W
  `auto_refresh_shows_monitored_progress`). While acquisition ran, the dashboard showed
  0 → 2 → 4 rows as sessions arrived (W `dashboard_shows_sessions_as_they_appear`).
- **Deliberately omitted:**
  - users-ahead, queue number, and wait-time text, which are Queue-it theme-dependent
    and not required;
  - transfer URLs, `storage_state` paths/content, cookies, and any secret.

## Queue Status

`QueueStatus` continues to come **only** from the existing lifecycle path:
`QueueSessionMonitor.apply_restore_result` calls `evaluate_queue_status` (the only
caller, `scheduler/monitoring.py`), and the repository validates transitions. The web
layer never assigns `.status`, never calls the evaluator, and never derives
`ACTIVE_QUEUE` from a Queue ID or lifecycle from progress: a code search of `web/`
finds no `.status =`, no `evaluate_queue_status`, and no `users_ahead`. The dashboard
renders the persisted value. `OPEN_IN_CHROME`, `CHECKING` (runtime), and `PARKED`
(runtime) are `BrowserRuntimeState` values derived from live ownership columns, never
written into `QueueStatus`.

## Monitoring Pause / Resume

**Pause Monitoring** writes one `runtime_control` row. It survived restart (W
`pause_persisted`, `restart_preserves_paused_state`).

While paused:

- no new automatic check ran for 4.5 s, more than two polling intervals (W
  `pause_stops_new_claims`);
- the due backlog stayed visible at 4 (W `due_backlog_visible_while_paused`);
- Queue IDs and statuses were unchanged (W `pause_keeps_queue_ids_and_status`);
- a check already in flight finishes, and claimed-but-unstarted work is released (U
  `test_pause_finishes_in_flight_check_and_releases_queued_claims`).

**Resume Monitoring** restarted checks within 0.3 s (W `resume_restarts_monitoring`).
Pause and resume each write exactly one row at 10,000 sessions (B).

## Open in Chrome

"Open in Chrome" is **browser ownership only**. It is not a Queue-it lifecycle state.
The persisted status stayed `ACTIVE_QUEUE` while the row showed `OPEN IN CHROME` (W
`open_in_chrome_is_not_queue_status`).

- **Restore:** the existing identity-safe restorer (transfer first, then HYBRID
  `storage_state`) restores the expected Queue ID into a visible installed-Chrome
  window. The evidence run was headed; one headless automatic process and one headed
  process were running (W `open_in_chrome_restores_existing_identity`).
- **Exclusion:** the scheduler skipped the open session while others kept being
  checked (W `scheduler_skips_open_session`). Refresh and Delete were refused with
  "Close the Chrome session…" (W `refresh_and_delete_refused_while_open`).
- **Bounds:** at most `MAX_MANUAL_OPEN_SESSIONS` (default 5) headed contexts, sharing
  the global context budget. The one above the limit is rejected (U
  `test_two_manual_sessions_open_and_one_above_limit_is_rejected`).
- **Close:** the still-open page gets one evaluator pass that persists progress and
  reschedules, ownership is released (W
  `close_releases_ownership_and_persists_latest_state`), and automatic monitoring
  resumed 1.9 s later (W `closed_session_returns_to_monitoring`).
- **Identity mismatch:** a mismatching page showed "Identity Mismatch". The expected
  Queue ID was kept, ownership was released, and no new identity was created (W
  `identity_mismatch_preserves_expected_queue_id`; U
  `test_identity_mismatch_preserves_expected_id_and_cleans_up`).
- **Crash:**
  - a real headless Chrome SIGKILLed while open released ownership and reopened (I
    `test_chrome_kill_while_open_releases_ownership_and_reopens`);
  - a crash during open is covered (U
    `test_chrome_crash_during_manual_open_releases_ownership`);
  - a crashed pool is never relaunched by the heartbeat (U
    `test_chrome_loss_while_open_is_detected_without_relaunch`);
  - after the UI process was SIGKILLed with a headed window open, Chrome exited with
    its pipe, and the stale owner (lease still about 4 min 47 s in the future) was
    cleared at the next start (P).

## Refresh Now

Refresh Now calls the existing `QueueSessionMonitor.check` under a fenced operator
lease: restore, evaluator, then persist progress, schedule, and HYBRID state.

- It works while automatic monitoring is paused: new progress was persisted in 0.2 s
  (W `refresh_now_works_while_paused`).
- It keeps the Queue ID and releases the lease (W `refresh_keeps_queue_id`).
- It is refused while the session is open in Chrome or being checked (W; U
  `test_refresh_refuses_automatic_and_manual_ownership`).
- A state-store load or save failure fails the action without changing the identity or
  marking the session FAILED. A verified observation is still persisted when only the
  state save failed (U `test_state_failures_during_refresh_preserve_identity`; I
  `test_refresh_with_failing_state_save_keeps_identity_and_no_context_leak`).

## Delete

Delete means "stop managing this visitor". It does **not** call any Queue-it
cancellation API.

- It removes the row, cascades progress and leases, and deletes the local state file
  (W `delete_removes_session_progress_and_state`).
- It records −1 in the population adjustment atomically, so restart does not refill.
- It leaves other sessions untouched (W `delete_leaves_unrelated_sessions`).
- Repeating a Delete, or deleting a session that never had a state file, succeeds (W
  `repeat_delete_is_idempotent`; U `test_delete_without_any_state_file_succeeds`).
- It requires Close first and cannot overlap an automatic check (W; U
  `test_conflicting_actions_on_one_session_are_fenced`).

## Replace

Replace is create-first:

1. The existing bounded creator acquires a new visitor, reserving +1 population.
2. Only after the new visitor is valid and persisted is the old row deleted, applying
   the matching −1 in the same transaction.

Evidence:

- The new Queue ID was distinct from the old one and from every persisted one, and the
  population size was unchanged (W
  `replace_creates_new_independent_identity_then_removes_old`,
  `replace_preserves_population_size`, `replace_leaves_unrelated_sessions`).
- Failed, raising, or duplicate replacement is reported FAILED. The old identity is
  kept and population accounting returns to zero (U
  `test_replace_keeps_old_on_failure_and_swaps_after_success`,
  `test_failed_replacement_that_raises_keeps_old_identity`,
  `test_duplicate_replacement_is_not_success_and_keeps_old`).

## Add Session

**+ New Session** runs exactly one existing creation work item against the active
`RunConfig` target.

- A duplicated submission (same render token, sent concurrently) added exactly one
  visitor (W `add_creates_exactly_one_despite_duplicate_submit`; real-browser
  double-click sent one POST, P).
- It leaves `requested_sessions` unchanged and records +1 adjustment (W
  `add_keeps_requested_target`). The managed count may safely exceed the initial
  target (W `managed_count_may_exceed_requested`).
- A failed Add leaves no orphan row and no phantom count (U
  `test_add_failure_before_commit_reverts_reservation`,
  `test_failed_add_leaves_no_orphan_or_population_drift`). A valid identity committed
  before a late failure is kept and counted (U
  `test_add_failure_after_commit_keeps_valid_identity`).
- Multiple Adds use the fixed worker pool and bounded queue (U
  `test_concurrent_adds_use_fixed_workers_and_bounded_queue`).

## Concurrency / Ownership Rules

Each session row carries at most one live owner:

- an automatic check lease (`worker_id=monitor-…`);
- an operator lease (`worker_id=operator-request-…`, taken before queueing, renewed by
  heartbeat);
- a headed lease (`manual_owner_id`, renewed by heartbeat).

Acquisition is one `BEGIN IMMEDIATE` transaction. Live ownership is never overlapped.
Expired ownership may be taken over, and the stopped owner's later writes are rejected
by owner id. There is no global lock.

- Duplicate submissions return the original action. A different action already pending
  on the same session is rejected (U
  `test_duplicate_replace_and_delete_and_refresh_submissions`,
  `test_conflicting_actions_on_one_session_are_fenced`).
- Actions on different sessions ran concurrently up to `OPERATOR_WORKERS` (U
  `test_actions_on_separate_sessions_run_concurrently`).
- The full pair matrix is in `docs/phase5_ui_reliability.md`.

## Restart / Recovery

`queue-load-test-ui` holds an OS lock on `<database>.lock`, which the OS releases on
any exit. A second instance is refused (P; U
`test_second_ui_instance_is_refused_until_first_exits`).

Holding the lock, startup clears every persisted headed owner and lease in one
transaction without touching identities (U
`test_restart_clears_stale_manual_and_lease_ownership_under_instance_lock`; P
SIGKILL).

Restart preserved:

- sessions and Queue IDs (W `restart_preserves_sessions_and_queue_ids`);
- the `RunConfig` (W `restart_preserves_run_config`);
- the PAUSED state (W `restart_preserves_paused_state`);
- the operator population adjustment (W
  `restart_preserves_operator_population_adjustment`).

Monitoring resumed after Resume (W `restart_resume_monitoring_checks_again`).

## Shutdown

Uvicorn owns SIGINT/SIGTERM. The lifespan shutdown then:

1. refuses new mutations;
2. stops creation and automatic claims;
3. drains automatic checks;
4. finishes or cancels operator work within `SHUTDOWN_TIMEOUT_SECONDS`;
5. gives headed pages a final inspection, closes their contexts, and releases ownership;
6. closes Chrome, then SQLite, then releases the lock.

Nothing is deleted or replaced.

Evidence:

- Workflow shutdown ended with zero owners, zero Chrome processes, and identical
  identities (W `shutdown_zero_contexts_owners_and_chrome`,
  `shutdown_deleted_or_replaced_nothing`, `final_shutdown_clean`).
- The real CLI exited on SIGINT in about 0.5 s (Prompt 5) and on SIGTERM cleanly (P),
  with no Chrome processes left.
- Operator work cut off by the timeout releases its lease (U
  `test_shutdown_during_operator_work_releases_ownership`,
  `test_close_timeout_cancels_running_work_and_releases_lease`).

## Sensitive Data Handling

- **Not rendered or reported:**
  - Templates never receive transfer URLs, `storage_state` paths or content, or
    cookies (W `dashboard_hides_transfer_and_state`; U
    `test_dashboard_renders_safe_paginated_searchable_partial_population`).
  - Error responses are static sanitized fragments.
  - Result files omit identifiers.
- **Metrics and logs:** Prometheus labels are only `operation` and `bucket` (code
  search of `metrics/prometheus.py`), with no session or Queue ID labels. Logs keep an
  approved field list and redact URLs.
- **Local only:** `UI_HOST` defaults to `127.0.0.1` (P: `TCP 127.0.0.1:8000 (LISTEN)`).
- **Ignored files:** `*.sqlite3`, `*.sqlite3-wal`, `*.sqlite3.lock`, `.env`, and
  `.browser-state/` are git-ignored (checked with `git check-ignore`).

## 10,000-Row UI Query Benchmark

Source: `docs/results/phase5_ui_benchmark_result.json`, from
`queue-load-test-phase5-ui-benchmark --iterations 100`. Synthetic local SQLite on an
Apple M5 Pro with SQLite 3.50.4. This is not Queue-it throughput.

| Query | p95 ms | SQL statements |
|---|---:|---:|
| First page | 0.219 | 2 |
| Middle page | 0.703 | 2 |
| Last page | 1.219 | 2 |
| Search | 1.608–2.111 | 2 |
| Status filter | 0.629 | 2 |
| Browser-state filter | 0.691 | 2 |
| Summary aggregate | 6.647 | 4 |
| `/partials/sessions` (HTTP) | 1.07–2.56 | 3 |
| `/partials/summary` (HTTP) | 7.13 | 5 |

Other results:

- 100 polls wrote 0 rows; pause and resume wrote 1 row each.
- 0 browser calls and no asyncio task growth.
- Deep pages walk `idx_queue_sessions_dashboard_order` with no sort. One poll pair costs
  6.9 ms, a 0.34% duty cycle at 2 s.

## Tests and Static Checks

| Check | Result |
|---|---|
| `python -m pytest` (ordinary suite, unit and integration) | **448 passed**, 4 gated staging tests deselected |
| `python -m pytest tests/integration` | **13 passed** (installed Chrome, local simulator) |
| `python -m pytest -o addopts="" -m staging tests/staging` | **NOT RUN**: no authorised gates |
| `ruff check src tests` | **passed** |
| `mypy src` (darwin, and `--platform linux`) | **passed**, no issues in 65 source files |
| `mypy src --platform win32` | 2 **pre-existing** errors: `signal.SIGKILL` in `harness/phase4_recovery.py:978,1478` (POSIX-only kill in a Phase 4 harness). The Phase 5 `instance_lock.py` `msvcrt` branch type-checks. |
| Workflow harness, headed | **56/56 passed** |
| Workflow harness, headless (repeat) | **56/56 passed** |

## Acceptance Matrix

Evidence keys:

- **W**: workflow check in `docs/results/phase5_workflow_result.json` (real app,
  installed Chrome, local simulator).
- **U**: unit test.
- **I**: installed-Chrome integration test.
- **B**: 10,000-row benchmark.
- **P**: process-level run of the real `queue-load-test-ui` CLI in this acceptance.
- **C**: code inspection.

All local evidence; none of it is Queue-it staging.

| # | Requirement | Status | Evidence |
|---:|---|:---:|---|
| 1 | Local web UI starts | PASS | P: CLI served `/` → 303 `/setup`; W lifespan start |
| 2 | Binds to localhost by default | PASS | P: `127.0.0.1:8000 (LISTEN)` with defaults; `UI_HOST` default |
| 3 | First boot shows setup | PASS | W `first_boot_shows_setup` |
| 4 | Setup requests target URL | PASS | W (`name="target_url"`) |
| 5 | Setup requests session count | PASS | W (`name="requested_sessions"`) |
| 6 | Invalid target URL rejected | PASS | W `invalid_url_rejected`, `credential_url_rejected`; U `test_setup_validation` |
| 7 | Invalid session count rejected | PASS | W zero/non-integer/over-limit → 422 |
| 8 | RunConfig persists after restart | PASS | W `restart_preserves_run_config` |
| 9 | Active run skips setup | PASS | W `restart_skips_setup` |
| 10 | Partial acquisition resumes only deficit | PASS | W `partial_restart_resumes_only_deficit` (3 of 6 → 6); U `test_restart_with_partial_target_resumes_only_deficit` |
| 11 | Queue IDs preserved on restart | PASS | W `restart_preserves_sessions_and_queue_ids`; partial-resume originals kept |
| 12 | Target cannot be silently changed | PASS | W `target_cannot_be_changed_for_existing_population`; U legacy-session refusal |
| 13 | Setup is not per-session tasks/contexts/browsers | PASS | W task delta 4, 1 Chrome process for 4/6 sessions |
| 14 | Displays session_id | PASS | W `dashboard_shows_ids_status_progress` |
| 15 | Displays queue_id | PASS | W same |
| 16 | Displays persisted lifecycle status | PASS | W same (`ACTIVE_QUEUE`) |
| 17 | Displays progress when available | PASS | W `auto_refresh_shows_monitored_progress` (44.0%) |
| 18 | Missing progress handled | PASS | U `test_dashboard_renders_safe_paginated_searchable_partial_population` (`—`) |
| 19 | No users-ahead dependency | PASS | C: not selected, not rendered, not used by the web layer |
| 20 | Status from lifecycle evaluator | PASS | C: sole `evaluate_queue_status` caller is the monitor |
| 21 | ACTIVE_QUEUE not inferred from Queue ID | PASS | C: the web layer never assigns status |
| 22 | Lifecycle not inferred from progress alone | PASS | C: the UI renders persisted status only |
| 23 | Runtime ownership separate from QueueStatus | PASS | W `open_in_chrome_is_not_queue_status`; U `test_runtime_state_is_ownership_not_lifecycle` |
| 24 | Auto-refresh works | PASS | W 0.3 s progress refresh; P real browser HTMX polls |
| 25 | Polling does not repeat mutations | PASS | C: `hx-trigger` only on GET containers; P: double-click sent 1 POST; U tokens |
| 26 | Pagination bounded | PASS | B 50-row pages; repository caps at 100 |
| 27 | 10,000 rows not materialized per refresh | PASS | B: 2 statements/page, no list-all |
| 28 | session_id search | PASS | W `search_by_session_and_queue_id` |
| 29 | queue_id search | PASS | W same |
| 30 | Lifecycle filter | PASS | W `lifecycle_filter` |
| 31 | Aggregate summary correct | PASS | W `summary_aggregates_match_database` |
| 32 | Viewing creates no BrowserContexts | PASS | B 0 browser calls; U `test_polling_is_read_only_and_never_starts_browser_work` |
| 33 | Transfer URLs not rendered | PASS | W `dashboard_hides_transfer_and_state` |
| 34 | storage_state not rendered | PASS | W same; U sensitive-state assertions |
| 35 | Browser state/cookies not rendered | PASS | W same; C templates |
| 36 | No session/Queue ID Prometheus labels | PASS | C: labels are `operation`, `bucket` only |
| 37 | Local-only by default | PASS | P bind; README warning |
| 38 | Monitoring can be paused | PASS | W `pause_persisted` |
| 39 | Pause persists | PASS | W `restart_preserves_paused_state` |
| 40 | Pause stops new claims | PASS | W `pause_stops_new_claims` |
| 41 | Existing checks may finish | PASS | U `test_pause_finishes_in_flight_check_and_releases_queued_claims` |
| 42 | Resume restarts monitoring | PASS | W `resume_restarts_monitoring`, `restart_resume_monitoring_checks_again` |
| 43 | Due backlog visible while paused | PASS | W `due_backlog_visible_while_paused` (4) |
| 44 | Pause/resume keeps Queue ID | PASS | W `pause_keeps_queue_ids_and_status` |
| 45 | Pause/resume keeps QueueStatus | PASS | W same |
| 46 | Open in headed Chrome | PASS | W headed run `open_in_chrome_restores_existing_identity` |
| 47 | Existing identity restoration used | PASS | W same (Queue ID unchanged); C `QueueSessionRestorer.restore_open` |
| 48 | Restore failure creates no replacement | PASS | W mismatch: simulator new identities unchanged |
| 49 | Mismatch keeps expected Queue ID | PASS | W `identity_mismatch_preserves_expected_queue_id`; U |
| 50 | OPEN_IN_CHROME not QueueStatus | PASS | W persisted `ACTIVE_QUEUE` while open |
| 51 | Scheduler skips open session | PASS | W `scheduler_skips_open_session` |
| 52 | Manual sessions bounded | PASS | U capacity test; W `manual_contexts_bounded` |
| 53 | Close releases ownership | PASS | W `close_releases_ownership_and_persists_latest_state` |
| 54 | Closed session returns to monitoring | PASS | W `closed_session_returns_to_monitoring` (1.9 s) |
| 55 | Latest state persisted where available | PASS | W close-time evaluation rescheduled and persisted 58% |
| 56 | Chrome crash leaves no permanent ownership | PASS | I real SIGKILL; U crash/loss tests |
| 57 | Restart reconciles stale ownership | PASS | P SIGKILL + restart → 0 owners; U |
| 58 | Shutdown closes manual contexts | PASS | W `shutdown_zero_contexts_owners_and_chrome`; U |
| 59 | Refresh uses monitor/evaluator | PASS | C `QueueSessionMonitor.check`; W progress via evaluator |
| 60 | Refresh works while paused | PASS | W `refresh_now_works_while_paused` |
| 61 | Refresh does not race OPEN_IN_CHROME | PASS | W `refresh_and_delete_refused_while_open` |
| 62 | Refresh keeps Queue ID | PASS | W `refresh_keeps_queue_id` |
| 63 | Delete removes session | PASS | W `delete_removes_session_progress_and_state` |
| 64 | Progress removed | PASS | W same; U delete test |
| 65 | State file removed | PASS | W (file existed, then removed) |
| 66 | Missing state file safe | PASS | U `test_delete_without_any_state_file_succeeds`; W repeat delete |
| 67 | No invented Queue-it cancellation | PASS | C: no cancellation/leave call anywhere |
| 68 | Delete cannot race open/checking | PASS | W refused while open; U fencing |
| 69 | Delete leaves unrelated sessions | PASS | W `delete_leaves_unrelated_sessions` |
| 70 | Replace creates new valid session | PASS | W `replace_creates_new_independent_identity_then_removes_old` |
| 71 | New Queue ID differs from old | PASS | W same |
| 72 | Failed/duplicate replacement not success | PASS | U `test_duplicate_replacement_is_not_success_and_keeps_old`, failure tests |
| 73 | Replacement uses bounded creation | PASS | C existing creator in the fixed operator pool; U bounded queue |
| 74 | Failure follows documented semantics | PASS | U old kept, reservation reverted |
| 75 | Old not destroyed before success | PASS | U raising/duplicate/failed replacement keep old |
| 76 | Replace preserves population size | PASS | W `replace_preserves_population_size` |
| 77 | Add creates exactly one | PASS | W `add_creates_exactly_one_despite_duplicate_submit` |
| 78 | Add uses active RunConfig target | PASS | C creator built from the run target; W new simulator identity |
| 79 | Add keeps requested target | PASS | W `add_keeps_requested_target` |
| 80 | No phantom success after failure | PASS | U add failure tests |
| 81 | Multiple Adds bounded | PASS | U `test_concurrent_adds_use_fixed_workers_and_bounded_queue` |
| 82 | Count may exceed requested safely | PASS | W `managed_count_may_exceed_requested` |
| 83 | Duplicate submissions safe | PASS | W, P double-click; U duplicates |
| 84 | Conflicting actions serialized/rejected | PASS | U `test_conflicting_actions_on_one_session_are_fenced` |
| 85 | Different sessions concurrent | PASS | U `test_actions_on_separate_sessions_run_concurrently` |
| 86 | DB failure keeps Queue ID | PASS | U `test_database_error_during_ui_action_is_contained` |
| 87 | State failure keeps identity | PASS | U state failures; I failing state save |
| 88 | Route/template failure keeps scheduler state | PASS | U `test_template_exception_is_contained_and_state_unchanged` |
| 89 | Restart preserves sessions | PASS | W restart checks |
| 90 | Restart preserves RunConfig | PASS | W `restart_preserves_run_config` |
| 91 | Restart preserves paused/running | PASS | W `restart_preserves_paused_state`; U running/paused restarts |
| 92 | No indefinite stale ownership | PASS | P, U: startup clearing plus expired-lease takeover |
| 93 | Shutdown leaves zero app contexts | PASS | W zero owners/Chrome; U context counts |
| 94 | Shutdown leaves no unexpected leases | PASS | W `final_shutdown_clean` (0 owners) |
| 95 | 10,000-row page query bounded | PASS | B |
| 96 | 10,000-row summary measured | PASS | B 6.647 ms p95 |
| 97 | Polling does no population writes | PASS | B 0 rows over 100 polls |
| 98 | No per-session task/browser/context | PASS | B task count flat; W 1–2 Chrome processes |
| 99 | SQLite/local state retained | PASS | C: no new backend; no measured trigger |
| 100 | No distributed architecture | PASS | C: single process |
| 101 | No stealth/evasion | PASS | C: no stealth, init-script, UA, or webdriver changes |
| 102 | Phase 4 bounded principles intact | PASS | C/U: fixed pools, bounded queues, leases, shared context budget |
| 103 | Full suite recorded | PASS | 448 passed, 4 staging deselected |
| 104 | Ruff recorded | PASS | passed |
| 105 | mypy recorded | PASS | passed (darwin/linux) |
| 106 | Platform type issues documented | PASS | win32: 2 pre-existing `SIGKILL` errors, recorded above |
| 107 | Sensitive files ignored | PASS | `git check-ignore` for db, WAL, lock, `.env`, state |
| 108 | Docs match implementation | PASS | README, PROJECT_CONTEXT, and this report updated; commands verified |

**Matrix total: 108 PASS, 0 FAIL, 0 UNKNOWN (local evidence).**

## Defects Fixed During Acceptance

1. **Target-URL meaning was not stated in setup.** The workflow harness first entered
   the simulator's waiting-room page as the target, and every session became
   `ADMITTED` on its first check. This follows the existing admission rule: reaching
   the configured protected destination means admission. An operator pasting a Queue-it
   waiting-room URL would hit the same trap. Setup now states that the target is the
   protected staging page, that reaching it is recorded as `ADMITTED`, and that the
   waiting-room URL must not be used. The README says the same, and a test asserts the
   hint. Admission logic is unchanged.
2. **Acceptance tooling.**
   - `LocalQueueSimulator` gained a protected `/entry` path that redirects into the
     queue, as a protected site would.
   - The workflow is packaged as `queue-load-test-phase5-workflow` with an integration
     test.
   - Two tests close evidence gaps: a duplicate replacement is not a success, and
     Delete works without a state file.

No lifecycle, ownership, persistence, or shutdown defect was found during acceptance.

## Known Issues

- **Harness lock:** harness CLIs do not take the single-instance lock, so do not run
  them against a database a live UI is using.
- **In-memory status:** operator action banners and request tokens are process-local.
  Their effects persist; the banners reset on restart.
- **Automatic lease renewal:** automatic checks do not renew their lease. A check
  longer than `MONITOR_LEASE_SECONDS` (120 s) could be taken over. Writes stay fenced,
  but two browsers could briefly drive one identity.
- **Reservation revert:** if reverting an Add/Replace reservation fails during a
  database outage, the +1 stays. The next restart then creates that one visitor.
- **Open is synchronous:** the Open request waits for the restore, and polls queue
  behind it, so the table does not refresh during a slow Open.
- **No retargeting:** a new target requires a new, empty database.
- **Close-time evaluation:** the close-time inspection reads the page as it currently
  is. With the static simulator this was the value loaded at Open. On Queue-it it
  depends on the page's own live updates, which are UNKNOWN.
- **mypy on win32:** the pre-existing `SIGKILL` errors remain in the Phase 4 harness.

## Remaining UNKNOWNs

These are staging-specific and **NOT RUN**. They are not included in the matrix totals.

| # | Item | Status |
|---:|---|:---:|
| S1 | Acquisition through the UI against authorised Queue-it staging | UNKNOWN / NOT RUN |
| S2 | Headed Open restores a real Queue-it visitor (transfer and HYBRID) | UNKNOWN / NOT RUN |
| S3 | Real identity-mismatch incidence on Open/Refresh | UNKNOWN / NOT RUN |
| S4 | Close-time inspection of a live Queue-it page after operator dwell | UNKNOWN / NOT RUN |
| S5 | Refresh Now lifecycle and progress on real Queue-it themes | UNKNOWN / NOT RUN |
| S6 | Add/Replace uniqueness and duplicate incidence on Queue-it | UNKNOWN / NOT RUN |
| S7 | Real `PRE_QUEUE` → `ACTIVE_QUEUE` → `ADMITTED` transitions seen in the dashboard | UNKNOWN / NOT RUN |
| S8 | Dashboard and polling behavior with a real 10,000-identity population and live monitoring load | UNKNOWN / NOT RUN |

The Phase 4 UNKNOWNs (real 10,000-ID acquisition, restore reliability, and live cadence)
are unchanged.

## Recommended Operator Workflow

1. Configure `.env`: `DATABASE_URL`, `STATE_DIRECTORY`, and browser limits. Use a
   dedicated, empty database for each target.
2. Start the application with `queue-load-test-ui`.
3. Open `http://127.0.0.1:8000`.
4. Enter the authorised protected staging URL. This is the page that sends visitors
   into the queue, not the waiting-room URL.
5. Enter the requested session target and click **Start**.
6. Watch sessions appear, then watch lifecycle and progress update every 2 s.
7. Use **Pause Monitoring** to stop automatic checks; the backlog stays visible.
   **Resume Monitoring** continues them.
8. Use **Open in Chrome** to inspect one session in a visible window. The scheduler
   skips it while it is open.
9. Close the window, or click **Close**, to persist a final observation and return the
   session to automatic monitoring.
10. Use **Refresh Now**, **Replace**, **Delete**, and **+ New Session** as explicit
    actions. Delete is local only and does not cancel at Queue-it.
11. Stop with Ctrl+C or SIGTERM. Restart with the same command; the run, sessions, pause
    state, and population accounting are recovered, and any stale ownership is cleared.

Do not run a second UI process, or a harness, against the same database. Do not expose
the UI beyond the local machine.

## Future Work

These are evidence-based only. None of them is new architecture.

- **Run the gated staging workflow** (S1–S8) with an authorised URL, using this
  harness's check list as the script.
- **Lease renewal for automatic checks,** if staging shows checks approaching
  `MONITOR_LEASE_SECONDS`.
- **Asynchronous Open,** if a real headed restore is slow enough to freeze the
  dashboard noticeably.
- **Take the instance lock in harness CLIs** that open an operator database.
- **Replace `signal.SIGKILL`** in `harness/phase4_recovery.py` with a platform guard, if
  that harness must run on Windows.
