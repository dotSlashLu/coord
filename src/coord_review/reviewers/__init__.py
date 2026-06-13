"""Reviewer adapters: one module per backing CLI."""

from coord_review.reviewers.base import Reviewer, ReviewResult
from coord_review.reviewers.claude import ClaudeReviewer
from coord_review.reviewers.codex import CodexReviewer
from coord_review.reviewers.cursor import CursorReviewer

__all__ = [
    "Reviewer",
    "ReviewResult",
    "ClaudeReviewer",
    "CodexReviewer",
    "CursorReviewer",
    "get_reviewer",
]


def get_reviewer(name: str) -> Reviewer:
    """Return the adapter instance for ``name``."""
    name = name.lower().strip()
    if name == "claude":
        return ClaudeReviewer()
    if name == "codex":
        return CodexReviewer()
    if name == "cursor":
        return CursorReviewer()
    raise ValueError(
        f"unknown reviewer {name!r}; expected 'claude', 'codex', or 'cursor'"
    )
