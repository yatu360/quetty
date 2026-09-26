# Queue Load Test

Phase 1 foundation for an authorised Queue-it staging test system.

This phase intentionally implements only:

- typed configuration and startup validation
- Queue-it session/progress domain models and lifecycle validation
- defensive, browser-independent Queue-it progress parsing
- SQLite session persistence with lightweight work leases
- atomic local JSON browser-state persistence
- project structure for future browser, scheduler, transfer, and metrics code
- unit tests for configuration, domain behavior, parsing, and persistence

It does not implement browser automation, queue polling, scheduling, transfer
logic, PostgreSQL, or metrics.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m playwright install chrome
```

The browser implementation in a later phase must use Playwright's async API
and launch the installed Google Chrome build with `channel="chrome"`. Phase 1
does not launch a browser.

The application is configured through environment variables. Start from:

```powershell
Copy-Item .env.example .env
```

The SQLite database, transfer URLs, and browser-state files contain sensitive
session data. The default local files are git-ignored and should not be logged
or shared.

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
- SQLite via `DATABASE_URL`
- local browser state files in `STATE_DIRECTORY`
- browser-based Queue-it monitoring will be added in a later phase
