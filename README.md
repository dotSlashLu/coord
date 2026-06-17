# coord-review

An MCP server that lets one coding agent ask another coding agent (Claude,
Codex, or Cursor) to review its work — with **resumable follow-up sessions**.

The intended workflow:

1. You drive a coding agent (e.g. **Codex**) to write code in a chat.
2. Mid-conversation you say: *"have Claude review this; tell it to watch the
   prometheus label cardinality."*
3. Codex calls **`review_repo`** or **`review_file`** on this MCP server. The
   server runs `claude`, `codex`, or `cursor-agent` headlessly, streams
   progress back, and returns a structured review report plus an opaque
   `session_id`.
4. Codex decides which findings to act on. If something is unclear, it calls
   **`ask_reviewer`** with the same `session_id` to ask a follow-up — the
   reviewer keeps full prior context.

## What it exposes

Three MCP tools, all over stdio:

| Tool          | Purpose                                                    |
|---------------|------------------------------------------------------------|
| `review_repo` | Review a directory. Args: `reviewer`, `repo_dir`, `brief`. |
| `review_file` | Review one file (no project / git required). Args: `reviewer`, `file_path`, `brief`. |
| `ask_reviewer`| Follow up on an existing review. Args: `session_id`, `question`. |

`reviewer` is `"claude"`, `"codex"`, or `"cursor"`. The `brief` is a
free-form review request — describe the change's purpose, scope, and what to
focus on. The reviewer reads files itself; the brief is the contract.

> **Brief-writing rule of thumb (for the calling agent).** If you wrote the
> code yourself, your sense of what to review is biased toward what you
> already considered. Before composing the brief, **ask the user what they
> are worried about** — domain rules, recent incidents, regressions in code
> you didn't touch, deployment constraints — and weave their answers in.
> Skip the question only when the user gave explicit guidance in this turn
> or the change is so trivial that asking would be noise. The tool's
> parameter description in the MCP schema repeats this so downstream agents
> see it without reading the README.

`session_id` is an opaque handle of the form `cs_<uuid>`. It survives MCP
server restarts (state is on disk) and is stable even if the underlying CLI
rotates its native session id internally.

## Install

```bash
pip install -e .
# or
uv pip install -e .
```

You also need at least one of the reviewer CLIs on `PATH`:

- **Claude Code** (`claude`) — install per https://docs.claude.com/claude-code
- **Codex CLI** (`codex`) — install per https://developers.openai.com/codex
- **Cursor CLI** (`cursor-agent`) — `curl https://cursor.com/install -fsS | bash`

## Wiring into a coding agent

### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.coord-review]
command = "coord-review"
args = []
```

### Claude Code (`~/.claude.json` or `claude mcp add`)

```bash
claude mcp add coord-review -- coord-review
```

### Cursor (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "coord-review": {
      "command": "coord-review",
      "args": []
    }
  }
}
```

## Configuration

Environment variables, all optional:

