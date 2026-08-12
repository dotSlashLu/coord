"""Cursor (`cursor-agent`) reviewer adapter.

Cursor's CLI doesn't have a per-tool allowlist. The honest mapping for
"read + restricted bash" is:

  - ``--mode plan`` → read-only, no shell at all (too restrictive for a useful
    reviewer that wants to run ``git diff``/``rg``)
  - ``--force`` (alias ``--yolo``) → all commands allowed unless explicitly
    denied in ``~/.cursor/cli-config.json``

We pick ``--force`` for parity with the Claude adapter's ability to run
``git diff`` and friends, and document the gap in the README. Workspace trust
plus ``--workspace`` still scope filesystem access.

Chat ids are pre-allocated via ``cursor-agent create-chat`` so we capture the
id deterministically before the conversation starts. ``--resume <id>`` is then
used both for the initial review (talking to that empty chat) and for
follow-ups, with the prompt as the positional argument.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

from coord_review.reviewers.base import Reviewer, ReviewResult
from coord_review.subprocess_util import (
    ProcResult,
    require_binary,
    stream_subprocess,
)


_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _cursor_model() -> str:
    return os.environ.get("COORD_REVIEW_CURSOR_MODEL", "claude-opus-4-8-thinking-high")


def _parse_chat_id(stdout: str) -> str:
    """Extract the chat UUID printed by ``cursor-agent create-chat``."""
    text = stdout.strip()
    # Common shapes observed: bare UUID on its own line, or JSON like
    # {"chatId":"<uuid>"}. Be permissive — find the first UUID anywhere.
    match = _UUID_RE.search(text)
    if not match:
        raise RuntimeError(
            f"could not parse chat id from `cursor-agent create-chat` output: {text!r}"
        )
    return match.group(0)


def _extract_text_from_obj(env: object) -> str:
    """Pull assistant text out of one parsed JSON object, or return ``""``."""
    if not isinstance(env, dict):
        return ""
    for key in ("result", "text", "response"):
        val = env.get(key)
        if isinstance(val, str) and val:
            return val
    msg = env.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content:
            return content
    return ""


def _parse_envelope(stdout: str) -> str:
    """Extract the assistant text from --output-format json output.

    Cursor's JSON shape varies across versions and across stream / non-stream
    modes. Order of confidence:
      1. Whole stdout parses as one JSON object → trust it.
      2. Otherwise scan lines bottom-up for the last JSON object that has
         text in a known key (stream-json mode emits one event per line, the
         final assistant message lands near the end).
      3. Give up and return the raw stdout.
    """
    text = stdout.strip()
    if not text:
        return ""

    # 1. Whole-buffer first — it's a single object when the CLI returns
    # ``--output-format json`` (non-streaming). Don't fall through on a
    # successful parse, even if every key was empty: that means the reviewer
    # legitimately produced no text, and a stray earlier line shouldn't
    # masquerade as the answer.
    try:
        env = json.loads(text)
    except json.JSONDecodeError:
        env = None
    if env is not None:
        return _extract_text_from_obj(env) or ""

    # 2. Line-by-line, bottom-up.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = _extract_text_from_obj(obj)
        if found:
            return found

    # 3. Last resort.
    return text


def _result_from_proc(proc: ProcResult, native_id: str) -> ReviewResult:
    text = _parse_envelope(proc.stdout)
    return ReviewResult(
        text=text or proc.stdout,
        native_session_id=native_id,
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
        returncode=proc.returncode,
        timed_out=proc.timed_out,
    )


class CursorReviewer(Reviewer):
    name = "cursor"

    async def _create_chat(self, *, log_line) -> str:
        binary = require_binary("agent", "cursor-agent")
        proc = await stream_subprocess(
            [binary, "create-chat"], log_line=log_line, timeout=60
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"`{binary} create-chat` failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return _parse_chat_id(proc.stdout)

    async def run_initial(
        self,
        *,
        brief: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        binary = require_binary("agent", "cursor-agent")
        chat_id = await self._create_chat(log_line=log_line)
        argv = [
            binary,
            "--resume",
            chat_id,
            "-p",
            "--output-format",
            "json",
            "--workspace",
            cwd,
            "--trust",
            "--approve-mcps",
            "--force",
            "--model",
            _cursor_model(),
            "--",  # stop flag parsing; protects briefs starting with '-'
            brief,
        ]
        proc = await stream_subprocess(argv, cwd=cwd, log_line=log_line)
        return _result_from_proc(proc, native_id=chat_id)

    async def run_resume(
        self,
        *,
        native_session_id: str,
        question: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        binary = require_binary("agent", "cursor-agent")
        argv = [
            binary,
            "--resume",
            native_session_id,
            "-p",
            "--output-format",
            "json",
            "--workspace",
            cwd,
            "--trust",
            "--approve-mcps",
            "--force",
            "--model",
            _cursor_model(),
            "--",  # stop flag parsing; protects questions starting with '-'
            question,
        ]
        proc = await stream_subprocess(argv, cwd=cwd, log_line=log_line)
        return _result_from_proc(proc, native_id=native_session_id)
