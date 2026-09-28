# Phase 7 Prompt 4 — Patchright Full Runtime and Dashboard Integration

**Date:** 2026-09-28  
**Decision:** **PASS on controlled local runtime evidence**  
**Workflow:** Patchright 74/74; Chrome control 74/74  
**Staging:** **NOT RUN / UNKNOWN**

## Outcome

Patchright passed the complete Quetty application and lightweight dashboard workflow
without changing application semantics. The formal run exercised a visible headed
Patchright manual window, headless automatic monitoring, real SQLite and state files,
the FastAPI lifespan and HTMX routes, bounded worker pools, browser-process kills,
application restart, ownership recovery, and Stop & Reset. Standard Chrome passed the
same extended workflow as the control.

Chrome remains the default and fallback for new runs. Patchright remains explicitly
selectable. Camoufox remains implemented and compatible with its persisted provenance,
but was excluded from the Phase 7 workflow result.

The committed aggregate report is
`docs/results/phase7_patchright_runtime_result.json`. It contains check names, counts,
timings, and bounded resource observations; it excludes Queue IDs, session IDs,
transfer URLs, cookies, and browser-state contents.

## Workflow matrix

| Area | Patchright | Chrome control |
|---|---:|---:|
| Scenario checks | 74/74 | 74/74 |
| Initial requested acquisition | 4/4 | 4/4 |
| Dashboard fields, search, filters, summary | PASS | PASS |
| Automatic monitoring and live progress | PASS | PASS |
| Pause, in-flight drain, visible backlog | PASS | PASS |
| Resume and continuing updates | PASS | PASS |
| Refresh Now while paused | PASS | PASS |
| Add while monitoring paused | PASS | PASS |
| Create-first Replace and Delete | PASS | PASS |
| Verified manual Open, ownership, Close | PASS, headed | PASS, controlled headless |
| Direct window close | PASS | PASS |
| Manual-window process crash and reopen | PASS | PASS |
| Automatic-monitoring process crash/replacement | PASS | PASS |
| Paused application restart | PASS | PASS |
| Stale manual and interrupted-monitor leases | PASS | PASS |
| Interrupted acquisition deficit resume | 3 to 6 | 3 to 6 |
| Environment-default migration fencing | PASS | PASS |
| Stop & Reset then backend change | PASS | PASS |
| Final resources and ownership | zero | zero |

The workflow used four initial sessions. Dashboard checks verified session ID, Queue ID,
Queue lifecycle/status, progress, creation state, valid/acquisition totals, remaining
target, monitoring state, due backlog, and manual browser state. Polling remained
read-only and did not expose transfer URLs or state paths.

## Monitoring and operator semantics

Pause persisted one runtime-control row. Any already-running checks drained and
released ownership; after that, timestamps stopped changing across more than two poll
intervals, no owner remained, all sessions and Queue IDs remained persisted, and the
due backlog stayed visible. Resume restarted due checks without rebuilding the
population.

Operator actions retained their independent semantics:

- Refresh Now completed while automatic monitoring was paused.
- Add acquired exactly one new session despite a duplicate submit and also completed
  while monitoring was paused.
- Replace first created a distinct valid identity and only then removed the selected
  old row. This was the sole intentional replacement; unrelated expected Queue IDs did
  not change.
- Delete removed only the chosen row, progress, and browser-state file.
- Requested target and operator population adjustment survived restart.

## Manual browser ownership

Patchright restored the persisted expected Queue ID into a visible headed Chrome
window. The dashboard showed `OPEN IN BROWSER` separately from Queue lifecycle. While
the window remained open, another session continued automatic monitoring but the
manual session's `last_checked_at` did not change. Refresh and Delete were refused for
that owned row, and the shared context count stayed within the configured limit.

Manual Close performed final inspection, saved progress/state, released ownership, and
returned the row to automatic monitoring. Direct page close likewise released and
parked an adopted session.

