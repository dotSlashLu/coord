"""Round 2: ask Cursor (Opus 4.6 thinking) to review the FIXES.

Round 1 identified four bugs. We applied targeted fixes; this script asks
the same reviewer to grade the fixes, look for regressions, and surface
anything we still missed.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coord_review.reviewers.cursor import CursorReviewer


async def main() -> None:
    repo = str(Path(__file__).resolve().parents[1])
    brief = (
        "This is a follow-up review of `coord-review`. You (or your earlier "
        "self) reviewed it once already and flagged several issues. We have "
        "since applied the following targeted fixes:\n\n"
        "1. **Cursor argv `--` separator**: in "
        "`src/coord_review/reviewers/cursor.py`, both `run_initial` and "
        "`run_resume` now insert a literal `\"--\"` between the flags and "
        "the positional brief/question. This was your finding: a brief "
        "starting with `-` would have been parsed as a flag.\n\n"
        "2. **Empty native_session_id no longer persisted**: in "
        "`src/coord_review/server.py`, a new `_persist_and_format` helper "
        "skips saving the SessionRecord when the reviewer produced no "
        "session id (e.g. crashed before printing anything). The caller "
        "still gets a structured response, but `session_id` is empty so "
        "they don't get a permanently-broken handle.\n\n"
        "3. **Bounded log pump replaces fire-and-forget `loop.create_task`**: "
        "`server.py` now uses an `_LogPump` class — a bounded "
        "`asyncio.Queue(maxsize=256)` + a single writer task. On enqueue "
        "overflow we drop the oldest line and count drops; on context exit "
        "we send a sentinel, await the writer, and surface the drop count "
        "via `ctx.info`. Used as `async with _LogPump(ctx) as pump:` in all "
        "three tools.\n\n"
        "4. **Cursor `_parse_envelope` priority inversion**: in "
        "`src/coord_review/reviewers/cursor.py`, the parser now returns "
        "immediately on a successful whole-buffer parse (even if every "
        "known text key is empty — that means the reviewer legitimately "
        "said nothing, and an earlier stray JSON-shaped line should not "
        "masquerade as the answer). Line-scan only runs if whole-buffer "
        "parse fails. Extracted into a small helper "
        "`_extract_text_from_obj`.\n\n"
        "**What I want from this review:**\n\n"
        "(a) Did each fix actually address the issue you flagged? Be "
        "specific — cite the new code and explain why it does or doesn't.\n\n"
        "(b) Did any fix introduce new bugs or regressions? Pay particular "
        "attention to `_LogPump.__aexit__` — does it handle subprocess "
        "cancellation correctly? What if `ctx.info` raises mid-drain? What "
        "if the writer task is cancelled before it processes the sentinel?\n\n"
        "(c) Are there issues you flagged previously that are NOT addressed "
        "here? List them. (We deliberately deferred multi-process locking "
        "in `session_store.py` and the stdin-deadlock latent issue in "
        "`subprocess_util.py`.)\n\n"
        "(d) New issues you didn't see the first time? Now that you've "
        "looked at the codebase once, a second pass often catches things "
        "the first pass missed. Don't reach for nits; only flag real "
        "correctness/concurrency/robustness issues.\n\n"
        "Output format: structured by my (a)/(b)/(c)/(d) above. Cite "
        "files:lines."
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


if __name__ == "__main__":
    asyncio.run(main())
