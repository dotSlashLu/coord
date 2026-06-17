"""Tests for the nested-review recursion guard (env depth sentinel)."""
import pytest

from coord_review import subprocess_util as su
from coord_review import server


@pytest.fixture
def top_level(monkeypatch):
    """Simulate the top-level coord-review server: depth 0, max 1."""
    monkeypatch.delenv(su._DEPTH_ENV, raising=False)
    monkeypatch.setattr(server, "_MAX_DEPTH", 1)


@pytest.fixture
def nested(monkeypatch):
    """Simulate a server running *inside* a reviewer subprocess: depth 1."""
    monkeypatch.setenv(su._DEPTH_ENV, "1")
    monkeypatch.setattr(server, "_MAX_DEPTH", 1)


def test_top_level_allowed(top_level):
    server._refuse_if_nested()  # must not raise


def test_nested_refused(nested):
    with pytest.raises(RuntimeError, match="refused to launch a reviewer"):
        server._refuse_if_nested()


def test_nested_allowed_when_max_depth_raised(monkeypatch):
    monkeypatch.setenv(su._DEPTH_ENV, "1")
    monkeypatch.setattr(server, "_MAX_DEPTH", 2)
    server._refuse_if_nested()  # depth 1 < max 2 -> allowed


def test_malformed_depth_env_is_treated_as_zero(monkeypatch):
    monkeypatch.setenv(su._DEPTH_ENV, "not-a-number")
    monkeypatch.setattr(server, "_MAX_DEPTH", 1)
    server._refuse_if_nested()  # treated as depth 0 -> allowed
