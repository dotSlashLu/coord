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