During the manual-crash scenario, three Patchright-controlled Chrome main processes
(automatic, visible creation, and manual pools) were killed. The manual lease was
released within 0.2 seconds, the authoritative identities were unchanged, and the next
Open repaired the bounded headed slot lazily. Close then returned the same row to
automatic monitoring.

## Browser crash, deadlines, and shutdown

A second kill during automatic monitoring terminated two Patchright Chrome main
processes. BrowserManager replaced only the required bounded slots; all sessions were
checked again, process count returned to one automatic process, and no new Queue
identity was acquired.

Patchright continued to use the project's existing bounded operation, close, controller
stop, task-drain, and repeated-cancellation mechanisms. No Patchright-only unbounded
workaround was added. The first full Patchright application shutdown completed in
0.216 seconds; Chrome completed in 0.091 seconds. Both left:

- zero active BrowserContexts;
- zero managed/descendant browser main processes;
- zero scheduler, operator, or manual ownership leases;
- zero queued/running operator work;
- no hung shutdown task.

Expected best-effort close warnings were logged after deliberate `SIGKILL`; cleanup and
replacement checks passed.

## Restart and backend provenance

Before restart the workflow persisted monitoring as paused, then injected one
future-dated manual lease and one future-dated interrupted-monitor lease. Exclusive
startup recovery cleared both without modifying identity, progress, lifecycle, or
schedule. The paused state and due backlog remained intact until Resume.

The Patchright database was restarted with the environment configured for Chrome. The
run and all its sessions still loaded as Patchright and no new-run preflight or backend
migration occurred. The Chrome control passed the inverse test with the environment
configured for Patchright. A target/backend change became possible only after Stop &
Reset deleted the old run and a new run was created. New Patchright runs recorded
`patchright` on the run and every session plus the observed installed-Chrome build.

## Identity continuity

There were zero unexpected Queue ID changes and zero automatic replacement identities.
The mismatch scenario was rejected and retained its expected Queue ID. Manual crash,
automatic crash, pause/resume, Refresh, application restart, stale ownership recovery,
and interrupted acquisition all operated on existing persisted rows.

Add, the explicit create-first Replace action, and no-ID adoption intentionally create
or adopt identities according to existing product semantics. Replace never mutates the
old row's Queue ID in place and leaves unrelated rows unchanged.

## Defects found and fixed

No production Patchright runtime defect was found in the successful controlled run.
Two evidence/documentation gaps were fixed:

1. The historical workflow process counter recognized standard Chrome and Camoufox but
   returned “unavailable” for Patchright. It now uses the shared backend-aware browser
   main-process classifier, enabling real Patchright crash/leak assertions.
2. The historical workflow CLI still defaulted to Camoufox after Phase 7 restored
   Chrome as the application default. Its default now matches Chrome.

The workflow was extended with generic checks for manual-process crash/reopen,
in-flight pause drain, Add while paused, stale ownership and interrupted-monitor
restart, environment-default fencing, post-reset backend change, bounded shutdown, and
operator-worker cleanup. These checks apply equally to Patchright and Chrome.

## Boundaries

- All browser traffic targeted `LocalQueueSimulator` on `127.0.0.1`.
- No Queue-it staging traffic was sent. Staging is **NOT RUN / UNKNOWN**.
- This result does not establish anti-detection behavior or real Queue-it reliability.
- Camoufox was not part of the Phase 7 Prompt 4 workflow acceptance matrix.
- The default backend remains Chrome pending final Phase 7 acceptance.

## Validation

- Formal Patchright headed workflow: **74/74**.
- Formal Chrome controlled regression workflow: **74/74**.
- Combined formal result: **148/148**.
- Focused application/UI/restart/recovery tests: **99 passed**.
- Full non-staging suite: **531 passed, 4 staging tests deselected**.
- Ruff, strict mypy (75 source files), and `pip check`: PASS.

## Next task

**Phase 7 Prompt 5.**
