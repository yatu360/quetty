# Phase 6 Camoufox Context Identity Strategy

**Date:** 2026-09-27

**Scope:** Phase 6 Prompt 3; local API, identity-replay, storage-state, and restart
evidence only

**Decision:** **FAIL — no supported per-context identity descriptor exists in the
installed Camoufox release. Do not use Camoufox for persisted Queue sessions.**

## Executive conclusion

The required model cannot be implemented truthfully with Camoufox 0.5.6 while also
retaining Quetty's shared, bounded browser-process architecture:

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
different process topology Quetty's long-term identity contract. It fails the bounded
shared-process, minimum-data, maintainability, and sensitive-state requirements. It is
not a safe workaround for the missing per-context API.

No browser-runtime identity schema was added to production state. Defining a format
around an incomplete preset or undocumented generated configuration would silently
claim continuity that the evidence disproves. Chrome remains the default and the only
supported backend for persisted Queue sessions.

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

There is deliberately **no selected production Camoufox identity strategy** for 0.5.6.
The gate is fail-closed:

- do not create persisted Queue sessions with `BROWSER_BACKEND=camoufox`;
- do not treat a preset as complete identity metadata;
- do not persist generated init scripts, Camoufox config dictionaries, environment
  chunks, or private helper output;
- do not silently use a new random `AsyncNewContext` identity during checks;
- do not replace the bounded shared-process design with a process/profile per session;
- do not make Camoufox the default.

A future acceptable API must provide a documented, serializable descriptor that is a
public input to per-context creation and recreates all identity-bearing values after
context, browser-process, and application restart.

## Persisted fields and version handling

No new fields are persisted in this prompt because no valid Camoufox identity payload
exists. In particular, Quetty does **not** persist a fabricated
`browser_identity_version=1` whose payload is only a partial preset.

When a supported API exists, the minimum sensitive state envelope should contain:

```text
browser_backend = "camoufox"
browser_engine = "firefox"
browser_identity_format = <documented Quetty format name>
browser_identity_version = 1
browser_runtime_metadata = <only the supported reusable public descriptor>
camoufox_package_version = "0.5.6"
camoufox_browser_version = "152.0.4-beta.30"
playwright_version = "1.62.0"
storage_state_backend = "camoufox"       # HYBRID only
storage_state_engine = "firefox"         # HYBRID only
```

That document must be stored through the existing sensitive state abstraction, must
embed and validate `session_id`, and must reject malformed payloads and unsupported
versions. It must never be logged at INFO, projected into dashboard rows, used as a
Prometheus label, or included in ordinary acceptance summaries.

This is a future schema description, not an implemented or accepted v1 format.
Implementing validators for a payload whose public contract does not exist would not
make it safe.

## Park/reopen lifecycle conclusion

The desired lifecycle remains correct:

```text
generate supported descriptor once
    -> create disposable context with descriptor
    -> acquire and persist Queue identity plus runtime artifacts
    -> close context
    -> load the same descriptor later
    -> create a new disposable context
    -> restore transfer first
    -> verify observed Queue ID equals the persisted expected Queue ID
    -> refresh HYBRID storage state
    -> close context
```

Camoufox 0.5.6 cannot supply the first and second steps at context scope. The dedicated
20-cycle test therefore concludes:

- core preset fields: **PASS, 20/20 cycles**;
- complete observed identity: **FAIL**;
- stable supported per-session descriptor: **FAIL**;
- production park/reopen loop: **NOT IMPLEMENTED / blocked**.

Queue ID mismatch behavior remains unchanged in existing Quetty code: the expected
persisted Queue ID is never replaced by an observed mismatch. Camoufox identity changes
do not grant permission to alter it.

## Restart results

### Browser-process restart with a reused context preset

**FAIL.** The preset survived an exact disk JSON round trip, but a new Playwright
controller, Camoufox browser, and `AsyncNewContext` did not reproduce the complete prior
observation. This is the required failure mode to avoid; Quetty must not claim
continuity from the preset.

### Application/repository restart

**FAIL / blocked for the required strategy.** Disk serialization does preserve the
preset dictionary itself, but that dictionary does not preserve the generated context
identity. Reopening repository objects cannot restore information the public API never
returned. No production repository/state integration was added.

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

The future semantics are unambiguous even though the Camoufox format is blocked:

- `TRANSFER_ONLY` must omit long-term Playwright `storage_state` but retain the stable
  browser identity descriptor and provenance needed to reconstruct the same device;
