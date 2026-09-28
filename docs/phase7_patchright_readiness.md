# Phase 7 Prompt 1 — Patchright Readiness

**Date:** 2026-09-28  
**Decision:** **PATCHRIGHT_READY_FOR_INTEGRATION**  
**Scope:** dependency and local lifecycle readiness only; runtime integration is not
implemented. Queue-it staging is **NOT RUN / UNKNOWN**.

## Policy and boundary

- Standard Chrome is the Phase 7 control/fallback and temporary default for **new**
  runs (`BROWSER_BACKEND=chrome`).
- Camoufox remains fully present as a dormant/experimental backend. Its dependency,
  parser value, backend, provenance, migrations, preflight, tests, and code paths were
  not removed. Phase 7 will not spend benchmark/acceptance effort on it.
- Existing persisted runs still restart with `run_config.browser_backend`. Legacy rows
  still migrate to Chrome. No row is migrated to Patchright, and switching backend
  still requires Stop & Reset Run.
- Patchright is not a runtime backend in Prompt 1. Adding it to the backend enum,
  persistence, manager factory, setup, and runtime belongs to Prompt 2.
- Queue ID remains authoritative and the bounded shared-process/disposable-context
  architecture remains unchanged.

## Authoritative upstream findings

Sources reviewed on 2026-09-28:

- [Patchright Python repository and usage](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
- [Patchright Python 1.63.0 on PyPI](https://pypi.org/project/patchright/1.63.0/)
- [Patchright core repository](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
- [Python package patch/build script](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python/blob/main/patch_python_package.py)

Exact decisions:

| Item | Decision / evidence |
|---|---|
| Patchright package | Pin `patchright==1.63.0`, the current release on the review date. PyPI provenance ties it to upstream commit `c4064f5833c811d99772aabefee8984c51322ace`. |
| Python | Upstream metadata requires `>=3.10` and classifies 3.10–3.14. Quetty remains `>=3.12`; local probe used Python 3.14.7. |
| API family | Patchright 1.63.0 is generated from Playwright Python 1.63.0 and exposes a Playwright-compatible sync/async API under `patchright.*`. Quetty's probe uses only `patchright.async_api`. |
| Existing Playwright | Keep `playwright==1.62.0`, required by retained Camoufox 0.5.6 (`playwright<1.63`). Patchright does not depend on the `playwright` distribution; it ships its separate namespace/driver. |
| Shared transitive pins | Patchright requires `pyee>=13,<14` and `greenlet>=3.1.1,<4.0.0`; installed `pyee==13.0.1` and `greenlet==3.5.6` satisfy both Playwright and Patchright. |
| Browser family | Patchright patches Chromium-based browsers only. Firefox and WebKit are not supported. |
| Intended browser | Installed Google Chrome through `chromium.launch(channel="chrome")`, matching upstream's recommendation to use Chrome. Local observed version: `153.0.8010.54`. |
| Browser install | The current installed Chrome can be launched directly. `patchright install chrome` is an optional explicit installation route, not a runtime prerequisite when Chrome is already installed. |
| Managed Chromium | `patchright install chromium --dry-run` identifies Chrome for Testing `153.0.8010.12`, Patchright Chromium revision `1243`, plus FFmpeg/headless-shell artifacts. Quetty does not need or install these for the selected `channel="chrome"` path. |
| Downloads | Installing the Python wheel supplies the Patchright driver. Browser download/install is separate and explicit. The preflight and future runtime must never invoke an install command. |
| Camoufox coexistence | Resolver dry run and real editable install succeed with exact pins `patchright==1.63.0`, `camoufox==0.5.6`, and `playwright==1.62.0`; `pip check` reports no broken requirements. No resolver constraint was bypassed. |

Patchright's API version need not equal the retained Playwright distribution version:
they are separate packages, imports, and bundled drivers. Runtime integration must not
pass a `playwright.async_api.Playwright`/`Browser` object into Patchright or vice versa;
the backend should own types from its corresponding namespace.

## Persistent-context recommendation

Upstream's “Best Practice” example uses `launch_persistent_context`, a user-data
directory, Google Chrome, headed mode, and no viewport override. This is a recommendation
for its preferred browser presentation, not a statement that ordinary `launch()` plus
`browser.new_context()` is unsupported.

Quetty intentionally parks sessions by saving transferable/state identity and closing
the BrowserContext. Prompt 1 therefore did not redesign the system around persistent
profiles. The technical lifecycle tested was:

```text
one Patchright-controlled installed-Chrome process
  -> create isolated temporary BrowserContext
  -> create page and navigate to an in-memory data: URL
  -> close page and context
  -> repeat with a fresh context
  -> close browser and Patchright driver
```

Result: **technically viable locally**. Twenty consecutive cycles completed; after each
close `browser.contexts` was empty. Browser close reported disconnected, the Patchright
driver stopped, and an OS-process baseline/final comparison found no new Chrome process
using `--remote-debugging-pipe`. This proves basic temporary-context lifecycle only. It
does **not** prove Queue identity restoration, storage-state fidelity, fingerprint
continuity, or staging behavior; those belong to Prompt 3 and later acceptance work.

## Local preflight

`queue-load-test-patchright-preflight`:

- verifies exact `patchright==1.63.0` metadata;
- uses the async Python API;
- launches installed Chrome with `channel="chrome"` and never calls an installer;
- creates fresh context/page pairs and navigates only to a `data:` URL;
- closes every page/context, the browser, and Patchright driver;
- requires zero active contexts and zero tracked managed processes after shutdown;
- supports `--context-cycles` and human/JSON output.

It is deliberately not wired into application setup yet because Patchright is not a
runtime backend in Prompt 1.

## Evidence and readiness decision

Host: macOS 26.5.1 arm64, Apple M5 Pro. Exact locally observed versions:

- Python 3.14.7;
- Patchright 1.63.0;
- Playwright 1.62.0;
- Camoufox 0.5.6;
- pyee 13.0.1;
- greenlet 3.5.6;
- Google Chrome 153.0.8010.54.

The decision is **PATCHRIGHT_READY_FOR_INTEGRATION** because exact compatible pins
resolve cleanly, installed Chrome launches without a managed browser download, the async
API supports the required object lifecycle, and repeated disposable contexts clean up
locally. This is not final acceptance and makes no anti-detection, Queue-it, or staging
claim.

## Validation

The completed command/result ledger is recorded in `CHANGELOG_AI.md`. Required staging
tests were deliberately not run: this prompt sent no Queue-it traffic. Staging remains
**NOT RUN / UNKNOWN**.

## Next task

**Phase 7 Prompt 2 — Patchright runtime integration.**
