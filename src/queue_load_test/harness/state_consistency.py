"""Operator-facing read-only browser-state consistency report."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.state import FileSystemStateStore, StateConsistencyChecker


async def run_consistency_check(database: Path, state_directory: Path) -> dict[str, object]:
    if not database.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {database}")
    repository = SQLiteSessionRepository(database)
    try:
        await repository.initialize()
        report = await StateConsistencyChecker(
            repository,
            FileSystemStateStore(state_directory),
        ).check()
        return report.to_dict()
    finally:
        await repository.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report SQLite/browser-state inconsistencies without changing data"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(run_consistency_check(args.database, args.state_directory))
    output = json.dumps(report, indent=2) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
