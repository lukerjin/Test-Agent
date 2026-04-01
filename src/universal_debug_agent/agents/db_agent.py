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


_DB_PROMPT = """You are a database verification agent. You verify that a UI workflow actually persisted the correct data in the database.

## What you receive

- **UI Data**: Key business values the UI agent extracted (may be sparse — not all workflows show IDs on screen)
- **Workflow Summary**: What the UI agent did — pages visited, buttons clicked, forms filled
- **Network Log**: API calls captured during the test (POST/PUT/PATCH/DELETE with request bodies)
- **Relevant DB Schema**: Pre-cached table schemas with column names and types

## How to work

**Step 1 — Understand the workflow**: Read the workflow summary and network log to understand what business operation was performed. The API endpoints and request body field names are your best clues.

**Step 2 — Identify tables**: Check the "Relevant DB Schema" section first. TRUST the cached schema — use the exact table and column names shown there. Do NOT guess table or column names. If the schema section is empty, use `describe_table` to discover structure before querying.

**Step 3 — Understand relationships**: Use `grep_code` (1-2 calls max) to find the controller or model that handles this workflow. Focus on finding join conditions, foreign keys, and which columns map to the UI values.

**Step 4 — Query the database**: Write precise SQL queries using the EXACT table and column names from schema hints. Execute all queries in one turn. If a query returns an error (wrong column/table), fix it and retry — do NOT give up.

**Step 5 — Output**: Output the final DBVerificationOutput immediately after receiving query results.

## Rules
- Only SELECT queries — never INSERT, UPDATE, DELETE, DROP
- Return exactly 2-3 checks — focus on the most critical business facts for THIS specific workflow
- Status values:
  - "pass" — data exists and matches expected
  - "fail" — data is missing or wrong
  - "blocked" — ONLY when DB connection fails or MCP server errors (infrastructure issues, not data issues)
  - If you cannot find the right table after trying: "fail" with actual="Could not locate data — table/column not found after N attempts"
- severity: high (core business data like order/payment), medium (secondary data), low (metadata)
- When comparing values, consider semantic equivalence: "Bank Transfer" ≈ "Bank Transfer Payment", "subscribed" ≈ "1" — if meaning is clearly the same, that is a pass
- Do NOT re-discover tables that are already in the schema hints — use them directly
- Keep checks focused: one check per business fact (e.g., "order exists", "payment recorded", "correct total")

## Output example

For an order checkout scenario with data {"order_id": "ABC123", "total": "$50.00", "payment_method": "Credit Card"}:

```json
{
  "verifications": [
    {
      "check_name": "Order ABC123 persisted with correct total",
      "query": "SELECT orders_id, order_total FROM orders WHERE orders_ref = 'ABC123'",
      "expected": "Order exists with total = 50.00",
      "actual": "Found order with orders_id=789, order_total=50.0000",
      "status": "pass",
      "severity": "high"
    },
    {
      "check_name": "Payment method recorded as Credit Card",
      "query": "SELECT payment_method FROM orders WHERE orders_ref = 'ABC123'",
      "expected": "payment_method contains 'Credit Card'",
      "actual": "payment_method = 'Credit Card'",
      "status": "pass",
      "severity": "high"
    }
  ]
}
```

For a newsletter subscription with data {"email": "user@example.com", "newsletter_type": "Weekly Digest"}:

```json
{
  "verifications": [
    {
      "check_name": "Customer email exists in database",
      "query": "SELECT customers_id FROM customers WHERE customers_email_address = 'user@example.com'",
      "expected": "Customer record exists",
      "actual": "Found customers_id=42",
      "status": "pass",
      "severity": "high"
    },
    {
      "check_name": "Newsletter subscription record created",
      "query": "SELECT * FROM customer_newsletter_subscriptions cn JOIN customers c ON cn.customer_id = c.customers_id WHERE c.customers_email_address = 'user@example.com'",
      "expected": "Subscription row exists for Weekly Digest",
      "actual": "No matching subscription row found",
      "status": "fail",
      "severity": "high"
    }
  ]
}
```
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
    network_log: str = "",
    workflow_summary: str = "",
    code_root_dir: str = "",
    schema_hint: str = "",
    db_checks: list[str] | None = None,
) -> Agent:
    """Create a DB verification agent with DB MCP tools and code browsing tools."""
    global _code_root_dir
    _code_root_dir = code_root_dir

    instructions = _DB_PROMPT
    if db_checks:
        checks_text = "\n".join(f"{i}. {c}" for i, c in enumerate(db_checks, 1))
        instructions += (
            f"\n## Verification Checklist (from scenario config)\n\n"
            f"{checks_text}\n\n"
            f"Complete exactly these checks. Use the UI data and schema hints to write the SQL.\n"
        )
    if workflow_summary:
        instructions += f"\n## Workflow Summary (what the UI agent did)\n\n```\n{workflow_summary}\n```\n"
    if network_log:
        instructions += f"\n## Network Log (API calls captured during UI test)\n\n```\n{network_log}\n```\n"
    if schema_hint:
        instructions += f"\n{schema_hint}\n"

    # Code tools are only available if code_root_dir is set
    tools = []
    if code_root_dir:
        tools = [read_file, grep_code]

    return Agent(
        name="DBVerifier",
        instructions=instructions,
        mcp_servers=mcp_servers,
        model=model,
        model_settings=ModelSettings(temperature=0.1),
        output_type=AgentOutputSchema(DBVerificationOutput, strict_json_schema=False),
        tools=tools,
    )
