# Phase 7 Prompt 2 — Patchright Browser Backend Integration

**Date:** 2026-09-28  
**Status:** **COMPLETE on local/data-URL evidence**  
**Staging:** **NOT RUN / UNKNOWN**

## Scope and policy

Patchright is now a selectable candidate backend. Standard Chrome remains the default
and control/fallback. Camoufox remains implemented and selectable, but is dormant and
excluded from Phase 7 benchmark/acceptance obligations.

```text
BROWSER_BACKEND=chrome|camoufox|patchright
default for new runs: chrome
```

No existing row is migrated. A persisted run restarts with its recorded backend,
legacy provenance still migrates to Chrome, and switching backends still requires Stop
& Reset Run. Queue ID remains authoritative. This prompt did not begin the Patchright
Queue-session restoration strategy.

## Architecture

There is still one application runtime and one authoritative browser manager:

```text
application services
  -> BrowserManager (slots, capacity, deadlines, restart, shutdown)
       -> ChromeBackend       -> Playwright controller -> installed Chrome
       -> CamoufoxBackend     -> Playwright controller -> pinned Camoufox
       -> PatchrightBackend   -> Patchright controller -> installed Chrome
```

`PatchrightBackend` implements launch, isolated context creation, connectivity, context
close, browser close, and package/browser diagnostics. It supplies Patchright's async
controller starter beneath the seam because Patchright and Playwright are independent
Python packages with nominally distinct API classes. Browser/controller objects remain
opaque at that narrow seam; application services never import Patchright.

`BrowserManager` remains responsible for:

- fixed browser-process slots and per-process/global context ceilings;
- the shared automatic/headed active-context coordinator;
- bounded launch, context creation, close, restart, and controller shutdown;
- capacity release after creation failure, cancellation, disconnect, and shutdown;
- one bounded replacement task per failed slot;
- process/context accounting, diagnostics, and cleanup.

Patchright starts with the ordinary Chrome-style concurrency model. Multiple isolated
temporary contexts may share one bounded browser process. No Camoufox semaphore or
one-context-per-process behavior was copied because Prompt 1 found no evidence requiring
it. Disconnected Patchright processes use BrowserManager's existing replacement path;
connected navigation timeouts do not trigger a special threshold yet.

Browser-library exception families are normalized in `browser/errors.py`, keeping
creation, extraction, admission, and restoration services independent of Patchright.

## Exact runtime and browser decisions

- Python: project `>=3.12`; local validation 3.14.7.
- Patchright: exact `1.63.0`.
- Playwright: retained exact `1.62.0`.
- Camoufox: retained exact `0.5.6`, browser `152.0.4-beta.30`.
- Patchright browser: installed Google Chrome via `channel="chrome"`; local build
  `153.0.8010.54`.
- Patchright-managed Chromium/FFmpeg is not installed or used.
- Runtime and preflight never execute an install or download command.
- No undocumented launch flags were added.

## Preflight and provenance

Creating a **new** Patchright run invokes `run_patchright_preflight()` before any run is
persisted. The setup path uses one temporary context/page, navigates only to an in-memory
`data:` URL, closes page/context/browser/controller, and requires clean resource return.
It validates exact `patchright==1.63.0` and launches installed Chrome through the
accepted channel.

Failure returns HTTP 503, persists nothing, and provides an explicit dependency or
Chrome installation remedy. Runtime never downloads a browser. Existing runs do not
rerun new-run preflight; they restart directly from persisted provenance.

On success:

- `run_config.browser_backend = patchright`;
- `run_config.browser_build` records the Chrome version observed by preflight;
- every new `queue_sessions.browser_backend = patchright`;
- the dashboard identifies the observed build as running via Patchright.

Chrome rows remain Chrome with NULL build. Camoufox rows remain Camoufox with their
pinned build. Legacy schema migration remains `DEFAULT 'chrome'`. Run/session mismatch
continues to return `BACKEND_MISMATCH` before a BrowserContext or state read is attempted.

## Headed/manual and recovery behavior

The existing headed manager calls the same backend factory. Patchright therefore uses a
bounded shared headed Chrome process with up to `MAX_MANUAL_OPEN_SESSIONS` contexts,
matching standard Chrome topology. Open/Close ownership, shared capacity, leases,
window-loss handling, final inspection, and shutdown ordering are unchanged.

Patchright browser disconnects are detected through `is_connected()`. BrowserManager
closes lost contexts, releases capacity, closes the failed browser best-effort, and
installs one replacement in the same slot. Shutdown closes all contexts and processes,
then stops the Patchright controller using the same bounded controller-stop path.

## Local evidence

Deterministic coverage verifies:

- configuration parsing and factory selection;
- Patchright launch options, package/browser diagnostics, and actionable missing-Chrome
  errors;
- ordinary bounded capacity and headed selection;
- capacity release after context-creation failure;
- context/browser/controller cleanup;
- disconnected-slot replacement without process multiplication;
- new-run preflight success/failure and no persistence on failure;
- Patchright run/session/build round trips and existing-run restart selection;
- mismatch rejection before context creation;
- Patchright session provenance at creation;
- Chrome control behavior.

The installed-browser integration creates two simultaneous Patchright contexts through
the real BrowserManager, navigates both only to `data:` URLs, then returns process and
context counts to zero. No Queue-it traffic was sent.

Final validation:

- focused Patchright/backend/provenance/preflight plus Chrome regression tests:
  **191 passed**;
- complete normal suite: **527 passed, 4 staging tests deselected**;
- Patchright local preflight: **5/5** disposable contexts, Chrome 153.0.8010.54,
  zero contexts/processes after shutdown;
- Ruff: PASS;
- strict mypy: PASS (73 source files);
- `pip check`: PASS.

The exact command ledger is recorded in `CHANGELOG_AI.md`.

## Known limitations and next task

- This proves backend lifecycle integration, not Queue-it identity continuity.
- Transfer/state restoration fidelity in fresh Patchright contexts is untested here.
- The observed installed Chrome build is provenance, not an immutable browser pin;
  Chrome can be externally updated.
- Persistent user-data directories remain deliberately rejected as an architectural
  redesign; disposable-context identity behavior is Prompt 3's subject.
- Patchright-specific sustained capacity, recovery, manual workflow, and final
  acceptance gates belong to later prompts.
- Staging remains **NOT RUN / UNKNOWN**.

Next: **Phase 7 Prompt 3 — Patchright Queue identity restoration.**
