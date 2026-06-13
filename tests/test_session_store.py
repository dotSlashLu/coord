import os
import tempfile
from pathlib import Path

import pytest

from coord_review import session_store


@pytest.fixture
def isolated_home(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("COORD_REVIEW_HOME", d)
        yield Path(d)


def test_save_and_get_round_trip(isolated_home):
    rec = session_store.SessionRecord.new(
        reviewer="claude", native_id="native-123", cwd="/tmp/x"
    )
    session_store.save(rec)
    fetched = session_store.get(rec.our_id)
    assert fetched is not None
    assert fetched.our_id == rec.our_id
    assert fetched.reviewer == "claude"
    assert fetched.native_id == "native-123"
    assert fetched.cwd == "/tmp/x"
    assert fetched.created_at == rec.created_at


def test_get_returns_none_for_unknown_id(isolated_home):
    assert session_store.get("cs_does-not-exist") is None


def test_update_native_id_changes_value_and_bumps_updated_at(isolated_home):
    rec = session_store.SessionRecord.new(
        reviewer="cursor", native_id="old-uuid", cwd="/tmp/y"
    )
    session_store.save(rec)
    updated = session_store.update_native_id(rec.our_id, "new-uuid")
    assert updated is not None
    assert updated.native_id == "new-uuid"
    assert updated.updated_at >= rec.updated_at
    # persisted on disk too
    again = session_store.get(rec.our_id)
    assert again is not None
    assert again.native_id == "new-uuid"


def test_update_unknown_id_returns_none(isolated_home):
    assert session_store.update_native_id("cs_missing", "whatever") is None


def test_corrupted_store_does_not_crash(isolated_home):
    path = isolated_home / "sessions.json"
    path.write_text("this is not json", encoding="utf-8")
    # load path is implicit in get(); should swallow the parse error
    assert session_store.get("cs_anything") is None
    # next save should overwrite cleanly
    rec = session_store.SessionRecord.new(reviewer="claude", native_id="n", cwd="/tmp")
    session_store.save(rec)
    assert session_store.get(rec.our_id) is not None


def test_atomic_write_leaves_no_tmp_file(isolated_home):
    rec = session_store.SessionRecord.new(reviewer="claude", native_id="n", cwd="/tmp")
    session_store.save(rec)
    leftovers = [p.name for p in isolated_home.iterdir() if ".tmp." in p.name]
    assert leftovers == []
