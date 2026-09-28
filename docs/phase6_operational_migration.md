# Phase 6 Operational Migration: Camoufox Default Decision

> **Superseded (2026-09-28).** This is a historical Phase 6 record. Phase 7 moved the
> new-run default back to Chrome (Prompt 1) and then to **Patchright** (Prompt 6,
> `docs/phase7_acceptance.md`). Camoufox is now retained as a dormant/experimental,
> uncertified backend. Existing runs keep their persisted backend.

**Date:** 2026-09-27
**Scope:** Phase 6 Prompt 6. Local simulator evidence only. **No Queue-it or staging
traffic was sent.**

## Decision

**Camoufox is now the default browser backend for NEW runs**
(`BROWSER_BACKEND=camoufox`). Chrome remains fully supported with
`BROWSER_BACKEND=chrome` and stays covered by the regression workflow.

This is a local-evidence decision:
- Every default gate below passed locally.
- The gates concern Queue-session correctness: restoration, Queue ID preservation,
  bounded resources and recovery, and Phase 5 workflow compatibility. They do not
  concern fingerprints.
- Queue-it staging behavior for Camoufox remains **UNKNOWN** and belongs to Phase 6
  acceptance.
- Existing runs are unaffected. Each keeps the backend it was created with.

## Why fingerprint continuity is not required

The durable identity of a Quetty session is its persisted **expected Queue ID** and the
Queue-it journey it names.
- **Recovery path:** a parked session is recovered by restoring through the supported
  transfer URL (or same-backend HYBRID storage state) in a fresh context. The live
  Queue ID is then verified against the persisted value.
- **What may differ:** fresh Camoufox contexts may present different canvas, WebGL,
  font, or device characteristics. That is expected, not a failure.
- **What may not differ:** a changed Queue ID is a failure. It is never written over the
  persisted value and never replaced automatically.
- **What is persisted:** Camoufox 0.5.6 has no supported complete per-context
  fingerprint descriptor (Prompt 3: FAIL). Quetty therefore persists none, and does not
  read or compare fingerprint values.

## Evidence against the default gates

| Gate | Result | Evidence |
|---|---|---|
| Supported Python / Playwright / Camoufox combination | **PASS** | Python ≥ 3.12 (validated on 3.14.7), `playwright==1.62.0`, `camoufox==0.5.6`. `pip check` is clean. The preflight enforces the exact package pins. |
| Deterministic Camoufox installation | **PASS** (version) / **UNKNOWN** (artifact integrity) | Exact specifier `official/stable/152.0.4-beta.30`. Launch selects that installed build and can never fetch. The preflight verifies the running build. Whether `camoufox fetch` verifies a download checksum was not established. |
| Bounded shared-process / multi-context behavior | **PASS, with the 0.5.6 constraint** | Many sessions share a small fixed pool of processes through disposable contexts: 40 sessions on 2 processes, and 1–4-process capacity cases had 0 failures. Concurrent contexts are limited to **one live context per process**. Unserialized churn wedged a process at 5 contexts, so that mode is unsupported (see the benchmark). |
| Queue-session creation | **PASS** | 40/40 via the real creator. Setup and deficit acquisition pass in the workflow. |
| Park | **PASS** | Every context closes after creation and after every check. Zero contexts and leases after each sweep. |
| Repeated park/reopen | **PASS** | 20/20 continuity cycles including a process/repository restart. 200/200 verified sweep restores. |
| Queue ID verification | **PASS** | Every restore verifies. An injected mismatch fails and keeps the expected ID, in both the workflow and the benchmark. |
| Transfer restoration | **PASS** (local) | Transfer-first on every restore. |
| Compatible storage restoration | **PASS** (local, same backend) | A transfer outage falls back to same-backend HYBRID state. Missing or corrupt state fails without a new identity. |
| Monitoring | **PASS** | Real scheduler and monitor sweeps. Workflow monitoring checks pass. |
| Refresh Now | **PASS** | Works while paused, including after an automatic-browser kill. |
| Add / Replace / Delete | **PASS** | The workflow covers Add with duplicate-submit dedupe, create-first Replace, and Delete with progress/state cleanup. Backend-agnostic focused tests cover a failed or duplicate Replace keeping the old row. |
| Headed Open / Close | **PASS** | Visible headed Camoufox workflow 63/63. Benchmark covered manual Close, window loss, process kill, shutdown, reopen, and 10 cycles of concurrent open/close. |
| No-ID Open / adoption | **PASS** | Unique adoption, and duplicate adoption rejected. |
| Pause / resume | **PASS** | No automatic claims while paused; operator paths keep working. |
| Restart | **PASS** | App restart keeps target, Queue IDs, backend, and pause state. Stranded leases are recovered. |
| Browser crash recovery | **PASS** | `SIGKILL` with 0/1/3 contexts, multi-slot, ×10 repeated, and mid-sweep kills. The workflow crash step kills every browser while monitoring runs. The new unresponsive-process restart also covers a connected-but-wedged process. |
| Stop & Reset Run | **PASS** | Rows, progress, state, owners, and processes all end at zero, and the UI returns to setup. |
| Clean shutdown | **PASS** | Zero contexts, processes, leases, and owners with automatic, running and queued operator, and headed work in flight. |
| Acceptable local resource behavior | **PASS, with a higher cost than Chrome** | 2 processes (default `CHROME_PROCESS_COUNT`): browser tree RSS average about 2.0 GB, peak about 3.4 GB. Restores are about 2.5–3× Chrome's local latency. Application RSS is about 180 MB. |
| Zero silent Queue ID replacement | **PASS** | 0 changes and 0 replacements across all Prompt 4–6 evidence. |
| Stable fingerprint replay | **Not required** | Deliberately not a gate. |

