# Repository Guidelines

## Project Structure & Module Organization

This Python 3.12 Queue-it staging-test system keeps production code in
`src/queue_load_test/`. Key areas are `browser/`, `queue_monitor/`,
`scheduler/`, `repository/`, `state/`, and gated `harness/` entry points. Keep
domain types in `models/` and settings in `config.py`. Tests mirror the code in
`tests/unit/`, `tests/integration/`, and opt-in `tests/staging/`; HTML fixtures
are in `tests/fixtures/`. Put operator docs in `docs/` and benchmark inputs in
`benchmarks/`.

## Build, Test, and Development Commands

Install test dependencies with `python -m pip install -e ".[test]"`; add the
`benchmark` extra for resource work. Install Chrome with
`python -m playwright install chrome`.

- `python -m pytest` runs the normal suite (staging tests are excluded).
- `python -m pytest -o addopts="" -m staging tests/staging` runs authorised
  staging tests; set the required gates and confirmation flags first.
- `ruff check src tests` checks lint rules, and `mypy src` enforces strict
  typing.
- `queue-load-test` runs the main application after `.env` is configured.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, and a 100-character line limit.
Ruff targets Python 3.12; mypy is strict. Use `snake_case` for modules,
functions, variables, and tests; `PascalCase` for classes; and
`UPPER_SNAKE_CASE` for environment aliases. Preserve async Playwright patterns.

## Testing Guidelines

Name tests `test_<behavior>.py` in the matching layer. Cover new parsing,
lifecycle, configuration, and persistence branches; prefer fixtures to live
browser traffic. Mark authorised-environment tests with `@pytest.mark.staging`
and gate them. Never commit Queue IDs, transfer URLs, browser state, SQLite
data, or sensitive reports.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects, often scoped by phase (for
example, `Add Phase 2 concurrency tuning harness`). Keep commits focused. PRs
should describe the change and validation, link relevant issues or phase docs,
and include sanitized evidence when useful. Call out configuration, schema, and
staging-gate changes.

## Configuration & Safety

Copy `.env.example` to `.env`; do not commit it. Treat SQLite data,
`.browser-state/`, transfer URLs, and benchmark reports as sensitive. Run
staging and benchmark harnesses only against authorised targets with their
documented gates.

## Agent Workflow

After each completed implementation or prompt, commit and push the resulting
changes. Use a concise, single-line commit subject and do not add AI authorship,
co-author, or tool-attribution trailers.
