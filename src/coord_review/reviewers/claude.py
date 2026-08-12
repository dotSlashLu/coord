"""Claude Code (`claude`) reviewer adapter.

Headless review:

    claude -p "<BRIEF>" \
        --output-format json \
        --add-dir <repo_dir> \
        --permission-mode acceptEdits \
        --allowedTools "Read,Grep,Glob,Bash(git diff:*),..." \
        --model sonnet \
        --bare

The final stdout is a JSON envelope containing ``result`` (the review text) and
``session_id`` (Claude's session UUID). Resume with ``--resume <id>``.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Optional

from coord_review.reviewers.base import Reviewer, ReviewResult
from coord_review.subprocess_util import (
    ProcResult,
    require_binary,
    stream_subprocess,
)


# Read-family tools + a fixed set of read-only shell commands. Anything that
# mutates the workspace stays off this list — Claude won't be able to call it
# even if it tries, because acceptEdits without the tool grant prompts and we
# never answer prompts in headless mode.
ALLOWED_TOOLS = ",".join(
    [
        "Read",
        "Grep",
        "Glob",
        "Bash(git diff:*)",
        "Bash(git log:*)",
        "Bash(git status:*)",
        "Bash(git show:*)",
        "Bash(git blame:*)",
        "Bash(rg:*)",
        "Bash(ls:*)",
        "Bash(cat:*)",
        "Bash(head:*)",
        "Bash(tail:*)",
        "Bash(wc:*)",
        "Bash(file:*)",
        "Bash(find:*)",
    ]
)


def _claude_model() -> str:
    return os.environ.get("COORD_REVIEW_CLAUDE_MODEL", "opus")


def _parse_envelope(stdout: str) -> tuple[str, str]:
    """Pull ``(result, session_id)`` out of Claude's --output-format json envelope.

    Claude prints a single JSON object to stdout when invoked with
    ``-p --output-format json``. If parsing fails we surface the raw stdout as
    the result and an empty session id — the caller decides how to react.
    """
    text = stdout.strip()
    if not text:
        return ("", "")
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        # last-resort: try to find a trailing JSON object on the final line
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    env = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        else:
            return (text, "")
    result = env.get("result") or env.get("response") or ""
    session_id = env.get("session_id") or ""
    return (str(result), str(session_id))


def _result_from_proc(proc: ProcResult) -> ReviewResult:
    text, native_id = _parse_envelope(proc.stdout)
    return ReviewResult(
        text=text or proc.stdout,
        native_session_id=native_id,
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
        returncode=proc.returncode,
        timed_out=proc.timed_out,
    )


class ClaudeReviewer(Reviewer):
    name = "claude"

    async def run_initial(
        self,
        *,
        brief: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        binary = require_binary("claude")
        argv = [
            binary,
            "-p",
            brief,
            "--output-format",
            "json",
            "--add-dir",
            cwd,
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            ALLOWED_TOOLS,
            "--model",
            _claude_model(),
            "--bare",
        ]
        proc = await stream_subprocess(argv, cwd=cwd, log_line=log_line)
        return _result_from_proc(proc)

    async def run_resume(
        self,
        *,
        native_session_id: str,
        question: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        binary = require_binary("claude")
        argv = [
            binary,
            "-p",
            question,
            "--resume",
            native_session_id,
            "--output-format",
            "json",
            "--bare",
        ]
        proc = await stream_subprocess(argv, cwd=cwd, log_line=log_line)
        return _result_from_proc(proc)