Evidence sources:
- `docs/phase6_camoufox_readiness.md`
- `docs/phase6_camoufox_context_strategy.md`
- `docs/phase6_runtime_integration.md`
- `docs/phase6_camoufox_benchmark.md` and
  `docs/results/phase6_camoufox_benchmark_result.json`
- the Prompt 6 validation runs below

## Exact versions

| Component | Version |
|---|---|
| Python package `camoufox` | 0.5.6 (`CAMOUFOX_PACKAGE_VERSION`) |
| Python package `playwright` | 1.62.0 (`PLAYWRIGHT_VERSION`) |
| Camoufox channel / build | `official/stable` / `152.0.4-beta.30` (`CAMOUFOX_BROWSER_VERSION`) |
| Chrome (fallback, as validated) | installed Google Chrome 153.0.8010.54 (`channel="chrome"`, externally updated) |

The three Camoufox-side pins live in `pyproject.toml` and
`queue_load_test/browser/backend.py`.

## What changed in Prompt 6

- **Default:** `Settings.browser_backend` now defaults to `camoufox`. It applies to new
  runs only. The Phase 5 workflow harness CLI default follows it.
- **New-run preflight:** `POST /setup` runs `run_camoufox_preflight()` before
  persisting a Camoufox run. The preflight checks:
  - package import and the exact package/Playwright versions;
  - that the pinned build is installed;
  - a launch, one context, and one `data:` page;
  - complete cleanup.

  On failure the response is HTTP 503, **nothing is persisted**, and the page names the
  fix (for example `camoufox fetch official/stable/152.0.4-beta.30`) and the Chrome
  fallback. Chrome runs skip it. Existing runs never re-run it. The CLI
  `queue-load-test-camoufox-preflight` uses the same code
  (`queue_load_test/browser/preflight.py`).
- **Build provenance:** `run_config.browser_build` records the pinned Camoufox build
  when a run is created. It is NULL for Chrome and for legacy runs; the migration is
  additive and nullable. Restart with a different pinned build logs
  `run_browser_build_changed`, and the dashboard shows
  `<installed> (run created with <recorded>)`.
