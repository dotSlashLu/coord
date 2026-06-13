"""Regression tests for the Codex reviewer's stdout JSONL parser."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coord_review.reviewers.codex import _parse_jsonl


def test_extracts_thread_id_and_final_agent_message():
    out = "\n".join(
        [
            '{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"final"}}',
            '{"type":"turn.completed"}',
        ]
    )
    text, thread_id = _parse_jsonl(out)
    assert text == "final"
    assert thread_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"


def test_ignores_non_message_items():
    out = "\n".join(
        [
            '{"type":"thread.started","thread_id":"tid"}',
            '{"type":"item.completed","item":{"type":"command_execution","command":"ls"}}',
        ]
    )
    text, thread_id = _parse_jsonl(out)
    assert text == ""
    assert thread_id == "tid"


def test_empty_stdout_returns_empty_pair():
    assert _parse_jsonl("") == ("", "")
    assert _parse_jsonl("   \n  ") == ("", "")


def test_unparseable_falls_back_to_raw_text():
    raw = "plain stdout without JSON"
    assert _parse_jsonl(raw) == (raw, "")
