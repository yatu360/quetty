# Phase 6 Camoufox Context Identity Strategy

**Date:** 2026-09-27

**Scope:** Phase 6 Prompts 3–4; local API evidence plus the corrected Queue-session
identity model

**Decision:** Stable complete Camoufox fingerprint replay is **FAIL / unsupported** in
0.5.6, but it is **NOT REQUIRED** for Quetty. Stable Queue ID and Queue-it journey
continuity are the authoritative requirement. Camoufox is supported as an opt-in
backend for new runs; Chrome remains the default.

## Executive conclusion

Prompt 3 correctly proved that this model cannot be implemented truthfully with
Camoufox 0.5.6:

```text
one persisted Queue session
    -> one supported stable Camoufox context identity descriptor
    -> any disposable context in a bounded shared process
```

`AsyncNewContext(preset=...)` is the only public per-context reuse input. Reusing one
preset keeps the preset's navigator, screen, and WebGL values, but Camoufox 0.5.6
deliberately draws new canvas, audio, font-spacing, font-list, and voice identity
components every time. A controlled 20-cycle local test kept the observed core fields
constant while producing multiple canvas observations. A disk JSON round trip followed
by a new Playwright runtime and Camoufox browser also changed the observed identity. The preset is
therefore a reusable partial fingerprint input, not a complete stable browser identity.

The public `launch_options()` / `AsyncNewBrowser(from_options=...)` path did reproduce
the same observed launch identity across browser-process restarts. It is not selected:

- it is browser-process identity, shared by all normal contexts in that process;
- giving every concurrently active Queue session a different identity would require a
  keyed browser process per active session, replacing the present multi-context pool;
- its reusable value is a roughly 27 KB Playwright launch-options bundle rather than a
  documented, versioned identity artifact;
- with the default arguments that bundle copies the complete application environment,
  including unrelated credentials; the controlled test used `env={}` to prevent that;
- even the clean bundle encodes identity in Camoufox-owned `CAMOU_CONFIG_*` environment
  values and contains an installation-specific executable path.

Persisting that bundle would make implementation-owned launch configuration and a
different process topology Quetty's long-term identity contract. It remains rejected.

The Prompt 4 design correction is that browser fingerprint continuity was never the
external session invariant Quetty needs. The persisted `queue_id` is authoritative;
the transfer URL and same-backend HYBRID storage state are restoration mechanisms.
Every BrowserContext is temporary and disposable, and Camoufox browser/device
characteristics may change after parking. An observed Queue ID mismatch never replaces
the expected Queue ID and never triggers automatic identity creation.

Production now persists only the minimum runtime provenance: `browser_backend` on the
run and each Queue session. No fingerprint, preset, generated init script, private
Camoufox data, package environment, or fabricated identity version is stored.

## Exact validated runtime

The repository dependency declaration and the repaired project virtual environment are:

| Component | Exact value |
|---|---|
| Python | CPython 3.14.7 (project contract remains `>=3.12`) |
| Camoufox Python package | 0.5.6 |
| Playwright | 1.62.0 |
| Camoufox browser | `152.0.4-beta.30` |
| Host used for evidence | macOS arm64 |

At the start of this prompt, shell `python3` had the declared versions but `.venv` was
stale: it had Playwright 1.63.0 and no Camoufox. Running
`python -m pip install -e ".[test]"` in `.venv` resolved it normally to Camoufox 0.5.6
and Playwright 1.62.0. `pip check` then reported no broken requirements. No dependency
constraint was bypassed.

The exact browser pin is already installed and was used for every Camoufox result in
this report. No Queue-it host or staging environment was contacted.

## Public API findings

The installed package source, its exported objects, current upstream source, official
Python usage documentation, and PyPI metadata were rechecked. The relevant public
surface in 0.5.6 is:

### `AsyncNewContext`

The installed signature is:

```python
await AsyncNewContext(
    browser,
    *,
    preset=None,
    os=None,
    ff_version=None,
    webrtc_ip=None,
    proxy=None,
    geolocation=None,
    **context_kwargs,
)
```

It returns only a `BrowserContext`. It has no identity export, seed return value, or
metadata callback. With no preset it generates a new BrowserForge fingerprint plus new
perturbation values. With a preset, 0.5.6 converts the preset and then still generates
fresh spacing/audio/canvas seeds and font/voice selections before creating the context
and installing an init script.