- `HYBRID` must retain transfer identity, storage state, and the same stable identity
  descriptor/provenance;
- HYBRID restoration remains transfer-first;
- both modes must verify the observed Queue ID against the immutable expected Queue ID.

Using TRANSFER_ONLY is not permission to regenerate browser identity. Because 0.5.6
cannot meet that condition, neither mode is approved for persisted Camoufox sessions.

## State store, checker, deletion, and reset

The state envelope and consistency checker were intentionally not changed. There is no
accepted identity format to validate and no legitimate persisted Camoufox identity
artifact to scan. Adding placeholder versions would create a misleading compatibility
promise.

When the API gate is resolved, the report-only checker must add findings for missing or
malformed Camoufox identity, unsupported descriptor version, backend/engine/state
provenance mismatch, embedded session-ID mismatch, and legacy Chrome state. It must
never repair, delete, or reinterpret state automatically.

The existing Delete path deletes the session's state document, and Stop & Reset Run
clears the state directory after wiping repository rows. Housing future browser runtime
metadata in that same per-session sensitive envelope will make both cleanup behaviors
apply without thousands of companion files. No cleanup behavior needed changing while
no Camoufox artifact is persisted.

## Focused tests

`tests/integration/test_camoufox_identity_evidence.py` adds four rerunnable local tests:

1. 20 reused-preset context cycles prove stable core fields but unstable complete
   identity;
2. JSON preset persistence plus a full Playwright/Camoufox runtime restart proves the
   descriptor remains incomplete;
3. clean public launch-option replay proves the process-scoped alternative and checks
   that no ambient environment is captured;
4. all four storage-state matrix directions verify actual cookie/local-storage values.

The tests persist no identity values, print no fingerprints, and send no external
traffic. Local focused result: **4 passed**. The final full non-staging suite result was
**475 passed, 4 staging tests deselected**.

Malformed descriptor, unsupported descriptor version, missing descriptor,
backend-provenance mismatch, identity mismatch through Camoufox, Camoufox delete/reset,
and successful repeated/application restart tests are **NOT IMPLEMENTED**, because
there is no supported valid descriptor against which those production paths can be
defined. Existing generic Queue-ID mismatch and state delete/reset regression coverage
remains in place.

## PASS / FAIL / UNKNOWN summary

| Question | Conclusion |
|---|---|
| Exact dependency compatibility | **PASS** |
| Multiple disposable contexts in one bounded Camoufox process | **PASS** |
| Normal-context launch identity repeatability | **PASS**, but shared |
| Fresh `AsyncNewContext` suitable for persistence | **FAIL** |
| Reused preset preserves core fields | **PASS** |
| Reused preset preserves complete identity | **FAIL** |
| Reused preset survives browser/app reconstruction as complete identity | **FAIL** |
| Public launch-option process replay | **PASS**, rejected architecture |
| Supported stable per-session context descriptor | **FAIL** |
| Required Quetty park/reopen model on Camoufox 0.5.6 | **FAIL / blocked** |
| Camoufox→Camoufox local storage state | **PASS** |
| Chrome→Camoufox local storage state | **PASS** |
| Camoufox→Chrome local storage state | **FAIL** |
| Chrome→Chrome local storage state | **PASS** |
| Any cross-engine Queue-it continuity | **UNKNOWN / NOT RUN** |
| Existing Chrome session safety | **PASS** through fail-closed legacy rule |
| Camoufox ready to become default | **FAIL** |

## Known limitations and unblock condition

- The identity probe observes a representative set of page-visible values; it is not a
  claim to enumerate every possible fingerprint surface. One changing identity-bearing
  value is sufficient to fail deterministic reconstruction.
- The storage matrix uses a local HTTP origin, not Queue-it.
- The preset-selection helper is publicly importable but the official usage guide does
  not describe it as a durable storage contract.
- Launch replay was tested only to evaluate an alternative; it is not approved state.
- No authorised staging tests ran.

This prompt can be revisited when a released Camoufox version documents a complete,
versioned, serializable **per-context** identity input/export contract. Until then,
Phase 6 Prompt 4 may proceed only as blocked design work; it must not enable creation,
restoration, monitoring, or manual open for persisted Camoufox Queue sessions.

**Exact next task: Phase 6 Prompt 4 — Camoufox Creation, Restoration, Monitoring, and
Manual Open.**

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
