import asyncio
import sys

from coord_review.subprocess_util import stream_subprocess


def test_subprocess_stdin_defaults_to_eof():
    async def run():
        return await stream_subprocess(
            [
                sys.executable,
                "-c",
                "import sys; print(repr(sys.stdin.read()))",
            ],
            timeout=5,
        )

    result = asyncio.run(run())
    assert result.returncode == 0
    assert result.stdout == "''"
    assert not result.timed_out


def test_subprocess_inherits_incremented_depth(monkeypatch):
    """The child env must carry COORD_REVIEW_DEPTH = parent_depth + 1."""
    from coord_review import subprocess_util as su

    monkeypatch.delenv(su._DEPTH_ENV, raising=False)
    assert su.current_depth() == 0
    assert su._child_env()[su._DEPTH_ENV] == "1"

    monkeypatch.setenv(su._DEPTH_ENV, "2")
    assert su.current_depth() == 2
    assert su._child_env()[su._DEPTH_ENV] == "3"


def test_subprocess_child_sees_incremented_depth():
    """End-to-end: a spawned subprocess observes the incremented sentinel."""
    import asyncio
    import sys

    from coord_review import subprocess_util as su

    async def run():
        return await su.stream_subprocess(
            [
                sys.executable,
                "-c",
                f"import os; print(os.environ.get({su._DEPTH_ENV!r}))",
            ],
            timeout=5,
        )

    # Parent depth 0 -> child should see "1".
    result = asyncio.run(run())
    assert result.returncode == 0
    assert result.stdout.strip() == "1"
