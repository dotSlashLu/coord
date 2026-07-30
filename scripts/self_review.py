"""End-to-end smoke: ask Cursor to review coord-review itself.

Calls the MCP tool functions directly (no stdio protocol) so we can see the
real reviewer output without standing up an MCP client.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coord_review.reviewers.cursor import CursorReviewer


async def main() -> None:
    repo = str(Path(__file__).resolve().parents[1])
    brief = (
        "Please review this Python MCP server (`coord-review`). It's a stdio "
        "MCP server that exposes three tools — `review_repo`, "
        "`review_file`, `ask_reviewer` — that wrap the Claude Code and Cursor "
        "CLIs as code reviewers, so a coding agent (e.g. Codex) can ask "
        "another agent to review its work and follow up via session resume.\n\n"
        "Focus on:\n"
        "1. Correctness — anything that wouldn't actually work as advertised. "
        "Pay particular attention to `src/coord_review/reviewers/cursor.py` "
        "(the `cursor-agent` argv shape) and `src/coord_review/server.py` "
        "(the MCP tool definitions).\n"
        "2. Concurrency — the async subprocess helper in "
        "`src/coord_review/subprocess_util.py` and the `loop.create_task` "
        "bridge in `server.py:_make_log_line`. Are there races, leaked tasks, "
        "or backpressure issues?\n"
        "3. Robustness — the JSON envelope parsers in the two reviewer "
        "adapters. What happens on malformed output? What happens if a "
        "subprocess crashes before printing anything?\n"
        "4. The on-disk session store in `src/coord_review/session_store.py` "
        "— is the atomic-write pattern correct? Anything that breaks under a "
        "concurrent read-modify-write?\n\n"
        "Skip style nits and trivial naming feedback. Be specific: cite "
        "files and lines."
    )

    reviewer = CursorReviewer()

    def log(line: str) -> None:
        if line.strip():
            print(f"  [cursor] {line}", flush=True)

    print(f"→ launching cursor-agent on {repo}", flush=True)
    result = await reviewer.run_initial(brief=brief, cwd=repo, log_line=log)
    print()
    print("=" * 80)
    print(f"returncode = {result.returncode}, timed_out = {result.timed_out}")
    print(f"native_session_id = {result.native_session_id}")
    print("=" * 80)
    print(result.text or "(no text)")
    print("=" * 80)
    if result.raw_stderr.strip():
        print("--- stderr tail ---")
        print("\n".join(result.raw_stderr.splitlines()[-20:]))


if __name__ == "__main__":
    asyncio.run(main())
