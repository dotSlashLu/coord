"""The wire contract clients actually see: server metadata + tool descriptions.

Regression guard for a silent-outage class of bug. ``9296e40`` wrote each
tool's description as a function docstring using the idiom::

    ) -> dict:
        \"\"\"Body text...\"\"\"
        + _NESTING_HINT + \"\"\"
        \"\"\"

which relies on *one* implicit string concatenation to merge the pieces.
``fb23201`` (v1 FastMCP -> v2 MCPServer) changed which attribute the framework
reads: ``FastMCP`` resolved the merged literal via ``inspect.getdoc``, while
``Tool.from_function`` reads ``fn.__doc__``, which is ``None`` for that
expression. Result: every tool reached agents with ``description == ""`` —
including the anti-recursion hint and the "don't blindly trust the report"
warning — and nothing failed, because no test looked at the wire value.

These tests speak the real MCP protocol over stdio rather than poking at
internals, so they also catch a future framework/decoration change that drops
the description somewhere else in the pipeline.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from coord_review import server

TOOL_NAMES = ["review_repo", "review_file", "ask_reviewer"]

# Fragments that make each tool usable. Checking for these (rather than a
# length threshold) keeps the assertion meaningful: an empty or truncated
# description fails, and so does one that lost the human-in-the-loop warning.
_REQUIRED_FRAGMENTS = {
    "review_repo": ["review", "session_id", "report"],
    "review_file": ["single file", "session_id", "report"],
    "ask_reviewer": ["follow-up", "prior context", "report"],
}

# The shared hint every tool must carry (see server._NESTING_HINT).
_NESTING_FRAGMENTS = ["explicitly asked", "review sub-flow"]


async def _handshake():
    """Initialize a real stdio session and list tools; returns (init, tools)."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coord_review.server"],
        env={
            "PATH": os.environ["PATH"],
            # Ensure the checkout under test is importable even when the
            # package is not installed into the running interpreter.
            "PYTHONPATH": os.pathsep.join(
                p for p in [os.path.dirname(os.path.dirname(
                    os.path.abspath(server.__file__)
                )), os.environ.get("PYTHONPATH", "")] if p
            ),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            return init, tools


@pytest.fixture(scope="module")
def handshake():
    return asyncio.run(_handshake())


def test_server_reports_name_and_version(handshake):
    init, _ = handshake
    assert init.server_info.name == "coord-review"
    # v2's MCPServer defaults version to "" — an empty version reaches clients
    # as a broken serverInfo, so it is asserted explicitly.
    assert init.server_info.version, "serverInfo.version must not be empty"
    # A description travels to clients via serverInfo.description; it is what a
    # human sees in a client's server list, so an empty one is a silent regression.
    assert init.server_info.description, (
        "serverInfo.description must not be empty — fill it in main()/_server_description"
    )


def test_server_instructions_reach_the_client(handshake):
    init, _ = handshake
    assert init.instructions, "InitializeResult.instructions must be populated"
    # Both policy rules must be present: they are the contract every tool
    # description abbreviates.
    assert "Explicit request only" in init.instructions
    assert "review sub-flow" in init.instructions
    # The instructions must agree with the enforced limit, not hardcode one.
    if server._MAX_DEPTH <= 1:
        assert "hard check" in init.instructions
    else:
        assert str(server._MAX_DEPTH) in init.instructions
    # Guidance must be rendered from the live configuration: with nesting
    # disabled the text says so, and raising the limit changes it.
    original = server._MAX_DEPTH
    try:
        server._MAX_DEPTH = 3
        raised = server._server_instructions()
        assert "depth 3" in raised
        assert "hard check" not in raised
    finally:
        server._MAX_DEPTH = original


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_every_tool_description_is_non_empty(handshake, tool_name):
    _, tools = handshake
    tool = next(t for t in tools.tools if t.name == tool_name)
    assert tool.description, (
        f"{tool_name} reached clients with an empty description — this is the "
        "fb23201 regression: the v2 decorator reads fn.__doc__, so an "
        "implicit-concatenation docstring expression yields None"
    )


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_tool_description_carries_required_content(handshake, tool_name):
    _, tools = handshake
    tool = next(t for t in tools.tools if t.name == tool_name)
    lowered = tool.description.lower()
    for fragment in _REQUIRED_FRAGMENTS[tool_name]:
        assert fragment.lower() in lowered, (
            f"{tool_name} description lost {fragment!r}"
        )


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_tool_description_carries_nesting_hint(handshake, tool_name):
    _, tools = handshake
    tool = next(t for t in tools.tools if t.name == tool_name)
    lowered = tool.description.lower()
    for fragment in _NESTING_FRAGMENTS:
        assert fragment in lowered, (
            f"{tool_name} description lost the nesting hint fragment "
            f"{fragment!r}; the soft nudge is documented in the README as "
            "reaching downstream agents through the tool schema"
        )


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_tool_input_schema_still_documents_every_parameter(handshake, tool_name):
    _, tools = handshake
    tool = next(t for t in tools.tools if t.name == tool_name)
    schema = tool.input_schema
    for name, prop in schema["properties"].items():
        assert prop.get("description"), (
            f"{tool_name}.{name} lost its description; parameter docs are the "
            "only guidance for values like `brief` and `reviewer`"
        )


def test_reviewer_enum_is_pinned(handshake):
    """`reviewer` must stay a closed enum — the server dispatches on it."""
    _, tools = handshake
    props = next(
        t for t in tools.tools if t.name == "review_repo"
    ).input_schema["properties"]
    assert props["reviewer"]["enum"] == ["claude", "codex", "cursor"]


def test_instructions_tool_inventory_matches_the_wire(handshake):
    """`_TOOL_SUMMARIES` is hand-maintained; it must not drift from reality.

    Both directions are checked: an added tool missing from the instructions
    summary, and a stale summary entry for a tool that no longer exists.
    """
    init, tools = handshake
    on_the_wire = {t.name for t in tools.tools}
    summarised = set(server._TOOL_SUMMARIES)
    assert summarised == on_the_wire, (
        f"instructions summary {sorted(summarised)} != exposed tools "
        f"{sorted(on_the_wire)}"
    )
    for name in on_the_wire:
        assert name in init.instructions, f"{name} missing from instructions"
    for name, desc in server._TOOL_SUMMARIES.items():
        assert f"- {name}: {desc}" in init.instructions
