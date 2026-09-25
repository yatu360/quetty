# Queue Load Test

Phase 1 foundation for an authorised Queue-it staging test system.

This phase intentionally implements only:

- typed configuration and startup validation
- core Queue-it session/progress domain models
- project structure for future browser, scheduler, repository, state, monitoring, transfer, metrics, and utility code
- unit tests for configuration and model behavior

It does not implement browser automation, queue polling, persistence, scheduling, transfer logic, or metrics.

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
