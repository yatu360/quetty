# Manual Strategy and Identity-Only PRE_QUEUE — Design and Acceptance

Date: 2026-10-04.

**Decision: ACCEPTED for local application behaviour.** Queue-it staging behaviour is
**NOT RUN / UNKNOWN**. Evidence is local only: deterministic fakes, a real SQLite
repository, and real Patchright and Chrome browsers against the local simulator.

This change adds two things:

1. **Manual Strategy** (`manual`), a third persisted run strategy beside Headed Window
   (`headed_window`) and Direct Monitoring (`direct`).
2. **The identity-only lifecycle fallback.** A valid Queue ID whose observation exposes
   no usable state is `PRE_QUEUE`.

## 1. Manual Strategy

### Semantics

- Setup offers three strategies. Manual Strategy is described as "opens one visible
  acquisition window at a time and waits for you to close it before opening the next".
- The persisted value is `manual`, stored in `run_config.monitoring_strategy`.
  - The column is plain text, so the change is additive with no migration.
  - Legacy rows still migrate to `headed_window`.
  - A run restarts with its persisted strategy. Changing the `MONITORING_STRATEGY`
    default never migrates a run.
  - Switching strategy requires Stop & Reset Run, as before.
- The defining difference is the acquisition lifecycle. After a window closes, the
  session is an ordinary parked `QueueSession`, monitored by the same browser monitor
  as Headed Window. That covers automatic checks, Refresh Now and Manual Open. There is
  no third monitoring engine and no browser per session.

### Exact acquisition lifecycle

The `SessionCreationController` for a Manual run uses `ManualAcquisitionHandler`
(`web/manual_acquisition.py`). It runs with **one worker and a one-slot queue**,
whatever `CREATION_WORKERS` and `CREATION_QUEUE_CAPACITY` say. The handler also holds
its own lock, so a second work item waits before reserving anything. One attempt runs
as follows:

1. **Reserve.** `QueueSessionCreator.reserve_session` persists a `CREATING` row with no
   Queue ID.
   - On an IPRoyal run, the row also carries a new immutable sticky-session ID.
   - The reservation is made for every provider, because the row holds the window's
     manual ownership.
2. **Open.** `ManualChromeSessionManager.run_acquisition_window` takes manual ownership
   of the row.
   - The window has its own headed slot, at capacity `MAX_MANUAL_OPEN_SESSIONS + 1`,
     so operator Open windows cannot starve it.
   - It opens **one visible window** (`headless=False` in production) at the run
     target, through the existing `QueueSessionRestorer.restore_open` no-ID path, which
     uses the row's own proxy.
   - With `keep_open_on_navigation_failure=True`, a failed first navigation (timeout,
     network or proxy error) is classified and counted, but the window stays open for
     the operator to reload or close.
3. **Wait.** The window's watcher loop waits on an event with a short tick (2 s).
   - Each tick checks that the browser is alive and renews the lease every
     `MANUAL_OPEN_LEASE_SECONDS / 3`.
   - Before a Queue ID, each tick checks for the access-restriction page. If the page is
     not restricted, it calls `adopt_open(require_live_queue=False)`.
   - Any valid page transfer identity is persisted at once through the existing
     adoption path: `CREATING` (the unique `queue_id` rejects a duplicate), then
     `PARKED`, then HYBRID state is saved.
   - The window is then inspected immediately through `inspect_open` and the browser
     monitor's `apply_restore_result`, so its lifecycle is persisted while the window
     stays open. It is re-inspected at the heartbeat cadence. Only verified, admitted or
     expired observations are written while the window is open.
   - The row keeps its manual lease throughout, so the scheduler never claims it.
4. **Close.** The operator closes the window. That is the only normal continue signal.
5. **Classify.** The watcher's existing finalisation runs: a final inspection if the
   page is still live, then the context closes and ownership is released. The handler
   then reads the authoritative row (see §1.2).
6. **Next.** The controller requests the next window only if the persisted
   successful-ID count is still below the target. With target *N*, window *N+1* never
   opens before window *N* has closed.

Nothing in this lifecycle closes a healthy window. A Queue ID, PRE_QUEUE, ACTIVE_QUEUE,
a navigation, a polling tick, any elapsed time, and the next creation item all leave it
open. There is no acquisition timeout. Low-level deadlines still apply to context
creation, navigation, page reads and shutdown.

