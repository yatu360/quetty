"""Protected persistence for response-refreshed experimental replay cookies."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import cast

from queue_load_test.direct_replay.models import ReplayCookie

_FORMAT = "queue-load-test.phase8-direct-replay-state"
_VERSION = 1


class ReplayStateError(RuntimeError):
    """Protected replay state is corrupt, mismatched, or unreadable."""


class ProtectedReplayStateStore:
    """Atomic, session-bound store kept outside normal application persistence."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, session_id: str) -> Path:
        safe = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in session_id
        )[:120]
        if not safe or safe in {".", ".."}:
            raise ReplayStateError("session ID cannot form a protected replay-state path")
        return self._directory / f"{safe}.json"

    async def save(
        self,
        *,
        session_id: str,
        recipe_fingerprint: str,
        cookies: tuple[ReplayCookie, ...],
    ) -> Path:
        payload = {
            "format": _FORMAT,
            "version": _VERSION,
            "session_id": session_id,
            "recipe_fingerprint": recipe_fingerprint,
            "cookies": [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": cookie.secure,
                    "expires": cookie.expires,
                }
                for cookie in cookies
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        document = {**payload, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}
        path = self.path_for(session_id)
        await asyncio.to_thread(write_protected_json, path, document)
        return path

    async def delete(self, session_id: str) -> bool:
        path = self.path_for(session_id)
        return await asyncio.to_thread(_unlink, path)

    async def load(
        self, *, session_id: str, recipe_fingerprint: str
    ) -> tuple[ReplayCookie, ...] | None:
        path = self.path_for(session_id)
        try:
            document = await asyncio.to_thread(read_protected_json, path)
        except FileNotFoundError:
            return None
        if (
            document.get("format") != _FORMAT
            or document.get("version") != _VERSION
            or document.get("session_id") != session_id
            or document.get("recipe_fingerprint") != recipe_fingerprint
        ):
            raise ReplayStateError("protected replay state does not match this session recipe")
        digest = document.pop("sha256", None)
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
        if digest != hashlib.sha256(canonical.encode()).hexdigest():
            raise ReplayStateError("protected replay state failed its integrity check")
        raw_cookies = document.get("cookies")
        if not isinstance(raw_cookies, list):
            raise ReplayStateError("protected replay state has no cookie list")
        cookies: list[ReplayCookie] = []
        try:
            for raw in raw_cookies:
                if not isinstance(raw, dict):
                    raise TypeError
                name = raw["name"]
                value = raw["value"]
                domain = raw["domain"]
                path = raw["path"]
                secure = raw["secure"]
                expires = raw["expires"]
                if not all(isinstance(item, str) for item in (name, value, domain, path)):
                    raise TypeError
                if not isinstance(secure, bool) or not (
                    expires is None or isinstance(expires, int)
                ):
                    raise TypeError
                cookies.append(
                    ReplayCookie(
                        name=cast(str, name),
                        value=cast(str, value),
                        domain=cast(str, domain),
                        path=cast(str, path),
                        secure=secure,
                        expires=expires,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayStateError("protected replay state contains an invalid cookie") from exc
        return tuple(cookies)


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def read_protected_json(path: Path) -> dict[str, object]:
    """Read one protected JSON object; a missing file raises ``FileNotFoundError``."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ReplayStateError("protected replay state is unreadable") from exc
    try:
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayStateError("protected replay state is unreadable") from exc
    if not isinstance(value, dict):
        raise ReplayStateError("protected replay state is not an object")
    return cast(dict[str, object], value)


def write_protected_json(path: Path, document: dict[str, object]) -> None:
    """Atomically write mode-0600 JSON beneath a mode-0700 ignore-all directory."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    marker = path.parent / ".gitignore"
    if not marker.exists():
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("*\n")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".replay-", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
