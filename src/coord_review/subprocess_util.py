"""Async subprocess helper shared by every reviewer adapter.

Streams the child's stdout and stderr line-by-line back to the calling MCP
client through ``ctx.info`` so a coder agent can watch the reviewer think,
not just receive a final blob. Enforces a wall-clock timeout; on timeout the
whole process group is signalled (SIGTERM, then SIGKILL) so grandchildren
spawned by the reviewer CLI (node, language servers, etc.) don't survive
as orphans.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass
from typing import Optional, Sequence


# Per-call timeout, seconds. Reviewer runs are long; the default of 10 minutes
# is generous enough for most repos, capped enough to fail visibly when the
# reviewer is wedged.
DEFAULT_TIMEOUT_SEC = float(os.environ.get("COORD_REVIEW_TIMEOUT", "600"))

# Recursion-depth sentinel. Every reviewer subprocess we spawn inherits an
# incremented value of this env var. The coord-review server started inside
# that subprocess (codex/cursor pull their MCP servers into the child agent)
# therefore sees a non-zero depth, and the tool handlers refuse to spawn yet
# another reviewer. This is the structural backstop against unbounded nesting:
# a coder agent reviewing its own work may, "reasonably", call review_repo
# again — without this guard codex1 spawns codex2 spawns codex3 ... and we get
# a fork bomb. Tool-description hints discourage the top-level agent from
# fanning out, but only this env check actually stops the recursion.
_DEPTH_ENV = "COORD_REVIEW_DEPTH"


def current_depth() -> int:
    """Depth of *this* process in the reviewer-spawn tree. 0 at the top level."""
    try:
        return max(0, int(os.environ.get(_DEPTH_ENV, "0")))
    except (TypeError, ValueError):
        return 0


def _child_env() -> dict[str, str]:
    """``os.environ`` copy with the depth sentinel incremented for the child."""
    env = dict(os.environ)
    env[_DEPTH_ENV] = str(current_depth() + 1)
    return env


class ReviewerNotFoundError(RuntimeError):
    """Raised when a reviewer's CLI binary isn't on PATH."""


class ReviewerTimeoutError(RuntimeError):
    """Raised when the reviewer exceeded the configured wall-clock timeout."""


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def require_binary(*names: str) -> str:
    """Resolve the first of ``names`` found on PATH, or raise ``ReviewerNotFoundError``.

    Accepts multiple candidate names so an adapter can prefer one canonical
    name (e.g. Cursor's ``agent``) but fall back to a legacy alias
    (``cursor-agent``) if the canonical one isn't on PATH.
    """
    for name in names:
        path = shutil.which(name)
        if path is not None:
            return path
    pretty = " / ".join(repr(n) for n in names)
    raise ReviewerNotFoundError(
        f"required CLI {pretty} not found on PATH; install it before using this reviewer"
    )


async def _drain(stream: asyncio.StreamReader, sink: list[str], log) -> None:
    """Read lines from ``stream``, append to ``sink``, forward each line via ``log`` (sync callable)."""
    while True:
        raw = await stream.readline()
        if not raw:
            return
        try:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
        except Exception:
            line = repr(raw)
        sink.append(line)
        if log is not None:
            try:
                log(line)
            except Exception:
                # never let a logging hiccup kill the read loop
                pass


async def stream_subprocess(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    stdin_text: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    log_line=None,
) -> ProcResult:
    """Run ``argv``, stream output via ``log_line``, return collected output.

    ``log_line`` is a synchronous callable taking one string. Adapters typically
    pass a small wrapper that schedules ``ctx.info(line)`` on the event loop.
    Keeping it synchronous lets the drain coroutine stay simple and avoids
    backpressure between two awaits per line.
    """
    stdin = (
        asyncio.subprocess.PIPE
        if stdin_text is not None
        else asyncio.subprocess.DEVNULL
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Put the reviewer in its own process group so we can signal the
        # whole tree on timeout. Reviewers (claude, cursor-agent) spawn
        # node/LSP children; killing only the direct child orphans those.
        start_new_session=True,
        # Inherit a copy of the environment with the recursion-depth sentinel
        # incremented. The reviewer CLI (and any coord-review server it starts
        # inside the child agent) sees this and refuses to nest further. See
        # _DEPTH_ENV / current_depth() for the rationale.
        env=_child_env(),
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    drains = asyncio.gather(
        _drain(proc.stdout, stdout_lines, None),  # stdout is the result; don't echo
        _drain(proc.stderr, stderr_lines, log_line),  # stderr is progress; do echo
    )

    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
        finally:
            proc.stdin.close()

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _terminate_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _terminate_group(proc.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    # let drain coroutines finish reading whatever is left in the pipes
    try:
        await asyncio.wait_for(drains, timeout=5)
    except asyncio.TimeoutError:
        drains.cancel()

    return ProcResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
        timed_out=timed_out,
    )


def _terminate_group(pid: int, sig: int) -> None:
    """Best-effort signal of the process group led by ``pid``.

    The reviewer was spawned with ``start_new_session=True``, so its pid is
    also the pgid. ``ProcessLookupError`` means the leader already exited;
    other ``OSError`` (e.g. ``EPERM`` if the OS doesn't honor pgid signalling
    in a sandboxed runtime) is swallowed because there's no useful recovery
    here — the wait_for will fall through and we'll report what we have.
    """
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        # Fall back to direct-process kill; better than nothing.
        try:
            os.kill(pid, sig)
        except OSError:
            pass