### 1.1 How a manual close is detected

Detection is event-driven, with no busy loop. The watcher sleeps in
`asyncio.wait_for(closed.wait(), timeout=tick)`.

- **Window closed.** Every page of the window, including tabs and popups added later
  through the context `page` event, has a `close` listener. The window counts as closed
  only when its **last** page closes, or when the context emits `close`. Closing the
  original tab while a popup is still open does not end the window; observation moves
  to the remaining page.
- **Not a close.** Navigation, Queue-it redirects, reloads and admission redirects
  happen inside the same page and emit no `close` event.
- **Recorded reasons.** Each end records the first `ManualCloseReason`:

  | Reason | Cause |
  | --- | --- |
  | `OPERATOR_CLOSED` | The window closed while the browser was still connected. |
  | `DASHBOARD_CLOSE` | The operator pressed Close on the dashboard. |
  | `BROWSER_LOST` | The browser disconnected, or the heartbeat found the process gone. |
  | `OWNERSHIP_LOST` | The lease could not be renewed, so the app releases the window to keep one owner per identity. |
  | `SHUTDOWN` | Application shutdown, Stop & Reset, or cancellation. |

  When the application closes a context itself, the reason is already recorded, so the
  resulting events cannot be mistaken for an operator close.

### 1.2 Successful and unsuccessful closes

| Row after the window ends | Outcome | Counted |
| --- | --- | --- |
| Queue ID, not `FAILED` | `SUCCESS`. The row is unchanged (PRE_QUEUE, ACTIVE_QUEUE, …), its owner is released, and it is parked for the browser monitor. | Yes |
| Queue ID, but `FAILED` (identity mismatch seen in the window) | `PERMANENT_FAILURE` (`queue_identity_lost`). The row's history is untouched. | No |
| No ID, reason `SHUTDOWN` | Interrupted. The reservation and any state are deleted, and nothing is recorded. | No |
| No ID, duplicate Queue ID seen | `DUPLICATE`. FAILED row with `duplicate_queue_id`; the original identity is untouched. | No |
| No ID, last observation was the restriction page | FAILED row with `access_restricted_before_queue`. Counted in `access_restricted` and the consecutive count. | No |
| No ID, operator or dashboard close | FAILED row with `manual_window_closed_before_queue_id` | No |
| No ID, browser lost | FAILED row with `manual_browser_lost_before_queue_id` | No |
| No ID, ownership lost | FAILED row with `manual_ownership_lost_before_queue_id` | No |
| Window could not open | FAILED row with `manual_window_open_failed`, then a 5 s pause before the next attempt (no window churn) | No |

FAILED rows have `queue_id IS NULL` and `transfer_url = ''`. No Queue ID is ever
fabricated, and an existing identity is never replaced. A late identity on an
already-closed page is never adopted. After any unsuccessful attempt, the next attempt
is a **fresh** reservation, with a new session row and, on IPRoyal, a new sticky ID.

### 1.3 Dashboard

- Creation shows **`WAITING FOR MANUAL CLOSE`** while an acquisition window is open.
- A "Manual window" line shows one of `AWAITING QUEUE ID`, `QUEUE ID ACQUIRED` or
  `ACCESS RESTRICTED BEFORE QUEUE`.
- The acquisition row appears as `OPEN IN BROWSER`, with the existing Close button.
- The two-second HTMX refresh is unchanged.
- There is no Continue button: closing the window is the continue signal.
- Only enum values and counts are shown. Queue IDs and transfer URLs follow the
  existing rules.

### 1.4 IPRoyal

The invariant still holds: one persisted QueueSession = one immutable IPRoyal sticky ID.

- Each attempt's reservation gets a new ID before the window navigates anywhere.
- `restore_open` resolves the row's own ID, or fails closed with
  `PROXY_ASSIGNMENT_MISSING` and opens no context.
- That ID is kept through adoption, in-window inspections, close and later monitoring.
- There is no unproxied fallback. Proxy failure classification and proxy-IP
  observation (one lookup at close, under the Manual Open policy) are unchanged.

### 1.5 Access restriction

- A restricted page is surfaced (`ACCESS RESTRICTED BEFORE QUEUE`), and the window
  **stays open** until the operator closes it. The application never closes it to churn
  to another window.
- After the close, the attempt is a FAILED `access_restricted_before_queue` row, and a
  fresh window opens for the unmet target.
