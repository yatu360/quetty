# Phase 6 Camoufox Readiness and Compatibility

**Date:** 2026-09-27

**Scope:** Prompt 1 readiness plus Prompt 2 dependency/backend implementation evidence

**Decision:** **PARTIAL / NOT READY to make Camoufox the default**

## Prompt 2 Implementation Update

The dependency and basic runtime compatibility gates are now resolved. The project
pins and has locally validated this supported combination:

| Component | Previous | Prompt 2 pin / observation |
|---|---:|---:|
| Project Python | `>=3.12` | unchanged; validation host CPython 3.14.7 |
| Playwright | declaration `>=1.46`, installed 1.63.0 | **1.62.0 exactly** |
| Camoufox Python | absent | **0.5.6 exactly** |
| Camoufox browser | absent | **152.0.4-beta.30 exactly** |

`pip check` reports no dependency conflicts. Camoufox 0.5.6 declares Python
`>=3.10,<4` and `playwright<1.63`. Its released source explicitly records that
Playwright 1.61 and 1.62 fail on beta.29 and pass on beta.30. A current repository sync
also exposes beta.31 as the newest official stable build. Quetty deliberately pins
beta.30 because it has both that upstream compatibility evidence and a successful local
launch test; it does not follow the moving stable channel.

The minimal boundary is implemented as `BrowserBackend`, `ChromeBackend`, and
`CamoufoxBackend`. `BrowserManager` still owns the one shared async Playwright
controller, fixed process slots, least-loaded allocation, context and global limits,
shared headed/automatic capacity, one restart task per failed slot, close timeouts,
metrics, and shutdown. Backends own only launch, context creation, connectivity, close,
and version diagnostics. Chrome's launch remains `chromium.launch(channel="chrome")`.
Camoufox uses the public asynchronous `AsyncNewBrowser` and `AsyncNewContext` APIs.

`BROWSER_BACKEND=chrome|camoufox` is typed; `chrome` remains the default. The Camoufox
backend passes the exact installed `browser="152.0.4-beta.30"` selector. It never calls
an implicit active-version launch path, so a missing build cannot cause an application
download and instead reports:

```text
camoufox fetch official/stable/152.0.4-beta.30
```

The local-only `queue-load-test-camoufox-preflight` command reports human or JSON
evidence. On macOS arm64 it imported Camoufox 0.5.6 and Playwright 1.62.0, launched
browser 152.0.4-beta.30 asynchronously, opened one context, navigated to a `data:` URL,
closed the context and browser, returned active managed contexts and processes to zero,
and left no Camoufox OS process. No Queue-it or staging traffic was sent.

This resolves the package, launch, bounded-context, and cleanup gates only. It does not
resolve the stable per-session identity contract, browser provenance schema,
park/reopen continuity, headed workflow, cross-engine storage compatibility, or
authorised staging evidence. Camoufox therefore remains non-default and should not be
used for Queue-it runs before Prompt 3.

## Executive Decision

The existing parked-session architecture can accommodate a Camoufox backend without
creating one browser process per persisted visitor. Camoufox 0.5.6 exposes
`AsyncNewBrowser(playwright, ...)`, which can launch through an existing async
Playwright controller, and a returned Playwright `Browser` can own multiple isolated
`BrowserContext` objects. The current bounded process slots, context limits, shared
headed/headless budget, ownership leases, scheduler, and Queue ID verification can
therefore remain.

Prompt 2 resolved the dependency blocker that existed at readiness review: Playwright
was deliberately changed from 1.63.0 to the mutually supported 1.62.0, Camoufox 0.5.6
was pinned, and the complete Chrome regression suite was rerun. One architectural
blocker still prevents accepting Camoufox as the default:

1. The expected per-session stable identity contract is not currently demonstrated by
   a supported public Camoufox API. `AsyncNewContext(..., preset=...)` is public and
   creates per-context identities, but Camoufox 0.5.6 generates fresh audio, canvas,
   font-spacing, font-list, and voice values while converting a reused preset. A
   persisted preset is therefore not a proven complete stable identity descriptor.
   Persisting generated init scripts, internal config dictionaries, or private helper
   output would make Camoufox internals a project persistence contract and is rejected.

The small backend seam and package set are now implemented, with Chrome still the
default. Camoufox must not be used for real Queue-it sessions until the identity gate in
this report passes.

## Sources and Snapshot

This review used the current public sources on 2026-09-27:

- [Camoufox repository](https://github.com/daijro/camoufox), inspected at commit
  [`0c6cc0a397da9ffcbca51df8efec6749c2a97f67`](https://github.com/daijro/camoufox/commit/0c6cc0a397da9ffcbca51df8efec6749c2a97f67).
- [Camoufox 0.5.6 on PyPI](https://pypi.org/project/camoufox/0.5.6/), the latest
  published Python package at review time.
- [Official Python installation documentation](https://camoufox.com/python/installation/)
  and [official Python usage documentation](https://camoufox.com/python/usage/).
- The released 0.5.6 source distribution and the current upstream
  [`async_api.py`](https://github.com/daijro/camoufox/blob/0c6cc0a397da9ffcbca51df8efec6749c2a97f67/pythonlib/camoufox/async_api.py),
  [`fingerprints.py`](https://github.com/daijro/camoufox/blob/0c6cc0a397da9ffcbca51df8efec6749c2a97f67/pythonlib/camoufox/fingerprints.py),
  and [`pyproject.toml`](https://github.com/daijro/camoufox/blob/0c6cc0a397da9ffcbca51df8efec6749c2a97f67/pythonlib/pyproject.toml).
- [Playwright BrowserContext documentation](https://playwright.dev/python/docs/api/class-browsercontext),
  [Browser documentation](https://playwright.dev/python/docs/api/class-browser), and
  [authentication/storage-state guidance](https://playwright.dev/python/docs/auth).

Upstream `main` is useful for maturity and direction, but unreleased 0.5.7 behavior is
not treated as an installable project contract. The released 0.5.6 package is the
dependency authority for the resolution decision.

## Exact Dependency and Version Findings

| Item | Current finding | Readiness effect |
|---|---|---|
| Project Python contract | `requires-python = ">=3.12"`; Ruff and mypy target 3.12 | Compatible with Camoufox's Python range |
| Baseline interpreter | CPython 3.14.7 in `.venv` | Camoufox 0.5.6 advertises/supports 3.10 through 3.14 |
| Project Playwright declaration | `playwright==1.62.0` (previously `>=1.46`; installed baseline 1.63.0) | Exact mutually supported pin |
| Installed/validated Playwright | 1.62.0 | Compatible with Camoufox 0.5.6 and beta.30 |
| Current released Camoufox | 0.5.6, published 2026-09-06 | Installed and pinned exactly |
| Current upstream package version | 0.5.7 on `main`, not published on PyPI at review time | Must not be pinned as if released |
| Camoufox Python range | `>=3.10,<4.0` in 0.5.6 metadata | Includes project Python 3.12+ and the baseline 3.14.7 |
| Camoufox Playwright constraint | `playwright<1.63` in 0.5.6; unchanged on reviewed `main` | **PASS** with the explicit 1.62.0 pin |
| Dependency resolution | `pip check` with Camoufox 0.5.6 and Playwright 1.62.0 | **PASS** without overrides or ignored dependencies |
| Current official stable browser release | `152.0.4-beta.31`; beta.30 remains supported and installed | Quetty pins locally/upstream-validated beta.30 rather than following the moving channel |

There is no lock file. `pyproject.toml` alone currently permits Playwright upgrades, so
adding Camoufox without an explicit compatible Playwright range would make the resolver
choose the browser-control version implicitly. That is unacceptable for a browser
runtime dependency.

## Installation and Browser Distribution Model

Camoufox has two separately managed components:

1. Install the Python wrapper package, normally with pip. Quetty does not need the
   `geoip` extra because Phase 6 explicitly excludes proxy work and automatic
   proxy-derived location behavior.
2. Fetch the patched browser separately with `python -m camoufox fetch` (or the
   `camoufox` console command on Windows). It is not installed by Playwright's browser
   installer.

Camoufox 0.5.6 stores data under `platformdirs.user_cache_dir("camoufox")`. The
official CLI exposes the resolved location with `camoufox path`; the documented Linux
example is `~/.cache/camoufox`. The corresponding platform cache root is used on
Windows and macOS. Versioned browser installs live below that cache and the manager
also stores active-selection and repository-cache metadata there.

The version manager supports:

- a moving channel such as `official/stable`;
- an exact active pin such as `official/stable/152.0.4-beta.30`;
- direct fetching of an exact specifier;
- a launch-time `browser=` selector for an already installed build.

For reproducibility, CI and developer setup must pin all three layers:

- `camoufox==0.5.6`;
- a deliberately supported Playwright version/range, presently requiring `<1.63`;
- exact browser build `official/stable/152.0.4-beta.30`, verified after fetch.

Following `official/stable` is convenient but not reproducible. The app must pass an
exact installed browser selector so a missing build fails clearly rather than allowing
an implicit runtime download. The 0.5.6 source can auto-fetch when the default browser
path is missing; Quetty should instead make browser installation an explicit setup/CI
step and perform a startup preflight. No application worker should fetch a browser.

Current official release assets cover:

- Windows x86_64 and i686;
- macOS x86_64 and arm64;
- Linux x86_64 and arm64.

The package source also lists Linux i686, but the reviewed stable release has no Linux
i686 asset. CI must test the actual supported target matrix rather than infer support
from package code. Linux headed operation may use `headless="virtual"` with Xvfb, but
the Quetty operator's visible Open action requires a real display and should use
`headless=False` where supported.

## Current Async API Findings

### `AsyncCamoufox`

`AsyncCamoufox(**launch_options)` is a convenience async context manager. It starts its
own Playwright context manager, calls `AsyncNewBrowser`, closes the returned browser on
exit, and then stops Playwright. It returns either a `Browser` or, with
`persistent_context=True`, a single `BrowserContext`.

It is convenient for standalone scripts but is not the best fit for the existing
multi-slot `BrowserManager`: the manager already owns lifecycle, crash replacement,
multiple browser slots, and ordered shutdown.

### `AsyncNewBrowser`

`AsyncNewBrowser(playwright, ..., headless=..., from_options=...,
persistent_context=False, **kwargs)` accepts an existing async Playwright object. In
normal mode it launches `playwright.firefox` with Camoufox's executable and launch
configuration and returns a normal Playwright `Browser`. Headed (`False`), native
headless (`True`), and Linux virtual-display (`"virtual"`) modes exist.

This is the appropriate launch primitive for a future `CamoufoxBackend`. It permits
the current manager to keep one Playwright controller and bounded browser slots after
the package versions are made compatible. A separate Playwright lifecycle is not
required by the public API.

### Normal `browser.new_context()`

The returned object is a Playwright Firefox `Browser`. It can create multiple isolated
contexts with `browser.new_context()`, including `storage_state=...`. Those contexts
have isolated cookies/cache/storage, but Camoufox's launch-level fingerprint is shared
by the browser process. This supports bounded resource use, but not one distinct stable
browser identity per Quetty session.

### `AsyncNewContext`

Camoufox 0.5.6 publicly exports:

```python
await AsyncNewContext(
    browser,
    preset=...,
    os=...,
    ff_version=...,
    webrtc_ip=...,
    proxy=...,
    geolocation=...,
    storage_state=...,
)
```

Additional Playwright context options, including `storage_state`, flow through
`**context_kwargs` to `browser.new_context()`. It creates a new isolated context, then
adds the generated per-context identity init script.

If no preset is supplied, a fresh identity is generated. If a preset dictionary is
supplied, its navigator/screen/WebGL material is reused, but 0.5.6's public path still
draws fresh perturbation seeds, fonts, and voices. Consequently:

- it is suitable for distinct concurrent context identities;
- it does not establish that a saved preset recreates the same complete identity;
- its generated `config`, init script, and helper-function output are implementation
  details and must not be persisted by this project.

Current upstream `main` contains active work on deterministic identity components, but
it remains unreleased and still does not document a versioned export/import descriptor
contract for `AsyncNewContext`. It cannot close the gate for 0.5.6.

### Browser restart and context recreation

A disconnected Camoufox `Browser` uses the same generic Playwright
`Browser.is_connected()` and `Browser.close()` surface as Chrome. A backend can relaunch
the failed slot with `AsyncNewBrowser`. All contexts in that process are lost and must
be reconstructed, just as they are after a Chrome crash today.

This recovery is compatible with parked sessions only if each session's supported
identity descriptor is independent of the process and survives restart. That last
condition is the current blocker.

## Can One Camoufox Process Host Multiple Contexts?

**PASS at the public API/architecture level.** `AsyncNewBrowser` returns a Playwright
`Browser`, and both Playwright and Camoufox's `AsyncNewContext(browser, ...)` operate on
that browser repeatedly. Multiple isolated contexts can therefore share one process.

This is essential but not yet a performance claim. Before enabling the backend, a
local integration test must create the configured context limit in one Camoufox
process, verify storage isolation and distinct context identities, close them all, and
show process/context counts return to baseline. Capacity and crash tests must then run
at the intended bounded Phase 6 profile.

Quetty must never use persistent-context-per-session or browser-process-per-session as
a workaround for identity persistence.

## Playwright Lifecycle Decision

Use the existing manager-owned lifecycle after dependency resolution:

```text
one async_playwright controller
    -> bounded Chrome and/or Camoufox browser slots
        -> bounded disposable BrowserContexts
```

`AsyncNewBrowser` is specifically designed to receive an existing `Playwright`
instance. Using `AsyncCamoufox` per browser slot would create nested controller
ownership and complicate slot restart/shutdown. A separate Python environment or
browser service merely to carry a second Playwright version would add IPC, another
process lifecycle, and state/fencing failure modes; it is not the smallest safe design.

The lifecycle conclusion does not waive Camoufox's dependency ceiling. One compatible,
explicit Playwright version must serve both backends in the application environment.

## BrowserManager Compatibility Audit

### Chrome-specific launch assumptions

- The manager starts `async_playwright()` directly and launches only
  `playwright.chromium.launch(channel="chrome", headless=...)`.
- Constructor/settings names use `chrome_process_count`.
- Module/class docstrings and logs describe Chrome.
- `from_settings()` assumes one global browser kind.

These belong behind the backend launch seam or need browser-neutral names with legacy
configuration aliases.

### Chromium-specific assumptions

Production `BrowserManager` does not use CDP or Chromium-only Page APIs. Context/page,
navigation, `storage_state`, `is_connected`, and close operations are generic
Playwright APIs. The explicit `chromium` browser type and `channel="chrome"` are the
material Chromium assumptions.

The installed-Chrome process tests and resource harnesses detect main processes using
Chrome command-line details such as `--remote-debugging-pipe` and `--type=`. Those
tests/samplers require backend-aware process discovery; they cannot validate Camoufox
as written.

### Connection, restart, and process-slot assumptions

- Each slot contains one Playwright `Browser` and a set of owned contexts.
- Health uses `Browser.is_connected()`.
- A disconnected slot invalidates all owned contexts and has at most one restart task.
- Restart closes the old browser best-effort, launches one replacement, and preserves
  the stable numeric slot ID.
- Allocation is least-loaded across connected slots.

These assumptions are backend-neutral for a non-persistent Camoufox `Browser`. The
launch call must be delegated to the backend. Restart must recreate the same
process-level backend configuration, while per-session identity remains an input to
context creation rather than slot state.

### Context and capacity assumptions

- Context construction currently accepts only optional Playwright `storage_state`.
- Context creation happens while the manager allocation lock is held and is
  deadline-bounded.
- Global, per-process, and cross-pool context capacities are released synchronously
  before potentially hanging close calls.
- Callers receive `OwnedBrowserContext`, never a raw browser.

The capacity design should stay. Context creation needs one additional browser-neutral
session identity argument. The backend—not creation, restoration, monitoring, or the
UI—must translate that descriptor into the supported context API.

### Explicitly Chrome-named metrics and status

The following are semantically browser metrics but currently say Chrome:

- `BrowserCapacity.chrome_processes`;
- `RuntimeCapacity.chrome_processes` and `DashboardSummary.chrome_processes`;
- dashboard label “managed Chrome processes”;
- Prometheus help strings for managed processes, crashes, restarts, and context
  creation;
- Phase 2–5 benchmark fields such as `chrome_cpu_percent`, `chrome_ram_bytes`, and
  `observed_chrome_processes`;
- configuration/harness fields such as `CHROME_PROCESS_COUNT`.

Do not rewrite historical report schemas. New live/runtime metrics should gain
browser-neutral names or a bounded backend label, while compatibility aliases can keep
existing dashboards and harness outputs intact. Historical Chrome benchmarks remain
Chrome evidence.

## Headed and Manual Browser Compatibility Audit

`ApplicationRunRuntime` currently constructs three Chrome-oriented pools:

- automatic monitoring (`HEADLESS`);
- optional separate acquisition (`CREATION_HEADLESS`);
- one lazily started manual pool, headed unless tests set `manual_headless`.

`ManualChromeSessionManager` is Chrome-named, and UI messages/actions say “Open in
Chrome”. Its actual behavior is mostly generic:

- take a persisted renewable ownership lease before browser work;
- call the existing restorer and retain an `OwnedBrowserContext` plus `Page`;
- listen for Page and BrowserContext `close` events;
- use non-repairing `capacity(repair=False)` heartbeat checks so a visible browser is
  not relaunched behind the operator's back;
- renew ownership while live;
- inspect/persist state on close when possible;
- preserve the Queue ID and last state after a lost browser;
- release ownership on close, page/window close, crash, or shutdown.

Those invariants should remain. Camoufox supports `headless=False`, but its official
installation docs warn that it is not intended for ordinary human use and that window
resizing/fonts may look broken. Real headed Open, native window close, crash detection,
lease renewal, final inspection, and reopening after a process restart are therefore
**UNKNOWN** until tested on the supported host platforms.

UI wording should become backend-aware only when a run actually uses another backend.
Existing Chrome runs must continue to say Chrome. The current installed-Chrome manual
tests, Chrome PID discovery, and expected strings are Chrome-specific evidence and
must stay rather than being reinterpreted as Camoufox evidence.

## Creation, Restoration, Monitoring, State, and Operator Audit

### Creation

`QueueSessionCreator` depends on `BrowserManager.context()`, generic Playwright
`Page`, navigation, extraction, and `context.storage_state()`. It has no direct
Chromium launch call. It will need a newly generated backend identity descriptor before
context creation and must persist it atomically with the acquired Queue ID, transfer
URL, storage state, and provenance. Failed/duplicate creation must not leave a usable
descriptor attached to another session.

### Restoration

`QueueSessionRestorer` is transfer-first and uses storage state only as HYBRID fallback.
It verifies the observed Queue ID against the immutable expected Queue ID and never
adopts a mismatch. Those semantics are browser-agnostic and must not change.

Every transfer and storage attempt must create the context with the persisted backend
identity descriptor. A transfer success does not excuse changing the device identity.
`TRANSFER_ONLY` remains supported and therefore also needs identity/provenance storage
even though it intentionally has no Playwright storage-state fallback.

### Monitoring and scheduling

`QueueSessionMonitor`, lifecycle evaluation, due-session claims, leases, worker queues,
retry bounds, pause/resume, and scheduling contain no direct Chrome/Chromium API calls.
They consume a restorer result and should remain unchanged except for browser-neutral
types/names at composition boundaries.

### State storage

The filesystem store currently persists only a version-1 envelope around a raw
Playwright storage-state dictionary. It records the Quetty `session_id` and a digest,
but no source backend, engine, browser/package version, storage-state schema, or
Camoufox identity descriptor. TRANSFER_ONLY sessions normally have no state document.

Consequently the current state store cannot safely decide whether a fallback state is
appropriate for the selected engine. Provenance must be added before Camoufox sessions
exist. Existing version-1/legacy documents must be interpreted as Chrome/Chromium only
in the context of an existing Chrome run, not silently relabeled.

### Operator actions

Refresh, Add, Replace, Delete, Open/Close, Stop & Reset Run, fencing, population
adjustment, and failure containment compose existing browser services. They contain no
reason to change Queue ID or lifecycle semantics. Add/Replace must select the immutable
run backend; Replace must not switch the new visitor to another backend. Delete/reset
must also remove any identity descriptor artifact.

## Identity Semantics and Strategy Comparison

### A. One launch identity shared by every context in a process

Normal `browser.new_context()` is cheap and isolated for cookies/storage, and one
Camoufox process can serve many sessions. However, all contexts inherit the same
launch-level Camoufox identity.

- **Queue continuity:** transfer/storage may restore the Queue ID, subject to normal
  verification.
- **Device identity:** shared across unrelated Quetty sessions; stable only while the
  same launch configuration is recreated.
- **Isolation:** storage is isolated; browser/device identity is correlated.
- **Processes/resources:** bounded and efficient.
- **Park/reopen/app restart:** possible only with a persisted browser-level identity,
  but assignment of sessions to process identities becomes a durable scheduling
  constraint.
- **Headed Open:** may move a session to a different process identity unless the headed
  pool reproduces the same launch identity.
- **Complexity/stability:** public launch API, but conflicts with one identity per
  session and complicates pool allocation.

**Decision: FAIL for the intended design.**

### B. `AsyncNewContext` with a fresh identity on every restoration

- **Queue continuity:** transfer/storage may keep the Queue ID, but Queue-it sees a
  changing browser/device identity.
- **Device identity:** intentionally changes at every create/restore/check/Open.
- **Storage and transfer:** supported as Playwright context inputs, but not sufficient
  to make identity stable.
- **Isolation/processes/resources:** good; multiple contexts share bounded processes.
- **Park/reopen/app restart:** mechanically easy, semantically wrong for Phase 6.
- **Headed Open:** changes identity again.
- **Complexity/stability:** simplest public per-context API.

**Decision: FAIL.** It violates the explicit stable-session identity direction.

### C. One stable supported descriptor per persisted session

This remains the preferred architecture:

```text
session descriptor + storage_state + transfer URL
    -> temporary context in a bounded shared Camoufox process
    -> verify expected Queue ID
    -> refresh artifacts
    -> close/park
```

- **Queue continuity:** preserves transfer-first/HYBRID behavior and expected-ID
  verification.
- **Device identity:** stable across context and browser-process recreation if the
  descriptor contract is complete and deterministic.
- **Isolation:** one descriptor and one storage state per session; disposable contexts.
- **Processes/resources:** bounded independently of persisted population.
- **Park/reopen/app restart/headed Open:** naturally supported by reconstructing from
  persisted artifacts.
- **Complexity:** small backend seam plus versioned sensitive persistence.
- **Public API stability:** currently insufficient. A 0.5.6 preset is supported input,
  but is not a complete deterministic identity.

**Decision: preferred but FAIL on current evidence.** It can pass only when Camoufox
documents and supports a serializable, versioned, deterministic context identity input
or an equivalent export/import contract.

### D. Persistent profile/context or one browser process per session

A persistent context/user-data directory could preserve a profile, and a dedicated
browser launch could preserve a launch identity. Both couple each parked session to a
browser process or durable full profile and undermine disposable contexts, bounded
processes, crash recovery, cleanup, and the 10,000-session architecture.

**Decision: rejected.** It is not a safe workaround for the missing descriptor API.

## Park/Reopen Feasibility

The resource lifecycle is feasible: launch a small number of Camoufox browsers, create
and close many contexts over time, restore with `storage_state`, and restart failed
browser slots. The existing manager already implements those bounds and cleanup rules.

The identity lifecycle is **not yet feasible under an accepted public contract**. A
0.5.6 preset does not reproduce every generated identity component, and a new preset
changes the identity. Park/reopen is therefore **UNKNOWN/blocked** for the exact Phase 6
semantics until the stable-descriptor gate passes.

Queue ID verification remains mandatory after every reopen regardless of identity
evidence. A mismatch must preserve the expected Queue ID, fail the attempt, and never
replace the identity silently.

## Storage-State Compatibility

### Chrome-created state opened with Camoufox

**UNKNOWN.** Playwright documents a common storage-state shape and notes that cookies,
local storage, IndexedDB, and virtual credentials can be used across browsers depending
on the application. That is not proof that a Chrome-captured Queue-it journey works in
Camoufox/Firefox. Browser cookie behavior, partitioning, storage features, and the
application's binding of state to client characteristics may differ. No project or
upstream Camoufox evidence reviewed here validates this direction.

### Camoufox-created state opened with Chrome

**UNKNOWN** for the same reason. Schema acceptance is not journey continuity. No silent
cross-engine fallback is allowed.

`storage_state` also remains incomplete browser state: Playwright does not persist all
session storage or live JavaScript/process state. Supported transfer restoration stays
first in HYBRID mode.

## Persisted Browser Provenance

Existing runs should remain Chrome. An upgrade migration must assign legacy/current
rows to `backend=chrome`, `engine=chromium` based on the run that created them. It must
not reinterpret their state as Camoufox-compatible or regenerate identities merely
because the application default changes.

The minimum durable model should contain:

### Immutable run selection

- `browser_backend`: `chrome` or `camoufox`;
- an immutable backend configuration/version policy for the run;
- migration default `chrome` for every pre-Phase-6 run.

### Per-session provenance

- `browser_backend` and engine (`chromium` or `firefox`);
- Playwright version used to capture the state;
- backend package version (`camoufox` version where applicable);
- exact browser build/version;
- identity descriptor schema name/version and a reference to the sensitive descriptor;
- storage-state format/version and capture engine/backend.

Per-session fields are required even with an immutable run backend so corruption,
partial migrations, imports, and future explicit migrations fail closed. Backend and
engine mismatches must not silently load state.

The sensitive filesystem document may evolve from the current envelope to contain
optional `storage_state`, provenance, and a supported identity descriptor. That allows
TRANSFER_ONLY sessions to persist identity without pretending they have storage state.
The SQLite row should retain enough non-secret provenance and artifact versioning to
select/refuse a backend before loading the file.

Do not persist Camoufox init scripts, environment variables, generated internal config,
private helper return values, or undocumented preset-bundle indexes. The exact identity
payload must remain undefined until upstream publishes a supported contract.

## Minimal BrowserBackend Boundary

Keep `BrowserManager` as the bounded allocator/recovery owner and delegate only the two
operations that differ:

```python
class BrowserBackend(Protocol):
    backend_id: BrowserBackendId
    engine: BrowserEngine

    async def launch(self, playwright: Playwright, *, headless: bool) -> Browser: ...

    def new_identity(self) -> BrowserIdentityDescriptor | None: ...

    async def new_context(
        self,
        browser: Browser,
        *,
        identity: BrowserIdentityDescriptor | None,
        storage_state: ContextStorageState | None,
    ) -> BrowserContext: ...
```

Then:

```text
BrowserManager (slots, locks, limits, restart, cleanup, metrics)
    +-- ChromeBackend
    |     launch: playwright.chromium.launch(channel="chrome")
    |     identity: none
    |     context: browser.new_context(...)
    +-- CamoufoxBackend
          launch: AsyncNewBrowser(existing_playwright, exact browser build, ...)
          identity/context: blocked until a stable public descriptor exists
```

The descriptor should be an opaque project type with explicit backend and schema—not a
general dictionary passed through the application. Only the backend interprets it.
`OwnedBrowserContext`, global capacity, slot allocation, timeouts, close behavior, and
shared headed/headless capacity remain in `BrowserManager`.

Do not add a generic plugin framework, navigation abstraction, Page wrapper, scheduler
backend, or browser microservice. Creation/restoration need only supply the persisted
identity to `create_context`; they should continue to operate on standard Playwright
`BrowserContext` and `Page` objects.

## Dependency-Resolution Plan

1. Add a lock/constraints mechanism or exact tested dependency set before adding the
   runtime package.
2. Keep the current Chrome runtime behavior and backend default unchanged.
3. Choose one explicit path; do not let pip choose silently:
   - preferred: wait for a released Camoufox version whose declared Playwright range
     includes the project's selected Playwright version; or
   - deliberate fallback: pin Playwright 1.62.x because Camoufox 0.5.6 requires
     `<1.63`, then rerun the entire Phase 5 suite and installed-Chrome/manual/recovery
     evidence before accepting that project-wide downgrade.
4. Pin the Camoufox Python package exactly and install without the proxy/GeoIP extras.
5. Pin and explicitly fetch an exact Camoufox browser build; verify package, Playwright,
   browser version, platform, and checksum/status in CI/setup.
6. Make startup fail with a sanitized actionable error when the exact browser is
   missing. Do not download during application startup or worker activity.
7. Test dependency resolution and browser preflight on Windows, macOS, and Linux for
   the architectures actually shipped upstream.

Installing Camoufox in a separate environment does not solve the in-process API
boundary and is not recommended. Vendoring, patching Camoufox, ignoring metadata, or
installing Playwright 1.63 with `--no-deps` are prohibited.

## Migration Sequence

1. **Complete:** introduce `BrowserBackend`, `ChromeBackend`, browser-neutral capacity
   naming, and compatibility aliases. Route existing behavior through `ChromeBackend`.
2. **Complete locally:** pin Playwright 1.62.0/Camoufox 0.5.6/browser beta.30; add
   explicit setup fetch, actionable missing-browser failure, and local preflight. The
   cross-platform CI matrix remains future evidence.
3. Add immutable run backend plus per-session provenance/artifact schema. Migrate all
   existing runs/sessions to Chrome/Chromium without touching Queue IDs, transfer URLs,
   storage state, schedules, or ownership.
4. Obtain a supported stable Camoufox identity descriptor contract. Implement its
   backend serializer/validator without private APIs.
5. Add Camoufox unit and local integration tests for multi-context isolation,
   descriptor round trips across context/process/app restart, storage-state behavior,
   crash recovery, capacity, and headed Open/Close.
6. Add a new-run-only Camoufox selection behind an explicit experimental gate. Existing
   Chrome runs continue on Chrome. Do not offer in-place backend migration.
7. Run the authorised Queue-it staging matrix for HYBRID and TRANSFER_ONLY, including
   park/reopen and headed Open, with zero Queue ID replacement.
8. Only after every default gate below passes may a later prompt consider changing the
   default for newly created runs.

## Readiness Gates

| Gate | Status | Evidence/reason |
|---|---|---|
| Project Python compatible with package | **PASS** | Project `>=3.12`; Camoufox 0.5.6 `>=3.10,<4.0` |
| Current Playwright dependency compatible | **PASS** | Playwright 1.62.0 is pinned; Camoufox 0.5.6 requires `<1.63`; `pip check` passes |
| Explicit reproducible package/browser pin | **PASS** | Camoufox 0.5.6, Playwright 1.62.0, and browser beta.30 are exact pins |
| Existing async Playwright lifecycle reusable | **PASS** | Public `AsyncNewBrowser(playwright, ...)` accepts the existing controller |
| Multiple isolated contexts in one process | **PASS** for basic lifecycle | Public `AsyncNewContext` runs through existing bounded manager; larger-scale evidence remains future work |
| Stable supported per-session identity descriptor | **FAIL** | Reused 0.5.6 preset still regenerates identity components; no versioned export/import contract |
| Park/reopen with same Camoufox identity | **UNKNOWN** | Blocked by descriptor gate; not tested |
| HYBRID transfer-first semantics retainable | **PASS** architecturally | Restorer is browser-agnostic and already identity-verifying |
| TRANSFER_ONLY retainable | **PASS** architecturally | No dependency on storage fallback; descriptor persistence still required |
| Chrome state -> Camoufox state compatibility | **UNKNOWN** | No Queue-it/Camoufox cross-engine evidence |
| Camoufox state -> Chrome state compatibility | **UNKNOWN** | No Queue-it/Camoufox cross-engine evidence |
| Headed Open/Close/crash behavior | **UNKNOWN** | Public headed mode exists; project behavior not exercised |
| Browser crash/restart and capacity cleanup | **PASS** for manager mechanics; **UNKNOWN** for real crash | Backend-focused failure/slot/restart tests and real clean shutdown pass; OS-kill recovery remains untested |
| Existing runs remain on original backend | **PASS as decision** | Required migration policy; implementation is future work |
| Camoufox upstream production maturity | **UNKNOWN / operational risk** | Upstream explicitly says it is under development and may not suit stable production use |
| Phase 5 regression baseline | **PASS** | 463 passed, 4 staging deselected; Ruff and mypy clean |

## Evidence Required Before Camoufox Can Become the Default

All of the following are mandatory:

1. A released Camoufox package with a compatible declared Playwright range, or a
   consciously approved project Playwright pin/downgrade followed by the full Phase 5
   regression, installed-Chrome workflow, crash recovery, and headed-manual evidence.
2. A documented public Camoufox API that exports/accepts a complete, serializable,
   versioned per-context identity descriptor. Reusing it after closing the context,
   restarting the Camoufox process, and restarting the application must reproduce the
   same observable identity components without private APIs.
3. A same-process isolation test proving two descriptors remain distinct across
   concurrent contexts while cookies/storage are isolated, with no cross-context
   identity leakage.
4. A bounded-capacity test proving persisted population size does not affect browser
   processes, contexts, tasks, or workers, and all counts return to zero/baseline after
   shutdown and repeated crashes.
5. Local simulator tests for creation, transfer-first restore, forced storage fallback,
   progress refresh, park/reopen, app restart, expected-ID mismatch, and state refresh
   in both HYBRID and TRANSFER_ONLY modes.
6. Headed Open/Close/window-close/crash/reopen tests with ownership renewal/release and
   no automatic relaunch from heartbeat on each supported operator platform.
7. Provenance migration tests showing every existing row remains Chrome/Chromium and
   that a mismatched/unknown backend, engine, browser build, descriptor schema, or state
   schema fails closed without changing the expected Queue ID.
8. Packaging/CI evidence for exact package and browser pins, explicit fetch, missing
   browser failure, checksum/version reporting, and supported Windows/macOS/Linux
   targets.
9. An authorised Queue-it staging sample demonstrating stable expected Queue IDs across
   create -> park -> reopen -> monitor -> park, process restart, app restart, and headed
   Open. It must record zero silent identity replacements and report transfer and
   storage fallback separately.
10. A documented operational rollback: new runs can select Chrome, existing runs never
    change backend implicitly, and Camoufox failure cannot corrupt or retarget a Chrome
    run.

Cross-engine storage-state compatibility is not required to make Camoufox the default
for **new** runs if cross-engine migration remains disabled and provenance enforcement
is proven. It remains required before any explicit Chrome-to-Camoufox or
Camoufox-to-Chrome migration feature can be offered.

## Baseline and Prompt 2 Validation

The Prompt 1 pre-change baseline remains:

- `.venv/bin/python -m pytest` — **463 passed, 4 staging deselected**, one existing
  Starlette/httpx deprecation warning, 78.36 seconds.
- `.venv/bin/python -m ruff check src tests` — **passed**.
- `.venv/bin/python -m mypy src` — **passed**, 65 source files.
- `.venv/bin/python -m pip install --dry-run 'camoufox==0.5.6'` — no installation;
  resolver selected Playwright 1.62.0, confirming the 1.63 conflict.

No authorised staging run was performed and no Queue-it traffic was sent.

Prompt 2 then resolved the environment without `--no-deps`, monkey-patching, or private
Camoufox persistence APIs:

- `python3 -m pytest` — **468 passed, 2 skipped, 4 staging tests deselected**;
- focused backend/manager/config tests — **77 passed**;
- `ruff check src tests` — **passed**;
- `mypy src` — **passed**, 68 source files;
- `python3 -m pip check` — **no broken requirements**;
- Camoufox preflight — **PASS**, with zero active contexts and zero managed processes
  after shutdown; a process-table check found no Camoufox process.

## Readiness Conclusion

The minimal backend boundary, supported dependency set, and bounded basic Camoufox
lifecycle are implemented without reopening Phase 5. Camoufox is still not ready to
become the runtime default because Strategy C lacks a proven public stable-descriptor
contract and persisted provenance has not yet been added.

**Exact next task: Phase 6 Prompt 3 — Stable Camoufox Session Identity and Park/Reopen.**