- **Unresponsive-process restart:** Prompt 5 found that a Camoufox process could stop
  completing navigations while still reporting connected.
  - **Detection:** the restorer and creator now report navigation outcomes. After 3
    consecutive navigation timeouts on one connected Camoufox process, `BrowserManager`
    replaces that slot through its ordinary bounded restart path. This is counted in
    `browser_unresponsive_restarts_total` and logged as
    `browser_process_unresponsive`.
  - **Evidence:** the unserialized 5-context churn case that had 0/30 restores in
    Prompt 5 now recovered to all 10 sessions verified, with 2 restarts and 0 leaked
    processes.
  - **Scope:** Camoufox only (`unresponsive_restart_threshold = 3`). Chrome keeps
    disconnect-only recovery, because its processes host up to 25 contexts and a slow
    target must not discard healthy in-flight work.
- **Diagnostics:** `browser_backend_info{backend, browser_build} 1` is one series per
  process with no Queue IDs, URLs, state, or fingerprints. `browser_manager_started`
  logs the backend, browser version, and package version through the allow-listed
  fields.
- **Controlled workflow:** the Phase 5 workflow gained a browser-crash step: `SIGKILL`
  of every live browser while monitoring, with monitoring resuming and zero identity
  change. The headed-mode process bound now accounts for the separate headed creation
  pool.

**Updated in Prompt 7 (acceptance):** the restart also triggers when a context close
misses its deadline or when a call ignores repeated cancellation. Every browser call is
now bounded by `await_bounded`, the Camoufox lease is held until a close completes, and
a stuck Playwright driver is killed at shutdown. See `docs/phase6_acceptance.md`.

## Persisted backend semantics

- `run_config.browser_backend` and `queue_sessions.browser_backend` are set when the
  run is created. They are immutable for the life of the run.
- `ApplicationRunRuntime.start_run` builds every browser pool from the **persisted run
  backend**, never from the current `BROWSER_BACKEND`. So:
  - An existing Chrome run still restarts as Chrome after the default changed to
    Camoufox. This is covered by
    `test_existing_chrome_run_restarts_as_chrome_after_default_change`.
  - A new run created after the change persists Camoufox and restarts as Camoufox.
    This is covered by the workflow restart checks.
- **Changing backend during a live run is unsupported.** Stored browser state is not
  proven compatible across engines, and no automatic cross-engine migration exists. To
  switch backends, use **Stop & Reset Run**, set `BROWSER_BACKEND`, and create a new
  run.

## Legacy runs

Repository history shows that every pre-Phase-6 run and session was created with
Chrome.
- **Backfill:** the additive migration backfills `browser_backend = 'chrome'` on legacy
  `run_config` and `queue_sessions` rows. Legacy `browser_build` stays NULL (unknown).
- **No inference from defaults:** a legacy row is never interpreted as Camoufox because
  the application default changed.
- **Tests:** `test_browser_backend_provenance_round_trips_and_legacy_defaults_to_chrome`
  covers this on a real pre-Phase-6 schema.

## Browser state compatibility restrictions

- **Same backend only:** storage state is used only when the session provenance equals
  the run backend. Otherwise `BACKEND_MISMATCH` is returned before any context opens,
  and the state file is not read or rewritten (verified byte-identical in both
  directions).
- **Local Prompt 3 matrix:** Camoufox→Camoufox PASS, Chrome→Camoufox PASS,
  Camoufox→Chrome FAIL, Chrome→Chrome PASS. No cross-engine direction is used at
  runtime, because Queue-it cross-engine behavior is UNKNOWN.
- **Nothing extra persisted:** no fingerprint or Camoufox identity data is stored. The
  durable state is the Queue ID, the transfer URL, and same-backend HYBRID storage
  state.

## Chrome fallback

- **To use Chrome for a new run:** set `BROWSER_BACKEND=chrome` and install Chrome with
  `python -m playwright install chrome`, or use an existing Google Chrome.
- **Behavior:** Chrome runs share one headed process for manual windows, allow up to
  `MAX_CONTEXTS_PER_BROWSER` contexts per process, and skip the Camoufox preflight.
- **Coverage:** Chrome passed the controlled workflow (63/63 in Prompt 6), every Prompt 5
  recovery scenario, and the existing Chrome integration tests. It is not dead code.

## Version policy, upgrade, validation, rollback

