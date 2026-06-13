"""Regression tests for the cursor reviewer's stdout JSON parser."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coord_review.reviewers.cursor import _parse_envelope


def test_whole_buffer_single_object_wins():
    """A single top-level JSON object should be trusted as-is."""
    out = '{"result": "the actual review text"}'
    assert _parse_envelope(out) == "the actual review text"


def test_whole_buffer_wins_over_stray_earlier_line():
    """Whole-buffer parse beats any inner JSON-shaped line.

    Pre-fix, the line-scan ran first and could grab a stray ``{"text": ...}``
    from inside a multi-line blob, even when the whole buffer was itself a
    single valid object. The fix returns the whole-buffer hit immediately.
    """
    # Note: the whole text is itself one valid JSON object (string value
    # contains an embedded JSON-shaped substring).
    out = '{"result": "real answer\\nwith a {\\"text\\": \\"decoy\\"} inline"}'
    parsed = _parse_envelope(out)
    assert parsed.startswith("real answer")
    assert "decoy" in parsed  # the decoy survives as part of the real text


def test_streaming_lines_bottom_up_lookup():
    """Stream-json mode: pick the last line with assistant text."""
    out = "\n".join(
        [
            '{"event": "start"}',
            '{"event": "tool", "tool": "read"}',
            '{"text": "the final answer"}',
        ]
    )
    assert _parse_envelope(out) == "the final answer"


def test_message_content_shape():
    """``message.content`` is also accepted."""
    out = '{"message": {"content": "answer via message.content"}}'
    assert _parse_envelope(out) == "answer via message.content"


def test_empty_stdout_returns_empty():
    assert _parse_envelope("") == ""
    assert _parse_envelope("   \n  ") == ""


def test_unparseable_falls_back_to_raw_text():
    """No JSON anywhere — return the raw stdout so the caller sees something."""
    raw = "not json at all, just plain text"
    assert _parse_envelope(raw) == raw


def test_whole_buffer_with_no_known_keys_returns_empty_not_decoy():
    """If the whole buffer parses but carries no recognised text key, we
    return ``""`` rather than scavenging an earlier line. The reviewer
    legitimately said nothing; surfacing a stray decoy would be worse than
    surfacing nothing."""
    out = '{"unrelated": "metadata only"}'
    assert _parse_envelope(out) == ""