`preset=` is a supported dictionary input, and Camoufox exposes preset-selection
helpers in `camoufox.fingerprints`. There is no documented versioned preset persistence
format, preset identifier, or method that captures the fully generated context identity.
The generated config, init script, environment chunks, bundled-preset file layout, and
private helpers are not accepted Quetty persistence contracts.

### Normal `browser.new_context()`

`AsyncNewBrowser` returns a normal Playwright Firefox `Browser`. Calling
`browser.new_context()` repeatedly gave the same observed launch-level identity in
three local contexts. It is resource-efficient, but every Queue session in the process
shares that identity. It cannot implement one identity per persisted session.

### `launch_options()` and `from_options=`

Camoufox publicly exports `launch_options()`, and `AsyncNewBrowser(from_options=...)`
explicitly accepts its result. A clean `launch_options(..., env={})` result was JSON
serializable and reproduced the same locally observed identity over two complete
browser launches.

This proves a reusable **browser-process configuration**, not a reusable context
identity. The default result also contains the caller's entire environment. Quetty must
never store it as returned by default. Reducing it to selected `CAMOU_CONFIG_*` values
would turn undocumented internal representation into a persistence contract, which the
prompt explicitly prohibits.

### `AsyncCamoufox`

`AsyncCamoufox` remains a convenience owner for a Playwright controller and browser.
It provides no additional identity export/import mechanism and is not appropriate for
the manager-owned multi-slot lifecycle.

### Current upstream

The reviewed upstream `main` still exposes `preset=` for per-context identity and
`from_options=` for launch replay. It does not publish a complete, serializable,
versioned context-identity export/import contract. Unreleased source is not treated as
the installed contract in any event.

Sources:

