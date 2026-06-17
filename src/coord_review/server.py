"""FastMCP server exposing review_repo / review_file / ask_reviewer tools."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Annotated, Callable, Literal

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from coord_review import session_store
from coord_review.reviewers import get_reviewer
from coord_review.reviewers.base import ReviewResult
from coord_review.subprocess_util import (
    ReviewerNotFoundError,
    current_depth,
)


mcp = FastMCP("coord-review")


ReviewerName = Literal["claude", "codex", "cursor"]


# Reused across review_repo / review_file. The "ask the user" sentence is
# load-bearing: a coding agent reviewing its own work has obvious blind spots
# (it picked the design, so it tends to ask the reviewer to confirm choices it
# already made). Surfacing user-known concerns up front gives the reviewer a
# fighting chance to find issues the coder isn't thinking about.
_BRIEF_DESCRIPTION = (
    "Free-form review request that becomes the reviewer's whole prompt. "
    "IMPORTANT: if you (the calling agent) wrote this code yourself, your "
    "sense of what to review is biased toward what you already considered. "
    "Before composing the brief, ask the user what THEY are worried about "
    "or want double-checked — domain rules, regressions in code you didn't "
    "touch, recent incidents, deployment constraints, anything you might "
    "be missing — and weave their answers in. Skip the question only if "
    "the user gave explicit guidance in this turn or the change is so "
    "trivial that asking would be noise. "
    "Then cover: (1) the purpose of the change and what success looks like; "
    "(2) the scope — which files / subsystems matter; (3) specific focus "
    "areas (correctness, perf, security, API stability, threading, etc.). "
    "The reviewer reads files itself; this brief is the contract."
)
_REVIEWER_DESCRIPTION = (
    'Which reviewer CLI to invoke. ASK THE USER which one to use. '
)
_QUESTION_DESCRIPTION = (
    "Follow-up question for the reviewer. Use this when a finding is "
    "unclear, when you need the reviewer to look at something it skipped, "
    "or when you want to push back on a specific issue before deciding "
    "whether to act on it. The reviewer keeps full prior context — refer "
    "to earlier findings by file/line, not by quoting them back."
)


# Bound the in-flight progress log buffer. A verbose reviewer can emit
# thousands of stderr lines; without a bound the queue grows until the MCP
# transport drains, and tool replies can land before all the progress
# notifications. 256 is enough headroom for normal bursts; once full we drop
# the oldest line rather than block the subprocess drain.
_LOG_QUEUE_MAX = 256

# Heartbeat interval, seconds. While a reviewer subprocess is running the pump
# emits a progress notification + info message at this cadence so clients with
# idle timeouts don't kill the connection during quiet periods.  Set to 0 to
# disable heartbeat entirely.
_HEARTBEAT_SEC = max(0.0, float(os.environ.get("COORD_REVIEW_HEARTBEAT_SEC", "30")))

# Maximum reviewer-spawn depth. 1 (the default) means only the top-level
# coord-review server may launch a reviewer; any server started *inside* a
# reviewer subprocess (depth >= 1) refuses. Set higher to allow controlled
# nesting, but beware the fork-bomb risk — see _DEPTH_ENV in subprocess_util.
_MAX_DEPTH = max(1, int(os.environ.get("COORD_REVIEW_MAX_DEPTH", "1")))

# Hint appended to each tool description: discourages the top-level agent from
# proactively fanning out review (and from calling these tools inside a review
# sub-flow). This is a soft nudge only — the hard backstop is the depth check
# in _refuse_if_nested().
_NESTING_HINT = (
    "Only call this when the user has explicitly asked to use coord-review to "
    "review code. Do NOT proactively invoke it without an explicit user "
    "request, and never call it from inside a review sub-flow — that nests "
    "reviewers and is blocked regardless."
)


def _refuse_if_nested() -> None:
    """Raise if this server is itself running inside a reviewer subprocess.

    The structural backstop against recursive review (codex1 -> codex2 -> ...).
    ``current_depth()`` reflects the ``COORD_REVIEW_DEPTH`` sentinel injected by
    ``stream_subprocess`` when the parent reviewer CLI spawned us; a non-zero
    value means we are a nested server and must not launch yet another reviewer.
    """
    depth = current_depth()
    if depth >= _MAX_DEPTH:
        raise RuntimeError(
            "coord-review refused to launch a reviewer: this server is running "
            f"inside a reviewer subprocess (depth {depth} >= max {_MAX_DEPTH}). "
            "Nested review is disabled to prevent unbounded recursion "
            "(codex1 -> codex2 -> codex3 ...). Run review from the top-level "
            "session instead, or raise COORD_REVIEW_MAX_DEPTH to allow nesting."
        )


class _LogPump:
    """Bounded queue + single writer that forwards reviewer stderr to ``ctx.info``.

    Why not ``loop.create_task(ctx.info(...))`` per line?

    1. Every line spawns a task that races to write to the MCP stdio
       transport. Under verbose output the transport's write buffer grows
       unbounded.
    2. The tool coroutine can return its reply *before* all those tasks
       drain, leaving "ctx.info after tool returned" notifications that
       confuse clients.
    3. On cancellation, orphan tasks raise ``CancelledError`` into nowhere.

    This pump fixes all three: bounded queue, one writer, awaits drain on
    exit, and propagates cancellation cleanly.

    A heartbeat timer runs alongside the writer: every
    ``_HEARTBEAT_SEC`` seconds it emits a ``ctx.report_progress()`` (when
    the client supplied a progressToken) and a ``ctx.info()`` (unconditional
    keep-alive).  This prevents clients with idle timeouts from killing the
    connection while the reviewer is thinking silently.
    """

    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=_LOG_QUEUE_MAX)
        self._writer_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_interval = _HEARTBEAT_SEC
        self._start: float = 0.0
        self._dropped = 0

    def push(self, line: str) -> None:
        """Synchronous, non-blocking. Drops the oldest line if the queue is full."""
        if not line:
            return
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # drop the oldest, enqueue the newest — bounded memory under bursts
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(line)
            except asyncio.QueueFull:
                self._dropped += 1

    async def _heartbeat(self) -> None:
        """Periodic keep-alive: sends progress + info notifications."""
        self._start = time.monotonic()
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            elapsed = int(time.monotonic() - self._start)
            msg = f"[coord-review] reviewer still running ({elapsed}s elapsed)"
            # Structured progress notification (no-ops if client didn't send progressToken)
            with contextlib.suppress(Exception):
                await self._ctx.report_progress(
                    elapsed, total=None, message="reviewer still running"
                )
            # Unconditional info as fallback keep-alive for clients without progressToken
            with contextlib.suppress(Exception):
                await self._ctx.info(msg)

    async def _run(self) -> None:
        while True:
            line = await self._queue.get()
            if line is None:  # sentinel: drain finished
                return
            try:
                await self._ctx.info(line)
            except Exception:
                # never let a transport hiccup kill the pump
                pass

    async def __aenter__(self) -> "_LogPump":
        self._writer_task = asyncio.create_task(self._run())
        if self._heartbeat_interval > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Stop heartbeat first — no more keep-alive needed.
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat_task
            self._heartbeat_task = None

        # Signal the writer to drain and stop; wait for it to finish so we
        # don't return from the tool with notifications still in flight.
        # Use put_nowait with drop-oldest so a full queue + slow transport
        # can't deadlock the shutdown path.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                # Last resort: cancel the writer directly. We'd rather lose
                # a couple of progress lines than wedge the tool reply.
                if self._writer_task is not None:
                    self._writer_task.cancel()
        assert self._writer_task is not None
        try:
            await asyncio.wait_for(self._writer_task, timeout=5)
        except asyncio.TimeoutError:
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer_task
        if self._dropped:
            with contextlib.suppress(Exception):
                await self._ctx.info(
                    f"[coord-review] dropped {self._dropped} progress lines under load"
                )

    @property
    def push_callback(self) -> Callable[[str], None]:
        return self.push


def _ensure_absolute(label: str, value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        raise ValueError(f"{label} must be an absolute path; got {value!r}")
    return p


def _format_report(reviewer: str, our_id: str, result: ReviewResult) -> dict:
    if result.timed_out:
        status = "timeout"
    elif result.returncode != 0:
        status = "error"
    else:
        status = "ok"
    return {
        "reviewer": reviewer,
        "session_id": our_id,
        "status": status,
        "returncode": result.returncode,
        "report": result.text,
        "stderr_tail": "\n".join(result.raw_stderr.splitlines()[-40:]),
    }


async def _persist_and_format(
    reviewer: str, result: ReviewResult, cwd: str, ctx: Context
) -> dict:
    """Save the session if we got a usable native id, then format the report.

    A reviewer that crashes before printing anything leaves
    ``native_session_id`` empty. Persisting that anyway would hand the caller
    a ``cs_*`` handle that ``ask_reviewer`` could never resume — every
    follow-up would invoke e.g. ``claude --resume ""`` and fail. Better to
    return ``session_id=""`` and let the caller see the error directly.
    """
    if not result.native_session_id:
        await ctx.info(
            "[coord-review] reviewer produced no session id; not persisting "
            "(returncode={}, timed_out={})".format(result.returncode, result.timed_out)
        )
        return _format_report(reviewer, "", result)

    record = session_store.SessionRecord.new(
        reviewer=reviewer, native_id=result.native_session_id, cwd=cwd
    )
    session_store.save(record)
    await ctx.info(f"[coord-review] session saved as {record.our_id}")
    return _format_report(reviewer, record.our_id, result)


@mcp.tool()
async def review_repo(
    reviewer: Annotated[ReviewerName, Field(description=_REVIEWER_DESCRIPTION)],
    repo_dir: Annotated[
        str,
        Field(
            description=(
                "Absolute path to the directory the reviewer should look at. "
                "The reviewer is launched with this directory as its working "
                "root and reads files from it directly. For a git repository, "
                "pass the repo root (not a subdirectory) so the reviewer sees "
                "all project files, configs, and CI metadata."
            )
        ),
    ],
    brief: Annotated[str, Field(description=_BRIEF_DESCRIPTION)],
    ctx: Context,
) -> dict:
    """Ask another coding agent to review a directory of code.

    Returns a dict with these keys:

    - `reviewer` (str): the CLI that ran ("claude", "codex", or "cursor").
    - `session_id` (str): opaque handle of form `cs_<uuid>` you can pass to
      `ask_reviewer` to follow up. Empty string when `status` is "error" or
      "timeout" and the reviewer crashed before producing a usable session
      — follow-ups are NOT possible in that case.
    - `status` (str): one of `"ok"`, `"error"`, `"timeout"`.
    - `report` (str): the reviewer's findings (the main body to read).
    - `returncode` (int): raw process exit code; usually redundant with
      `status` and only useful for debugging.
    - `stderr_tail` (str): last 40 lines of reviewer stderr, for debugging
      crashes or empty reports.

    IMPORTANT: do not blindly accept everything in `report`. Assess whether
    each finding is real and actually needs fixing. If you are unsure about a
    finding, explain your reasoning (e.g. relevant code context, design intent)
    to the user and let them decide instead of applying the change yourself.

    """ + _NESTING_HINT + """
    """
    _refuse_if_nested()
    repo = _ensure_absolute("repo_dir", repo_dir)
    if not repo.is_dir():
        raise ValueError(f"repo_dir does not exist or is not a directory: {repo}")
    if not brief.strip():
        raise ValueError("brief must not be empty — describe what to review and why")

    rv = get_reviewer(reviewer)
    await ctx.info(f"[coord-review] launching {reviewer} on {repo}")
    async with _LogPump(ctx) as pump:
        try:
            result = await rv.run_initial(
                brief=brief, cwd=str(repo), log_line=pump.push_callback
            )
        except ReviewerNotFoundError as e:
            raise RuntimeError(str(e)) from e

    return await _persist_and_format(reviewer, result, str(repo), ctx)


@mcp.tool()
async def review_file(
    reviewer: Annotated[ReviewerName, Field(description=_REVIEWER_DESCRIPTION)],
    file_path: Annotated[
        str,
        Field(
            description=(
                "Absolute path to a single file to review. The reviewer's "
                "working root is set to this file's PARENT directory, so it "
                "can read sibling files but may NOT see the wider repository "
                "root. Prefer `review_repo` when the reviewer needs "
                "project-wide context (cross-package imports, configs, CI). "
                "Use this tool when the file is self-contained or lives "
                "outside a repository (e.g. a one-off script in /tmp)."
            )
        ),
    ],
    brief: Annotated[str, Field(description=_BRIEF_DESCRIPTION)],
    ctx: Context,
) -> dict:
    """Ask another coding agent to review a single file.

    The reviewer's working directory is set to the file's parent folder
    (not the surrounding repository root, if any) — see the `file_path`
    description for when that matters. The file path is prepended to the
    brief so the reviewer knows where to look.

    Returns the same shape as `review_repo`: a dict with `reviewer`,
    `session_id` (empty on failure), `status` (`"ok" | "error" | "timeout"`),
    `report`, `returncode`, and `stderr_tail`. Follow up via `ask_reviewer`
    with the returned `session_id`.

    IMPORTANT: do not blindly accept everything in `report`. Assess whether
    each finding is real and actually needs fixing. If you are unsure about a
    finding, explain your reasoning (e.g. relevant code context, design intent)
    to the user and let them decide instead of applying the change yourself.

    """ + _NESTING_HINT + """
    """
    _refuse_if_nested()
    target = _ensure_absolute("file_path", file_path)
    if not target.is_file():
        raise ValueError(f"file_path does not exist or is not a file: {target}")
    if not brief.strip():
        raise ValueError("brief must not be empty — describe what to review and why")

    rv = get_reviewer(reviewer)
    cwd = target.parent
    framed_brief = f"Please review the file at: {target}\n\n{brief}"
    await ctx.info(f"[coord-review] launching {reviewer} on {target}")
    async with _LogPump(ctx) as pump:
        try:
            result = await rv.run_initial(
                brief=framed_brief, cwd=str(cwd), log_line=pump.push_callback
            )
        except ReviewerNotFoundError as e:
            raise RuntimeError(str(e)) from e

    return await _persist_and_format(reviewer, result, str(cwd), ctx)


@mcp.tool()
async def ask_reviewer(
    session_id: Annotated[
        str,
        Field(
            description=(
                "Opaque handle returned by `review_repo` or `review_file` "
                "(form `cs_<uuid>`). Stable across follow-ups even if the "
                "underlying CLI rotates its native session id internally. "
                "The original `reviewer` choice (claude / codex / cursor) "
                "is bound to this session and cannot be changed in a "
                "follow-up."
            )
        ),
    ],
    question: Annotated[str, Field(description=_QUESTION_DESCRIPTION)],
    ctx: Context,
) -> dict:
    """Ask a follow-up question in an existing review session.

    The reviewer keeps full prior context from the original `review_repo`
    or `review_file` call — refer to earlier findings by file/line, don't
    repeat them.

    Returns the same shape as `review_repo` / `review_file`: a dict with
    `reviewer`, `session_id`, `status` (`"ok" | "error" | "timeout"`),
    `report`, `returncode`, and `stderr_tail`. The `report` field holds
    the reviewer's answer to this follow-up.

    IMPORTANT: do not blindly accept everything in `report`. Assess whether
    each finding is real and actually needs fixing. If you are unsure about a
    finding, explain your reasoning (e.g. relevant code context, design intent)
    to the user and let them decide instead of applying the change yourself.

    """ + _NESTING_HINT + """
    """
    _refuse_if_nested()
    if not question.strip():
        raise ValueError("question must not be empty")

    record = session_store.get(session_id)
    if record is None:
        raise ValueError(
            f"unknown session_id {session_id!r}; "
            "use review_repo or review_file to start a session first"
        )

    rv = get_reviewer(record.reviewer)
    await ctx.info(
        f"[coord-review] resuming {record.reviewer} session "
        f"{record.our_id} (native={record.native_id})"
    )
    async with _LogPump(ctx) as pump:
        try:
            result = await rv.run_resume(
                native_session_id=record.native_id,
                question=question,
                cwd=record.cwd,
                log_line=pump.push_callback,
            )
        except ReviewerNotFoundError as e:
            raise RuntimeError(str(e)) from e

    if result.native_session_id and result.native_session_id != record.native_id:
        session_store.update_native_id(record.our_id, result.native_session_id)
        await ctx.info(
            f"[coord-review] native session id rotated to {result.native_session_id}"
        )

    payload = _format_report(record.reviewer, record.our_id, result)
    return payload


def main() -> None:
    """Entry point for the ``coord-review`` console script."""
    # ``mcp.run()`` defaults to stdio, which is what every supported client wants.
    mcp.run()


if __name__ == "__main__":
    main()
