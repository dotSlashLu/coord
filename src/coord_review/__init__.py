"""coord-review — an MCP server that wraps coding-agent CLIs as code reviewers.

Exposes three tools to MCP clients:
  - review_repo(reviewer, repo_dir, brief)
  - review_file(reviewer, file_path, brief)
  - ask_reviewer(session_id, question)

The first two return an opaque ``session_id`` that ``ask_reviewer`` resumes.
"""

__version__ = "0.1.0"