- [Camoufox Python usage](https://camoufox.com/python/usage/)
- [Camoufox 0.5.6 package metadata](https://pypi.org/project/camoufox/0.5.6/)
- [Current async API source](https://github.com/daijro/camoufox/blob/main/pythonlib/camoufox/async_api.py)
- [Current fingerprint source](https://github.com/daijro/camoufox/blob/main/pythonlib/camoufox/fingerprints.py)
- [Playwright browser-context API](https://playwright.dev/python/docs/api/class-browsercontext)
- [Playwright authentication and storage state](https://playwright.dev/python/docs/auth)

## Local strategy comparison

All pages were either `data:` documents or served on `127.0.0.1`.

| Strategy | Local observation | Repeat/restart result | Decision |
|---|---|---|---|
| A. normal `browser.new_context()` | Three contexts had the same full observed launch identity | Stable only because every context shares its process identity | **FAIL**: not one identity per Queue session |
| B. `AsyncNewContext()` without preset | Three contexts had three different core and canvas observations | A new random identity is expected on every reconstruction | **FAIL** |
| C. `AsyncNewContext(preset=same_dict)` | Core observation stayed fixed in 20 cycles; canvas observation changed | Full identity changed within one process and after disk round trip plus Playwright/browser restart | **FAIL** |
| D. `launch_options(env={})` replayed through `from_options=` | Same full observation across two browser launches | Browser-process replay **PASS** | **REJECTED** for per-session context architecture and persistence safety |

The selection order was Queue identity integrity, repeatable reopening, supported APIs,
bounded architecture, maintainability, and resource efficiency. No decision was based
on stealth.

## Selected identity strategy

The selected production identity strategy is **stable Queue-session identity, with a
disposable browser context**:

1. create a fresh `AsyncNewContext` through the Camoufox backend;
2. acquire and persist the Queue ID and supported transfer URL;
3. persist Playwright storage state only for HYBRID mode;
4. persist `browser_backend=camoufox` separately as provenance;
5. close the context completely;
6. reconstruct later in another fresh Camoufox context, restore transfer-first, and
   require the observed Queue ID to equal the expected persisted Queue ID.

Fingerprint values are irrelevant to the Queue identity decision and are not persisted.
Camoufox remains opt-in, browser processes remain shared and bounded, and no process or
profile is allocated per persisted session.

## Persisted fields and version handling

The exact new persisted field is:

```text
run_config.browser_backend
queue_sessions.browser_backend
```

The enum accepts only `chrome` and `camoufox`. Existing databases are migrated with the
safe historical default `chrome`. A run reopens with its persisted backend even if the
environment later changes, and a run/session backend mismatch fails before a browser
context opens. The report-only state checker reports that mismatch. No identity format
or version exists because no browser identity payload exists to version.

## Park/reopen lifecycle conclusion

The implemented lifecycle is:

```text
create fresh disposable Camoufox context
    -> acquire and persist Queue identity, transfer URL, and backend provenance
    -> save storage state when HYBRID
    -> close context
    -> create another fresh disposable Camoufox context later
    -> restore transfer first
    -> verify observed Queue ID equals the persisted expected Queue ID
    -> refresh HYBRID storage state
    -> close context
```

The dedicated production-path 20-cycle test concludes:

- Queue ID continuity: **PASS, 20/20 cycles**;
- fresh context and complete park after every cycle: **PASS**;
- HYBRID state refresh: **PASS, 20/20 cycles**;
- complete browser/repository restart after cycle 10: **PASS**;
- stable supported per-session descriptor: **FAIL**;
- production park/reopen loop: **PASS** because descriptor continuity is not required.

Queue ID mismatch behavior remains unchanged in existing Quetty code: the expected
persisted Queue ID is never replaced by an observed mismatch. Camoufox identity changes
do not grant permission to alter it.

## Restart results

### Browser-process and application/repository restart

**PASS for Queue-session continuity.** After ten successful restore/inspect/save/close
cycles, the test shuts down the Camoufox process and Playwright, closes the repository,
constructs a new repository, manager, backend, and Camoufox process, then completes ten
more cycles with the same expected Queue ID and persisted `camoufox` provenance.

**FAIL for fingerprint continuity**, as Prompt 3 established. That result has no effect
on the persisted Queue ID.

### Alternative launch replay

`from_options=` was **PASS** across complete browser launches in one application, but
remains rejected for the reasons above. It was not promoted into a process-restart or
application persistence format.

## Local storage-state compatibility matrix

The test created a cookie, local-storage value, and session-storage value on a local
HTTP origin, captured `context.storage_state()`, opened it in the destination engine,
navigated to the same origin, and read the values. A syntactically accepted JSON file
was not enough for PASS.

| Source state | Destination | Result | Meaningful observation |
|---|---|---|---|
| Camoufox | Camoufox | **PASS** | Cookie and local storage restored |
| Chrome | Camoufox | **PASS** | Cookie and local storage restored |
| Camoufox | Chrome | **FAIL** | Local storage restored; cookie was absent |
| Chrome | Chrome | **PASS** | Cookie and local storage restored |

Camoufox serialized the locally created cookie with `sameSite: "None"` while it was
not secure. Chrome rejected that cookie on import; it still restored the origin's local
storage. This is exactly why cross-engine compatibility cannot be inferred from the
shared JSON shape.

Session storage was absent after every restore. Playwright storage state does not make
it a persisted browser snapshot, so the matrix does not classify session-storage loss
as an engine-specific failure.

These are local generic Playwright-state results, not Queue-it continuity evidence.
Chrome-to-Camoufox happens to pass this fixture but remains **UNKNOWN for Queue-it**.
Camoufox-to-Chrome is **FAIL for the local fixture** and **UNKNOWN for Queue-it** because
no Queue-it request was made.

No automatic cross-engine state migration is permitted.

## Existing Chrome sessions and legacy rule

Repository history establishes the safe rule:

- all persistence phases before Phase 6 used installed Google Chrome/Chromium;
- Phase 6 Prompt 2 introduced the Camoufox backend boundary but explicitly prohibited
  its use for Queue-it runs before this identity gate;
- no Camoufox provenance or identity artifact has ever been written by production code.

Therefore every legacy state document and row without explicit backend provenance is
treated as **Chrome/Chromium-origin state**. It must not be opened in Camoufox or
silently converted. If an operator manually ignored the Prompt 2 prohibition and made
Camoufox state without provenance, the repository cannot distinguish it safely; it
must still fail closed as legacy Chrome rather than guess.

## TRANSFER_ONLY and HYBRID

The implemented semantics are:

- `TRANSFER_ONLY` omits long-term Playwright `storage_state` and retains transfer
  identity plus backend provenance;
- `HYBRID` retains transfer identity, storage state, and backend provenance;
- HYBRID restoration remains transfer-first;
- both modes must verify the observed Queue ID against the immutable expected Queue ID.

## State store, checker, deletion, and reset

The repository schema and report-only consistency checker now carry backend provenance.
They reject unknown enum values, default pre-Phase-6 rows/runs to Chrome, and report a
`backend_provenance_mismatch` when a session backend differs from its active run. There
is no Camoufox identity artifact whose format, session ID, or version could be malformed.
The checker never repairs, deletes, or reinterprets state automatically.

The existing Delete path deletes the session's state document, and Stop & Reset Run
clears the state directory after wiping repository rows. Housing future browser runtime
metadata in that same per-session sensitive envelope will make both cleanup behaviors
apply without companion files. There is no fingerprint artifact to clean up.

## Focused tests

`tests/integration/test_camoufox_identity_evidence.py` adds four rerunnable local tests:

1. 20 reused-preset context cycles prove stable core fields but unstable complete
   identity;
2. JSON preset persistence plus a full Playwright/Camoufox runtime restart proves the
   descriptor remains incomplete;
3. clean public launch-option replay proves the process-scoped alternative and checks
   that no ambient environment is captured;
4. all four storage-state matrix directions verify actual cookie/local-storage values.

`tests/integration/test_camoufox_queue_continuity.py` additionally exercises the real
production creator and restorer for 20 fresh-context cycles, a full process/repository
restart, HYBRID refresh, and mismatch preservation. The backend-parameterized Phase 5
workflow covers the complete application lifecycle. Descriptor-malformation/version
tests are intentionally absent because Quetty persists no descriptor.

## PASS / FAIL / UNKNOWN summary

| Question | Conclusion |
|---|---|
| Exact dependency compatibility | **PASS** |
| Disposable contexts in one bounded Camoufox process | **PASS** when serialized to one live context per process; unserialized churn **FAIL** (Prompt 5) |
| Normal-context launch identity repeatability | **PASS**, but shared |
| Fresh `AsyncNewContext` suitable for fingerprint persistence | **FAIL** |
| Reused preset preserves core fields | **PASS** |
| Reused preset preserves complete identity | **FAIL** |
| Reused preset survives browser/app reconstruction as complete identity | **FAIL** |
| Public launch-option process replay | **PASS**, rejected architecture |
| Supported stable per-session context descriptor | **FAIL** |
| Required Queue-ID park/reopen model on Camoufox 0.5.6 | **PASS, 20/20** |
| Full Camoufox process/repository restart | **PASS** |
| Queue-ID mismatch preserves expected identity | **PASS** |
| Backend provenance persistence and mismatch rejection | **PASS** |
| Camoufox→Camoufox local storage state | **PASS** |
| Chrome→Camoufox local storage state | **PASS** |
| Camoufox→Chrome local storage state | **FAIL** |
| Chrome→Chrome local storage state | **PASS** |
| Any cross-engine Queue-it continuity | **UNKNOWN / NOT RUN** |
| Existing Chrome session safety | **PASS** through fail-closed legacy rule |
| Camoufox ready to become default | **NOT YET; remains opt-in until Prompt 6** |

## Known limitations

- The identity probe observes a representative set of page-visible values; it is not a
  claim to enumerate every possible fingerprint surface. One changing identity-bearing
  value is sufficient to fail deterministic reconstruction.
- The storage matrix uses a local HTTP origin, not Queue-it.
- The preset-selection helper is publicly importable but the official usage guide does
  not describe it as a durable storage contract.
- Launch replay was tested only to evaluate an alternative; it is not approved state.
- Camoufox 0.5.6 repeated concurrent navigation waves were unreliable locally. The
  backend therefore permits one live context per managed Camoufox process while fixed
  workers and the global capacity coordinator remain bounded. Prompt 5
  (`docs/phase6_camoufox_benchmark.md`) confirmed the constraint:
  - without serialization, create/navigate/close churn wedged a process at 5 contexts,
    and it still reported connected;
  - with serialization, 1–4 processes and all recovery scenarios passed.
- No authorised staging tests ran.

Fingerprint continuity can be revisited if a future released API documents a complete,
versioned per-context descriptor, but Quetty does not depend on that capability.

## Validation

- `.venv/bin/python -m pytest tests/integration/test_camoufox_identity_evidence.py -q`:
  **4 passed**.
- `.venv/bin/python -m pytest`: **475 passed, 4 staging tests deselected**. The first
  run hit the previously documented manual-ownership timing flake; it passed in
  isolation and the final full run was clean.
- `.venv/bin/python -m ruff check src tests`: passed.
- `.venv/bin/python -m mypy src`: passed, 68 source files.
- `.venv/bin/python -m pip check`: no broken requirements.
- Authorised staging: **NOT RUN**; no Queue-it traffic was sent.
