"""Reviewer adapter ABC plus the result type both adapters return."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ReviewResult:
    """Outcome of a reviewer turn (initial review or follow-up)."""

    text: str  # the assistant message — review report or answer
    native_session_id: str  # reviewer-specific session id we'll resume against
    raw_stdout: str  # whole captured stdout, useful for debugging
    raw_stderr: str
    returncode: int
    timed_out: bool


class Reviewer(ABC):
    """One reviewer backend."""

    name: str  # "claude" / "codex" / "cursor"

    @abstractmethod
    async def run_initial(
        self,
        *,
        brief: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        """Start a fresh review session in ``cwd`` with the given brief."""

    @abstractmethod
    async def run_resume(
        self,
        *,
        native_session_id: str,
        question: str,
        cwd: str,
        log_line: Optional[Callable[[str], None]] = None,
    ) -> ReviewResult:
        """Continue session ``native_session_id`` with a follow-up question."""
