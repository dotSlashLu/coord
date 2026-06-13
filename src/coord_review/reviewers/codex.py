"""Codex CLI (`codex`) reviewer adapter.

Headless review:

    codex --sandbox read-only --ask-for-approval never \
        exec --json --skip-git-repo-check "<BRIEF>"

Codex streams progress and final messages as JSONL on stdout when invoked with
``exec --json``. The first ``thread.started`` event provides the session id,
which can be resumed with ``codex exec resume <id>``.
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


def _codex_model() -> str:
    return os.environ.get("COORD_REVIEW_CODEX_MODEL", "")


def _codex_sandbox() -> str:
    return os.environ.get("COORD_REVIEW_CODEX_SANDBOX", "read-only")


def _common_argv(binary: str) -> list[str]:
    argv = [
        binary,
        "--sandbox",
        _codex_sandbox(),
        "--ask-for-approval",
        "never",
    ]
    model = _codex_model()
    if model:
        argv.extend(["--model", model])
    return argv


def _extract_agent_text(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("type") != "agent_message":
        return ""
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, str):
        return content
    return ""


def _parse_jsonl(stdout: str) -> tuple[str, str]:
    """Pull ``(result, thread_id)`` out of Codex ``exec --json`` JSONL.

    Codex emits one JSON object per line. We keep the latest completed
    ``agent_message`` as the result, and the ``thread.started.thread_id`` as
    the resumable native session id.
    """
    text = stdout.strip()
    if not text:
        return ("", "")

    thread_id = ""
    last_message = ""
    parsed_any = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(env, dict):
            continue
        parsed_any = True
        if env.get("type") == "thread.started":
            candidate = env.get("thread_id")
            if isinstance(candidate, str):
                thread_id = candidate
        if env.get("type") == "item.completed":
            found = _extract_agent_text(env.get("item"))
            if found:
                last_message = found

    if parsed_any:
        return (last_message, thread_id)

    return (text, "")


def _result_from_proc(proc: ProcResult) -> ReviewResult:
    text, native_id = _parse_jsonl(proc.stdout)
    return ReviewResult(
        text=text or proc.stdout,
        native_session_id=native_id,
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
        returncode=proc.returncode,
        timed_out=proc.timed_out,
    )


class CodexReviewer(Reviewer):
    name = "codex"

    async def run_initial(
        self,
        *,
        brief: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        binary = require_binary("codex")
        argv = _common_argv(binary)
        argv.extend(
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--",
                brief,
            ]
        )
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
        binary = require_binary("codex")
        argv = _common_argv(binary)
        argv.extend(
            [
                "exec",
                "resume",
                "--json",
                "--skip-git-repo-check",
                native_session_id,
                "--",
                question,
            ]
        )
        proc = await stream_subprocess(argv, cwd=cwd, log_line=log_line)
        return _result_from_proc(proc)
