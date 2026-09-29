"""Adopt a direct recipe only from a newly observed legitimate browser request.

The Prompt 2 observer records a protected artifact whenever a Direct run's browser
fallback inspects a visitor page. This harvester reads only artifacts written for
the same session after that fallback began, and selects an exchange the browser
itself made whose captured response parses through the reviewed schema with the
persisted Queue ID. Nothing is constructed, guessed, or copied across sessions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from queue_load_test.direct_replay import ReplayEvidenceError, ReplayRecipe, load_replay_recipe
from queue_load_test.observation_equivalence import DirectObservationError, DirectResponseParser

AUTHORIZED_STAGING_SCOPE = "authorized_queue_it_staging"
LOCAL_SIMULATOR_SCOPE = "local_simulator"
_REPLAYABLE = frozenset(
    {"json_status_candidate", "periodic_status_candidate", "dom_correlated_status_candidate"}
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").casefold() in _LOOPBACK_HOSTS


def accepted_evidence_scopes(target_url: str) -> frozenset[str]:
    """Scopes whose evidence may drive direct monitoring for this run target.

    Authorised Queue-it staging evidence is always required for a real target.
    Local-simulator evidence is accepted only when the run itself targets a
    loopback simulator, so it can never enable direct requests to a real event.
    """

    if is_loopback_url(target_url):
        return frozenset({AUTHORIZED_STAGING_SCOPE, LOCAL_SIMULATOR_SCOPE})
    return frozenset({AUTHORIZED_STAGING_SCOPE})


class DiscoveryRecipeHarvester:
    """Select one exact browser-observed status request for a session."""

    def __init__(
        self,
        *,
        evidence_directory: Path,
        parser: DirectResponseParser,
        accepted_scopes: Collection[str],
    ) -> None:
        if not accepted_scopes:
            raise ValueError("at least one evidence scope must be accepted")
        self._directory = evidence_directory
        self._parser = parser
        self._scopes = frozenset(accepted_scopes)

    async def harvest(
        self,
        *,
        session_id: str,
        expected_queue_id: str,
        observed_since: float,
    ) -> ReplayRecipe | None:
        return await asyncio.to_thread(
            self._harvest, session_id, expected_queue_id, observed_since
        )

    def _harvest(
        self, session_id: str, expected_queue_id: str, observed_since: float
    ) -> ReplayRecipe | None:
        for path in self._artifacts(session_id, observed_since):
            recipe = self._from_artifact(path, session_id, expected_queue_id)
            if recipe is not None:
                return recipe
        return None

    async def prune(self, *, session_id: str, keep: int) -> int:
        """Bound sensitive evidence per session, keeping the newest ``keep`` artifacts."""

        if keep < 1:
            return 0
        return await asyncio.to_thread(self._prune, session_id, keep)

    def _prune(self, session_id: str, keep: int) -> int:
        removed = 0
        for path in self._artifacts(session_id, 0.0)[keep:]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
        return removed

    def _artifacts(self, session_id: str, observed_since: float) -> list[Path]:
        if not self._directory.is_dir():
            return []
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in session_id
        )[:100]
        paths: list[Path] = []
        for path in self._directory.glob(f"*-{safe}.json"):
            try:
                if path.is_file() and path.stat().st_mtime >= observed_since:
                    paths.append(path)
            except OSError:
                continue
        # Artifact names start with a UTC timestamp, so the newest capture is last.
        return sorted(paths, reverse=True)

    def _from_artifact(
        self, path: Path, session_id: str, expected_queue_id: str
    ) -> ReplayRecipe | None:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(artifact, Mapping) or artifact.get("scope") not in self._scopes:
            return None
        exchanges = artifact.get("exchanges")
        if not isinstance(exchanges, list):
            return None
        candidates = sorted(
            (
                item
                for item in exchanges
                if isinstance(item, Mapping)
                and item.get("classification") in _REPLAYABLE
                and isinstance(item.get("sequence"), int)
                and isinstance(item.get("response_status"), int)
                and 200 <= int(item["response_status"]) < 300
                and item.get("response_json") is not None
                and item.get("request_body_truncated") is not True
                and item.get("response_body_truncated") is not True
            ),
            key=lambda item: int(item["sequence"]),
            reverse=True,
        )
        for exchange in candidates:
            try:
                recipe = load_replay_recipe(
                    path,
                    session_id=session_id,
                    expected_queue_id=expected_queue_id,
                    exchange_sequence=int(exchange["sequence"]),
                    require_authorized_staging=False,
                )
            except ReplayEvidenceError:
                continue
            if recipe.source_scope not in self._scopes:
                continue
            if recipe.source_scope == LOCAL_SIMULATOR_SCOPE and not is_loopback_url(recipe.url):
                continue
            try:
                # The browser's own captured response must already satisfy the
                # reviewed schema with the persisted identity.
                self._parser.parse(
                    exchange["response_json"],
                    session_id=session_id,
                    expected_queue_id=expected_queue_id,
                )
            except DirectObservationError:
                continue
            return recipe
        return None
