"""DB verification agent — verifies extracted UI data against the database."""

from __future__ import annotations

from typing import Any

from agents import Agent, AgentOutputSchema, ModelSettings, function_tool
from agents.mcp import MCPServerStdio
from pydantic import BaseModel

from universal_debug_agent.schemas.report import DataVerification

DB_MAX_TURNS = 12


class DBVerificationOutput(BaseModel):
    verifications: list[DataVerification]


_DB_PROMPT_FOCUSED = """You are a database verification agent.

## What you receive

- **Verification Checklist**: Exact checks to perform (from scenario config)
- **Live Schema**: Target table definitions with column names and types
- **Network Log**: Mutation requests (POST/PUT/PATCH/DELETE) with request bodies captured during UI test
- **UI Data** (user message): Business values visible on the page

## Steps

1. Read the checklist — each item tells you what table and condition to verify.
2. Extract precise values from the network log request bodies (e.g. order_ref, payment_method_id) to use as WHERE conditions.
3. If a column stores coded values (e.g. status=1), use `grep_code` to find the enum/constant definition in the codebase.
4. Write SELECT queries using the EXACT column names from live schema.
5. Execute queries. If a query errors, fix it and retry — do NOT give up.
6. Compare results to expected values and output DBVerificationOutput.

## Rules

- Only SELECT — never INSERT, UPDATE, DELETE, DROP.
- Complete ONLY the checklist checks — no more, no less.
- Status: "pass" (matches), "fail" (missing/wrong), "blocked" (DB connection/MCP error only).
- Severity: "high" (core business data), "medium" (secondary), "low" (metadata).
- Semantic equivalence is a pass: "Bank Transfer" ≈ "bank_transfer", "subscribed" ≈ "1".
- One check per business fact.
"""


# Sandboxed code tools for the DB agent
_code_root_dir: str = ""


def _safe_path(relative: str):
    """Resolve a relative path and ensure it stays within root_dir."""
    from pathlib import Path

    if not _code_root_dir:
        return None
    root = Path(_code_root_dir).resolve()
    target = (root / relative).resolve()
    if not str(target).startswith(str(root)):
        return None
    return target


@function_tool
def read_file(path: str, start_line: int = 1, end_line: int = 100) -> str:
    """Read lines from a code file in the project.

    Args:
        path: Relative path from the project root.
        start_line: First line to read (1-based).
        end_line: Last line to read (1-based, max 100 lines per call).
    """
    target = _safe_path(path)
    if target is None:
        return "Error: code root not configured or path invalid"
    if not target.is_file():
        return f"Error: not a file: {path}"

    end_line = min(end_line, start_line + 99)
    lines = target.read_text(errors="replace").splitlines()
    selected = lines[start_line - 1: end_line]
    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=start_line)]
    return f"# {path} (lines {start_line}-{min(end_line, len(lines))} of {len(lines)})\n" + "\n".join(numbered)


@function_tool
def grep_code(pattern: str, directory: str = "", file_glob: str = "*") -> str:
    """Search for a pattern in project code files.

    Args:
        pattern: Regex pattern to search for.
        directory: Subdirectory to search in (relative to project root). Empty = entire root.
        file_glob: File glob pattern, e.g. '*.php', '*.py'. Default '*' matches all.
    """
    import shutil
    import subprocess
    from pathlib import Path
    from collections import defaultdict

    if not _code_root_dir:
        return "Error: code root not configured"

    root = Path(_code_root_dir).resolve()
    search_dir = _safe_path(directory) if directory else root
    if search_dir is None or not search_dir.is_dir():
        return f"Error: not a directory: {directory}"

    rg = shutil.which("rg")
    grep = shutil.which("grep")
    if rg:
        cmd = [rg, "--line-number", "--with-filename", "--glob", file_glob, "--regexp", pattern, str(search_dir)]
    elif grep:
        cmd = [grep, "-rnE", "--include", file_glob, pattern, str(search_dir)]
    else:
        return "Error: neither rg nor grep available"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "Error: search timed out"

    lines = result.stdout.strip().splitlines()
    if not lines:
        return f"No matches for pattern: {pattern}"

    root_str = str(root)
    grouped: dict[str, list[str]] = defaultdict(list)
    for line in lines:
        normalized = line.replace(root_str + "/", "")
        file_path, sep, remainder = normalized.partition(":")
        if sep:
            grouped[file_path].append(remainder)

    max_files = 6
    max_matches = 2
    output_lines = [f"# grep: {pattern}", f"# matched {len(grouped)} files"]
    for fp in sorted(grouped)[:max_files]:
        matches = grouped[fp]
        output_lines.append(f"\n- {fp} ({len(matches)} matches)")
        for m in matches[:max_matches]:
            line_no, _, text = m.partition(":")
            output_lines.append(f"  {line_no}: {text.strip()[:140]}")
        if len(matches) > max_matches:
            output_lines.append(f"  ... {len(matches) - max_matches} more")

    result_str = "\n".join(output_lines)
    return result_str[:2500]


def create_db_agent(
    mcp_servers: list[MCPServerStdio],
    model: Any = None,
    db_checks: list[str] | None = None,
    live_schema: str = "",
    network_log: str = "",
    code_root_dir: str = "",
) -> Agent:
    """Create a DB verification agent with DB MCP tools and optional code search."""
    global _code_root_dir
    _code_root_dir = code_root_dir

    instructions = _DB_PROMPT_FOCUSED
    if db_checks:
        checks_text = "\n".join(f"{i}. {c}" for i, c in enumerate(db_checks, 1))
        instructions += (
            f"\n## Verification Checklist (from scenario config)\n\n"
            f"{checks_text}\n"
        )
    if live_schema:
        instructions += f"\n{live_schema}\n"
    if network_log:
        instructions += f"\n## Network Log (API calls captured during UI test)\n\n```\n{network_log}\n```\n"

    tools = [grep_code] if code_root_dir else []

    return Agent(
        name="DBVerifier",
        instructions=instructions,
        mcp_servers=mcp_servers,
        model=model,
        model_settings=ModelSettings(temperature=0.1),
        output_type=AgentOutputSchema(DBVerificationOutput, strict_json_schema=False),
        tools=tools,
    )