- **Adapted halt semantics.** `ACCESS_RESTRICTED_MAX_CONSECUTIVE` (default 25) still
  applies. On a Manual run it counts **operator-closed** restricted windows; a success
  resets it. Reaching it halts acquisition for the runtime (dashboard `HALTED`), and a
  run restart resumes.
  - This keeps the existing protection without violating window ownership: the halt
    only ever prevents a *next* window and never closes one.
  - Other strategies are unchanged.

### 1.6 Shutdown and restart

- `ApplicationRunRuntime.close()` calls `stop_accepting()` first. That ends the
  acquisition window with reason `SHUTDOWN`. Its final inspection persists any live
  identity, then the context closes and ownership is released.
- The controller sees the stop event and opens no next window.
- Any work item still waiting gets `ManualOpenError` (closing) and discards its
  reservation.
- The remaining ordered shutdown is unchanged and closes the headed pool and Chrome.
- After a crash, the startup ownership recovery clears the stale owner. The existing
  `discard_orphaned_reservations` then deletes the leftover `CREATING` no-ID row.
- Restart resumes from the persisted successful IDs and opens windows only for the
  remaining deficit.

### 1.7 Pause, Add, Replace, Delete, Refresh, Manual Open

- Monitoring pause/resume does not gate acquisition and is not used to sequence windows.
- Refresh Now and automatic checks use the browser monitor.
- Manual Open of a parked Manual-run session is the existing headed path.
- Operator **Add** and **Replace** on a Manual run still use the existing automatic
  creator (one bounded creation, no operator-owned window). This is unchanged behaviour
  and is listed below as a known limitation.

## 2. Identity-only lifecycle fallback

### Rule

The rule lives in `models/lifecycle.py` and applies through
`evaluate_monitoring_observation(observation, *, current_status=None)`:

1. The existing ordered evaluation runs first: connection lost, expired, admitted,
   turn started, ready (first in line), serviced soon, paused, explicit pre-queue,
   explicit active queue, then two or more progress values meaning ACTIVE_QUEUE.
2. Only if that would return `CHECKING` (no usable state) **and**
   `has_valid_queue_identity(observation)` holds is the result
   `identity_only_status(current_status)`.
   - `has_valid_queue_identity` requires `identity_match` not to be `False`, the
     expected (persisted) or observed Queue ID to be non-blank, and the two not to
     contradict each other.
3. `identity_only_status` is `PRE_QUEUE` whenever that is a legal transition from the
   persisted status (PARKED, CHECKING, CREATING, PAUSED, CONNECTION_LOST, PRE_QUEUE, or
   none).
   - A session already explicitly observed at a later in-queue stage (ACTIVE_QUEUE,
     SERVICED_SOON, TURN_STARTED, READY) **keeps that stage**. The earlier explicit
     signal is stronger evidence, and the transition rules forbid moving backwards.
   - This avoids an `InvalidQueueTransition` and never regresses a session.

### Precedence and exclusions

- **Stronger signals win.** ACTIVE_QUEUE, PAUSED, SERVICED_SOON, TURN_STARTED, READY,
  ADMITTED, EXPIRED and CONNECTION_LOST signals all take precedence.
- **Identity problems are excluded.** An identity mismatch is never PRE_QUEUE. In the
  browser path it remains `FAILED` with the expected ID kept, and Direct falls back to
  the browser.
- **Genuine failures are excluded.** These restore failures never reach the evaluator
  and remain `CONNECTION_LOST` with retry: navigation, HTTP, proxy, transfer
  unavailable, identity unverified, and state failures.
- **The Queue ID is never touched.** Missing progress never reacquires, replaces,
  clears or fails it.

### Where it applies

The fallback is in the one shared evaluator, called with the persisted status by:

- `QueueSessionMonitor.apply_restore_result` — browser checks, Headed Window, Direct's
  browser fallback, Refresh Now, Manual Open close, and Manual Strategy in-window
  inspection;
- `QueueSessionMonitor._persist_verified_observation` — Direct observations;
- `validate_direct_observation` — Direct acceptance, so an identity-only direct
  observation is now persistable as PRE_QUEUE instead of `UNKNOWN_LIFECYCLE` fallback.

Rows already persisted as `CHECKING` before this change are re-evaluated at their next
check.

