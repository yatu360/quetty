# Queue Load Test

Phase 1 foundation for an authorised Queue-it staging test system.

This phase intentionally implements only:

- typed configuration and startup validation
- Queue-it session/progress domain models and lifecycle validation
- defensive, browser-independent Queue-it progress parsing
- SQLite session persistence with lightweight work leases
- atomic local JSON browser-state persistence
- shared Google Chrome process and isolated BrowserContext resource management
- live, defensive Queue-it DOM state extraction after JavaScript execution
- supported Queue-it transfer-link capture with explicit identity-mismatch handling
- bounded session creation until the configured unique Queue ID target is reached
- identity-safe TRANSFER_ONLY restoration with HYBRID storage-state fallback
- bounded parked-session monitoring with SQLite leases and adaptive polling
- browser-verified admission, terminal failure classification, and bounded recovery
- signal-aware graceful shutdown that preserves persisted journeys
- project structure for future scheduler, transfer, and metrics code
- unit tests for configuration, domain behavior, parsing, and persistence

It does not implement PostgreSQL, Prometheus export, or later post-admission
workflows.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m playwright install chrome
```

The browser manager uses Playwright's async API to launch the installed Google
Chrome build with `channel="chrome"`. It shares each Chrome process across
isolated browser contexts and does not implement stealth or automation hiding.

The application is configured through environment variables. Start from:

```powershell
Copy-Item .env.example .env
```

The SQLite database, transfer URLs, and browser-state files contain sensitive
session data. The default local files are git-ignored and should not be logged
or shared. Queue IDs and transfer URLs are also excluded from model result
representations.

HYBRID restoration prefers the official Queue-it transfer URL and supplies saved
Playwright storage state only as a fallback. Storage state is not a complete
browser snapshot: it does not preserve JavaScript memory, timers, WebSockets,
the execution stack, every form of session storage, every browser store, or live
service-worker state.

`TURN_STARTED` records Queue-it entering its service/redirect stage. A session
is only marked `ADMITTED` after normal browser navigation reaches the configured
staging destination. The application does not extract or manipulate Queue-it
admission tokens.

## Test

```powershell
python -m pytest
```

## Initial Phase 1 Defaults

- `TARGET_QUEUE_IDS=10`
- `SESSION_MODE=HYBRID`
- `CHROME_PROCESS_COUNT=1`
- `MAX_CONTEXTS_PER_BROWSER=5`
- `MAX_ACTIVE_CONTEXTS=5`
- `QUEUE_POLL_SECONDS=30`
- adaptive pre-queue, active-queue, serviced-soon, and turn-started intervals
- SQLite via `DATABASE_URL`
- local browser state files in `STATE_DIRECTORY`
- browser-based Queue-it extraction and bounded polling orchestration
