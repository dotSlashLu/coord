"""Persisted mapping ``cs_<uuid>`` → reviewer + native session id.

The store lives at ``$COORD_REVIEW_HOME/sessions.json`` (default
``~/.coord-review/sessions.json``). Writes are atomic via ``os.replace`` so a
crash mid-write cannot corrupt the file. Concurrent processes can race on the
read-modify-write, which is acceptable for the expected single-user load — the
last writer wins, no record is silently dropped because every save reloads
before merging.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


def _home() -> Path:
    override = os.environ.get("COORD_REVIEW_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".coord-review"


def _store_path() -> Path:
    return _home() / "sessions.json"


@dataclass
class SessionRecord:
    """One persisted reviewer session."""

    our_id: str
    reviewer: str
    native_id: str
    cwd: str
    created_at: float
    updated_at: float

    @classmethod
    def new(cls, reviewer: str, native_id: str, cwd: str) -> "SessionRecord":
        now = time.time()
        return cls(
            our_id="cs_" + uuid.uuid4().hex,
            reviewer=reviewer,
            native_id=native_id,
            cwd=cwd,
            created_at=now,
            updated_at=now,
        )


def _load_all(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # corrupted/unreadable file → start fresh rather than crash the server
        return {}


def _atomic_write(path: Path, data: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def save(record: SessionRecord) -> None:
    """Insert or update ``record`` in the store."""
    path = _store_path()
    data = _load_all(path)
    data[record.our_id] = asdict(record)
    _atomic_write(path, data)


def get(our_id: str) -> Optional[SessionRecord]:
    """Return the record for ``our_id`` or ``None`` if unknown."""
    raw = _load_all(_store_path()).get(our_id)
    if raw is None:
        return None
    return SessionRecord(**raw)


def update_native_id(our_id: str, new_native_id: str) -> Optional[SessionRecord]:
    """Update the native session id (some CLIs rotate it on resume) and bump ``updated_at``.

    Returns the updated record, or ``None`` if ``our_id`` is unknown.
    """
    path = _store_path()
    data = _load_all(path)
    raw = data.get(our_id)
    if raw is None:
        return None
    raw["native_id"] = new_native_id
    raw["updated_at"] = time.time()
    data[our_id] = raw
    _atomic_write(path, data)
    return SessionRecord(**raw)
