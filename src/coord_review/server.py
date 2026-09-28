"""MCP server exposing review_repo / review_file / ask_reviewer tools."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Annotated, Callable, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from coord_review import session_store
from coord_review.reviewers import get_reviewer
from coord_review.reviewers.base import ReviewResult
from coord_review.subprocess_util import (
    ReviewerNotFoundError,
    current_depth,
)


def _resolve_version() -> str:
    """Report the installed distribution version, or an explicit placeholder.

    ``MCPServer`` defaults ``version`` to ``""``, which reaches clients as
    ``serverInfo.version = ""`` — that reads as a client-side bug rather than
    as "this server has no version". Running from a source checkout that was
    never installed is a legitimate case, so fall back to a visible sentinel
    instead of silently claiming the empty string.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("coord-review")
    except PackageNotFoundError:
        return "0.0.0+source"


# Server-level guidance. Tool descriptions are per-call and can be skimmed;
# this restates the cross-cutting contract once per session. Rendered by
# ``_server_instructions()`` below, which is also what the wire-contract tests
# call directly to check the text against ``COORD_REVIEW_MAX_DEPTH``.
_SERVER_INSTRUCTIONS = (
    "coord-review hands code off to a *different* coding agent.\n"
    "\n"
    "{gate}\n"
    "\n"
    "Reviewers: {reviewers}\n"
    "\n"
    "Rules:\n"
    "1. Explicit request only (see the gate above): a plain \u201creview this code\u201d "
    "is not a request to spend a second agent. Do not fan out reviews "
    "proactively.\n"
    "2. Never call from inside a review sub-flow. A reviewer you launch is "
    "itself a full agent that can see this same MCP server, so a review "
    "started inside a review becomes codex1 -> codex2 -> codex3, an "
    "unbounded chain. {nesting}\n"
    "3. Ask the user which reviewer to use rather than choosing for them.\n"
    "4. Treat a returned `report` as advice: verify each finding, and if you "
    "disagree, explain your reasoning to the user instead of silently acting.\n"
    "\n"
    "Tools:\n{tools}\n"
)

_REVIEWER_SUMMARY = (
    "claude, codex, and cursor each run their own CLI headlessly; the choice "
    "is bound to a session at creation and cannot be changed in a follow-up. "
    "They differ in cost, latency and sandbox strength, not in the shape of "
    "the report."
)


_SERVER_DESCRIPTION_FALLBACK = (
    "MCP server that lets one coding agent ask another coding agent "
    "(Claude, Codex, or Cursor) to review its work, with resumable "
    "follow-up sessions."
)


def _server_description() -> str:
    """One-line summary; mirrors ``pyproject.toml``'s project description."""
    from importlib.metadata import PackageNotFoundError, metadata

    try:
        return metadata("coord-review")["Summary"] or _SERVER_DESCRIPTION_FALLBACK
    except (PackageNotFoundError, KeyError):
        return _SERVER_DESCRIPTION_FALLBACK


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