Consequence for Direct: a direct response with a matching Queue ID and no lifecycle
fields is no longer an `UNKNOWN_LIFECYCLE` fallback. It is accepted directly as
`PRE_QUEUE`, or keeps the session's already-observed later stage. `UNKNOWN_LIFECYCLE`
remains for genuinely unusable lifecycles such as connection lost. The local simulator's
`unknown_lifecycle` status fault and its reviewed schema therefore now carry
`connectionLost` (a test fixture change only). The Direct unit tests and the Phase 8
Direct workflow were updated to match, and both pass.

## Evidence

| Source | Proves |
| --- | --- |
| `tests/unit/test_manual_strategy.py` (18) | Sequencing with 1 and 3 workers. No second window while the first is open, including after it acquires a Queue ID. An open-ended wait (≈1 s of 5 ms ticks) with nothing closed or opened. Close-to-advance and the windows 1→2→3→4 order. Persist-before-close and immutability after close. Unsuccessful close is FAILED and not counted, followed by a fresh replacement. Tabs and popups. Explicit state persisted. Restricted window stays open. Browser-loss classification. Dashboard Close. A second window refused. Duplicate never adopted. A distinct immutable IPRoyal ID per attempt. Shutdown is bounded with zero owners or leases and the interrupted reservation discarded. Restart resumes only the deficit. Runtime wiring (worker 1, headed, +1 slot, browser monitor) and other strategies unchanged. Persisted `manual`. |
| `tests/unit/test_manual_strategy_web.py` (6) | Setup offers all three strategies with the explanation. `manual` is persisted and restored after the environment default changes. A Headed run is not migrated. Switching requires Stop & Reset. Dashboard `WAITING FOR MANUAL CLOSE` and window state. |
| `tests/unit/test_manual_strategy_restorer.py` (5) | Keep-open on navigation failure, with Manual Open unchanged. The window routes through the reserved sticky ID, or fails closed. Adoption without live-queue markers only when requested. |
| `tests/unit/test_identity_only_lifecycle.py` (33) | Questions 18–34: evaluator fallback and precedence; mismatch and absent identity; no backward transition; browser, Direct, Refresh and normalisation paths; genuine failures stay CONNECTION_LOST; the Queue ID is never cleared. |
| `tests/integration/test_manual_strategy_browser.py` (2: Patchright, Chrome) | A real browser, the full `ApplicationRunRuntime` with `CREATION_WORKERS=3`, and the local simulator's identity-only page. Window 1 opens. The app does not advance for 2 s. The Queue ID is persisted as **PRE_QUEUE** while the window is open. Closing window 1 opens window 2, and closing window 2 completes a target of two. No third window. After shutdown: 0 active contexts, both pools stopped, no owners or leases. |
| Existing suites | Headed and Direct behaviour, pause/resume, Add/Replace/Delete/Refresh/Manual Open, access-restriction and sensitive-data tests are all unchanged and passing (see Tests run). |

The integration test runs the window headless so CI needs no display. Production
wiring (`manual_headless=False`, so visible) is asserted by
`test_manual_run_forces_one_visible_sequential_acquisition_worker`. The test closes the
page through Playwright, which raises the same `close` event as an operator closing
the window.

## Tests run

- `python -m ruff check src tests`: all checks passed.
- `python -m mypy src`: no issues, 113 source files.
- `python -m pytest -q`: 1074 passed, 4 deselected (baseline before the change: 1005).

## Remaining unknowns and limitations

- **Queue-it staging: NOT RUN.** It is unknown whether a real Queue-it page exposes a
  transfer identity before any pre-queue or active marker. On the real restriction
  page, only the known English phrase is detected.
- **Add and Replace on a Manual run** use the automatic creator, not an operator-owned
  window.
- **Shared headed capacity.** The acquisition window counts toward the shared headed
  manual-lease capacity. Operator Open keeps its `MAX_MANUAL_OPEN_SESSIONS` limit, so
  while the acquisition window is open one fewer operator window is available.
- **Brief PARKED status.** Between adoption and the immediate first inspection (a few
  milliseconds), the row's status is `PARKED`.
- **Queue ID detection is a 2 s DOM observation.** Close detection itself is
  event-driven.
- **Camoufox is not certified** for Manual Strategy (dormant backend policy unchanged).
- **The visible-window path was not run headed** in this validation. Headed Patchright
  and Chrome manual windows were exercised by Phase 7 Manual Open evidence.