**Policy:**
- All three Camoufox-side versions are exact pins: the Python package, Playwright, and
  the browser build.
- The application never downloads, updates, or selects a floating "latest" build.
- A build change only happens when an operator deliberately edits the pins and fetches
  the new build.
- Upgrade **between runs**, not during a persisted run.

**Upgrade procedure:**
1. Finish the current run, or use **Stop & Reset Run**.
2. Update `camoufox==` and `playwright==` in `pyproject.toml`, and the constants
   `CAMOUFOX_PACKAGE_VERSION`, `PLAYWRIGHT_VERSION`, and `CAMOUFOX_BROWSER_VERSION` in
   `queue_load_test/browser/backend.py`. Use only combinations upstream declares
   compatible.
3. Run `python -m pip install -e ".[test]"` and `pip check`.
4. Run `camoufox fetch official/stable/<new build>`.
5. **Validate:**
   - `queue-load-test-camoufox-preflight`
   - `python -m pytest`
   - `queue-load-test-phase5-workflow --headed`
   - `queue-load-test-phase5-workflow --backend chrome`
   - `queue-load-test-phase6-camoufox-benchmark --headed --no-capacity`

   Record the results before creating a new run.

If a build is changed while a Camoufox run is persisted anyway, the run keeps working
under Queue ID verification. The change is logged (`run_browser_build_changed`) and
shown in run info. It is never silent.

**Rollback procedure:**
1. Revert the pins (git revert of the upgrade commit).
2. Reinstall dependencies.
3. Run `camoufox fetch official/stable/152.0.4-beta.30`. `camoufox list` shows installed
   builds, and an older build can be kept installed alongside the new one.
4. Run the preflight.

A run created on the rolled-back build matches its recorded build again. To abandon
Camoufox entirely, use Stop & Reset Run and create a new run with
`BROWSER_BACKEND=chrome`.

## Validation (Prompt 6)

| Check | Result |
|---|---|
| Camoufox controlled workflow, visible headed manual pool (`queue-load-test-phase5-workflow --headed`, now default backend) | **63/63 PASS**: setup, acquisition, park, reopen/repeated monitoring, Refresh, Add, Replace, Delete, headed Open/Close, no-ID adoption and duplicate rejection, pause/resume, browser crash (4 processes killed; every row checked again in 2.5 s; 0 identity changes), mismatch preservation, app restart, and Stop & Reset |
| Chrome regression workflow (`--backend chrome`) | **63/63 PASS**, including the same crash step |
| Prompt 5 recovery scenarios re-run after the Prompt 6 changes (`--no-capacity --headed`, both backends) | **Camoufox 11/11 scenarios PASS.** Chrome passed 10/11: its shutdown-under-load precondition (work in flight) failed on a harness race, and all of that scenario's outcome checks passed. After fixing the race (queue operator work while paused, then resume), both backends passed the manual/pause/shutdown group in 2 consecutive runs (29/29 checks each) |
| Full non-staging suite | **505 passed, 4 staging deselected** (final run in `CHANGELOG_AI.md`) |
| Ruff, strict mypy, `pip check` | PASS |
| Staging | **NOT RUN** |

## Remaining UNKNOWNs

- **Staging:** Queue-it transfer, storage fallback, admission, no-ID adoption, and
  throughput with Camoufox against authorised staging.
- **Safe concurrency:** a staging-safe Camoufox concurrency level. Locally it is bounded
  by `CHROME_PROCESS_COUNT` ≤ 4.
- **Cross-engine:** Queue-it cross-engine storage compatibility. It is disabled
  regardless.
- **Download integrity:** whether `camoufox fetch` verifies artifact checksums.
- **Other hosts:** visible headed Camoufox on hosts other than this macOS arm64 machine.
- **Wedge rate:** how often the unresponsive-process restart would trigger in the
  serialized production profile. It never triggered in Prompt 5.
- **Shutdown duration:** the total shutdown time under heavy load can exceed a single
  `SHUTDOWN_TIMEOUT_SECONDS`, because each stage is bounded separately. Camoufox took
  13.8 s against a 10 s setting.