# Tool-level descriptions. These are assembled from constants and passed to the
# decorator explicitly (``@mcp.tool(description=...)``) rather than written as
# function docstrings: under the v2 ``MCPServer`` the decorator reads
# ``fn.__doc__``, which is ``None`` for the ``"""body""" + _NESTING_HINT + """."""
# idiom this file used to use. That idiom left every tool with an empty
# description on the wire, nesting hint included (see
# ``tests/test_wire_contract.py``). An explicit argument keeps the wire value
# independent of docstring grammar.
_REVIEW_REPO_DESCRIPTION = (
    "Ask another coding agent to review a directory of code.\n\n"
    "Returns a dict with these keys:\n\n"
    "- `reviewer` (str): the CLI that ran (\"claude\", \"codex\", or \"cursor\").\n"
    "- `session_id` (str): opaque handle of form `cs_<uuid>` you can pass to "
    "`ask_reviewer` to follow up. Empty string when `status` is \"error\" or "
    "\"timeout\" and the reviewer crashed before producing a usable session "
    "— follow-ups are NOT possible in that case.\n"
    "- `status` (str): one of `\"ok\"`, `\"error\"`, `\"timeout\"`.\n"
    "- `report` (str): the reviewer's findings (the main body to read).\n"
    "- `returncode` (int): raw process exit code; usually redundant with "
    "`status` and only useful for debugging.\n"
    "- `stderr_tail` (str): last 40 lines of reviewer stderr, for debugging "
    "crashes or empty reports.\n\n"
    "IMPORTANT: do not blindly accept everything in `report`. Assess whether "
    "each finding is real and actually needs fixing. If you are unsure about a "
    "finding, explain your reasoning (e.g. relevant code context, design intent) "
    "to the user and let them decide instead of applying the change yourself.\n\n"
)
_REVIEW_FILE_DESCRIPTION = (
    "Ask another coding agent to review a single file.\n\n"
    "The reviewer's working directory is set to the file's parent folder "
    "(not the surrounding repository root, if any) — see the `file_path` "
    "description for when that matters. The file path is prepended to the "
    "brief so the reviewer knows where to look.\n\n"
    "Returns the same shape as `review_repo`: a dict with `reviewer`, "
    "`session_id` (empty on failure), `status` (`\"ok\" | \"error\" | \"timeout\"`), "
    "`report`, `returncode`, and `stderr_tail`. Follow up via `ask_reviewer` "
    "with the returned `session_id`.\n\n"
    "IMPORTANT: do not blindly accept everything in `report`. Assess whether "
    "each finding is real and actually needs fixing. If you are unsure about a "
    "finding, explain your reasoning (e.g. relevant code context, design intent) "
    "to the user and let them decide instead of applying the change yourself.\n\n"
)
_ASK_REVIEWER_DESCRIPTION = (
    "Ask a follow-up question in an existing review session.\n\n"
    "The reviewer keeps full prior context from the original `review_repo` "
    "or `review_file` call — refer to earlier findings by file/line, don't "
    "repeat them.\n\n"
    "Returns the same shape as `review_repo` / `review_file`: a dict with "
    "`reviewer`, `session_id`, `status` (`\"ok\" | \"error\" | \"timeout\"`), "
    "`report`, `returncode`, and `stderr_tail`. The `report` field holds "
    "the reviewer's answer to this follow-up.\n\n"
    "IMPORTANT: do not blindly accept everything in `report`. Assess whether "
    "each finding is real and actually needs fixing. If you are unsure about a "
    "finding, explain your reasoning (e.g. relevant code context, design intent) "
    "to the user and let them decide instead of applying the change yourself.\n\n"
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

# The gate. This is the single most important thing a model must read before
# calling any of these tools, so it leads BOTH channels: it is appended to every
# tool description, and it is the first thing in ``instructions``. Order matters
# because some clients bound the server-authored text they forward — Shrimp, for
# one, projects it into one <=400-rune line (see
# ``_INSTRUCTIONS_GATE_BUDGET_RUNES``), so a gate buried after the prose would be
# truncated away.
_USER_REQUEST_HINT = (
    "Use this ONLY when the user explicitly asked for an external review — by "
    "naming coord-review or one of the CLIs it drives (claude / codex / "
    "cursor). A plain \u201creview this code\u201d does NOT qualify: review it yourself "
    "unless the user asked to hand it off. Spending a second agent is the "
    "user's call, not yours."
)
_NESTING_ONLY_HINT = (
    "Never call it from inside a review sub-flow — that nests reviewers and is "
    "blocked regardless."
)
# Appended to each tool description: the gate first, then the nesting rule.
# Soft nudges only — the hard backstop is the depth check in _refuse_if_nested().
_NESTING_HINT = _USER_REQUEST_HINT + " " + _NESTING_ONLY_HINT


_TOOL_SUMMARIES = {
    "review_repo": "Start a review of a whole directory (pass a repo root).",
    "review_file": "Start a review of one file (parent dir is the reviewer's root).",
    "ask_reviewer": "Ask a follow-up in an existing review session by session_id.",
}

# Not a guess: this mirrors Shrimp's ``hiddenMCPInstructionsRunes``
# (internal/agent/mcp_hidden.go), the bound it applies when projecting a
# server's instructions into its system prompt as one ``说明：`` line. Any
# consumer that trims server-authored text will cut from the end, so the gate
# must fit inside this budget to survive. Asserted by the wire-contract tests.
_INSTRUCTIONS_GATE_BUDGET_RUNES = 400


def _server_instructions() -> str:
    """Render the server-level guidance text.

    Tool descriptions are per-call and can be skimmed; this restates the
    cross-cutting contract once per session. The nesting wording is derived
    from ``_MAX_DEPTH`` so the text can never claim a limit the server is not
    actually enforcing.

    ``{gate}`` is ``_USER_REQUEST_HINT``: it leads the text so a client that
    truncates or bounds server-authored instructions (Shrimp projects it into
    a single ``<= _INSTRUCTIONS_GATE_BUDGET_RUNES``-rune line) still forwards
    the rule that matters most.
    """
    if _MAX_DEPTH <= 1:
        nesting = (
            "The refusal is a hard check in the server, not guidance you can "
            "reason your way around: no prompt makes a nested review succeed."
        )
    else:
        nesting = (
            f"This machine allows nesting up to depth {_MAX_DEPTH} "
            "(COORD_REVIEW_MAX_DEPTH); still avoid it unless the user "
            "explicitly asked for a review within a review."
        )
    return _SERVER_INSTRUCTIONS.format(
        gate=_USER_REQUEST_HINT,
        reviewers=_REVIEWER_SUMMARY,
        nesting=nesting,
        tools="\n".join(
            f"- {name}: {summary}" for name, summary in _TOOL_SUMMARIES.items()
        ),
    )


mcp: MCPServer = MCPServer(
    "coord-review",
    version=_resolve_version(),
    description=_server_description(),
    instructions=_server_instructions(),
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


@mcp.tool(description=_REVIEW_REPO_DESCRIPTION + _NESTING_HINT)
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


@mcp.tool(description=_REVIEW_FILE_DESCRIPTION + _NESTING_HINT)
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


@mcp.tool(description=_ASK_REVIEWER_DESCRIPTION + _NESTING_HINT)
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
    #
    # ``version`` / ``description`` / ``instructions`` are rendered at import,
    # when MCPServer is constructed — they are read-only properties afterwards.
    # That is the same moment the env-derived constants (_MAX_DEPTH,
    # _HEARTBEAT_SEC) are read, so the announced configuration always matches
    # the one the server enforces.
    mcp.run()


if __name__ == "__main__":
    main()
