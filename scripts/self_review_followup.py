"""Follow-up turn: confirm the brief actually reached the reviewer.

If `-p` argv ordering is broken, the brief never landed and Cursor was just
inferring focus areas from the code. Ask it explicitly.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coord_review.reviewers.cursor import CursorReviewer


CHAT_ID = "68364ee5-a40f-42aa-b753-e1676406dd38"


async def main() -> None:
    repo = str(Path(__file__).resolve().parents[1])
    reviewer = CursorReviewer()

    def log(line: str) -> None:
        if line.strip():
            print(f"  [cursor] {line}", flush=True)

    question = (
        "Quick meta-check: in my original review request I asked you to focus "
        "on four specific areas — correctness (esp. cursor.py argv and "
        "server.py FastMCP defs), concurrency (subprocess_util.py + "
        "_make_log_line), robustness (JSON envelope parsing), and the session "
        "store's atomic-write pattern. Can you quote back, verbatim or "
        "paraphrased, the exact phrasing of focus area #2 (concurrency) from "
        "my original message? I want to confirm the brief actually reached "
        "you and wasn't dropped by an argv-parsing bug in the wrapper that "
        "invoked you."
    )

    print(f"→ resuming chat {CHAT_ID}", flush=True)
    result = await reviewer.run_resume(
        native_session_id=CHAT_ID, question=question, cwd=repo, log_line=log
    )
    print()
    print("=" * 80)
    print(f"returncode = {result.returncode}, timed_out = {result.timed_out}")
    print("=" * 80)
    print(result.text or "(no text)")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