| Variable                       | Default                  | Effect                                         |
|--------------------------------|--------------------------|------------------------------------------------|
| `COORD_REVIEW_HOME`            | `~/.coord-review`        | Where the session-id mapping is stored.        |
| `COORD_REVIEW_TIMEOUT`         | `600` (seconds)          | Wall-clock cap per reviewer call.              |
| `COORD_REVIEW_CLAUDE_MODEL`    | `sonnet`                 | Model passed to `claude --model`.              |
| `COORD_REVIEW_CODEX_MODEL`     | Codex CLI default        | Optional model passed to `codex --model`.      |
| `COORD_REVIEW_CODEX_SANDBOX`   | `read-only`              | Sandbox passed to `codex --sandbox`.           |
| `COORD_REVIEW_CURSOR_MODEL`    | `claude-4.6-opus-high-thinking` | Model passed to `cursor-agent --model`.        |
| `COORD_REVIEW_MAX_DEPTH`       | `1`                      | How deep reviewer spawning may nest (see [Nested-review guard](#nested-review-guard)). |

## Sandbox behavior — what each reviewer can actually do

This is asymmetric, and it is the reviewer CLIs' fault, not the server's.

### Claude reviewer
Launched with `--permission-mode acceptEdits` plus a hard `--allowedTools`
whitelist: `Read`, `Grep`, `Glob`, and a fixed set of read-only `Bash` rules
(`git diff:*`, `git log:*`, `git status:*`, `git show:*`, `git blame:*`,
`rg:*`, `ls:*`, `cat:*`, `head:*`, `tail:*`, `wc:*`, `file:*`, `find:*`).
No `Edit`, no `Write`, no general `Bash`.

### Codex reviewer
Launched as `codex --sandbox read-only --ask-for-approval never exec --json`
for initial reviews, and `codex exec resume --json` for follow-ups. Codex's
JSONL `thread.started.thread_id` is persisted as the native session id. We also
pass `--skip-git-repo-check` so `review_file` works for one-off files outside a
git repository.

The default `read-only` sandbox keeps this reviewer from editing files. If you
deliberately want a different Codex sandbox, set `COORD_REVIEW_CODEX_SANDBOX`
(for example `workspace-write`) and understand that this widens what the
reviewer can do.

### Cursor reviewer
Cursor's CLI doesn't have a per-tool allowlist. The realistic options are
`--plan` (no shell at all — too restrictive to run `git diff`) or `--force` /
`--yolo` (allow all). We use `--force`, scoped to the directory you pass via
`--workspace`. **Run only inside a workspace you'd be willing to trust.**

## Nested-review guard

There is a recursion hazard unique to this design. When you ask a coding agent
to review code and that agent (say Codex) is itself one of the reviewer CLIs,
the reviewer subprocess runs a *full* agent that can see the same MCP servers
its parent sees — including this one. So `review_repo` → reviewer subprocess →
the subprocess's agent calls `review_repo` again → spawns another reviewer → …
and you get an unbounded chain (`codex1` → `codex2` → `codex3` → …), i.e. a
fork bomb. The task semantics ("review this code") and the tool's purpose
overlap heavily, so a model can arrive at the recursive call quite "reasonably".

coord-review blocks this with two layers:

1. **Structural backstop (hard).** Every reviewer subprocess is spawned with
   `COORD_REVIEW_DEPTH` incremented in its environment. The coord-review server
   started *inside* that subprocess (Codex/Cursor pull their configured MCP
   servers into the child agent) therefore inherits a non-zero depth and
   **refuses to launch another reviewer** — `review_repo`, `review_file`, and
   `ask_reviewer` all raise before spawning anything. This is independent of
   what the model decides to do; the parent process set the boundary.
   - The Claude reviewer already had a hard tool whitelist (`--allowedTools`
     with no `mcp__coord-review__*`), so it physically cannot recurse — the env
     check is the equivalent backstop for the Codex and Cursor paths.
2. **Tool-description hint (soft).** Each tool's docstring tells the calling
   agent to only invoke it when the user explicitly asked for a coord-review
   review, and never from inside a review sub-flow. This steers the *top-level*
   agent (the one with a real user in the loop) away from fanning out
   proactively; layer 1 catches anything that ignores it.

`COORD_REVIEW_MAX_DEPTH` (default `1`) controls the cutoff. `1` means only the
top-level server may spawn a reviewer — any nested server refuses. Raise it if
you genuinely want controlled nesting, but be aware that each extra level is
another agent that can spawn more, so the fork-bomb risk grows fast.

## Caveats

- The session mapping at `~/.coord-review/sessions.json` is **not** secret.
  Anyone with shell access on this machine can read it and resume the
  underlying CLI session directly.
- Claude rotates Code transcripts after `cleanupPeriodDays` (30 days by
  default). Stale `cs_*` handles will eventually fail to resume on Claude;
  `ask_reviewer` returns a clear error in that case.
- Cursor's headless mode requires `--trust` for untrusted workspaces; we
  always pass it. If you don't want that, don't run this server.

## Manual smoke test

```bash
mkdir -p /tmp/coord-smoke
cat > /tmp/coord-smoke/foo.py <<'PY'
def total(items):
    s = 0
    for i in range(len(items) + 1):   # off-by-one
        s += items[i]
    return s
PY

# Inspect the server with the official MCP inspector
npx @modelcontextprotocol/inspector coord-review
# → call review_file(reviewer="claude", file_path="/tmp/coord-smoke/foo.py",
#                     brief="check correctness; we want sum(items)")
# → note the returned session_id
# → call ask_reviewer(session_id=<that>, question="what line exactly?")
```

## Tests

```bash
pip install -e ".[dev]"  # if you add a dev extra; otherwise:
pip install pytest
pytest
```

The test suite covers the on-disk session store. Reviewer adapters are
exercised by manual smoke runs (above) — they're thin shims over external
CLIs and gain little from heavily-mocked unit tests.

## Out of scope (v1)

- Multi-reviewer fan-out / consensus voting. Trivially built on top of the
  three tools above without changing the protocol.
- Remote MCP transport. Stdio only.
