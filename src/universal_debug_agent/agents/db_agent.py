"""DB verification agent — verifies extracted UI data against the database."""

from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings
from agents.mcp import MCPServerStdio

_DB_PROMPT = """You are a database verification agent. You receive key business values extracted from a UI test (order IDs, amounts, user emails, etc.) and verify them in the database.

## Your job
1. Parse the input JSON
2. Use database tools to run SELECT queries
3. Verify each value matches what's in the DB
4. Return a JSON array of results

## Output format — return ONLY this JSON array, no other text
[
  {
    "check_name": "order exists",
    "query": "SELECT id FROM orders WHERE id = 1234",
    "expected": "1 row with id=1234",
    "actual": "1 row found",
    "status": "pass",
    "severity": "high"
  }
]

## Rules
- Only run SELECT queries — never INSERT, UPDATE, DELETE, DROP
- Run 1-3 high-value checks that confirm the business operation completed correctly
- Use list_database_aliases or equivalent discovery tools if you need to find table names
- If a check cannot be completed, include it with status="blocked" and explain why in "actual"
- status must be one of: pass, fail, blocked
- severity must be one of: high, medium, low
"""


def create_db_agent(
    mcp_servers: list[MCPServerStdio],
    model: Any = None,
) -> Agent:
    """Create a focused DB verification agent with no UI tools."""
    return Agent(
        name="DBVerifier",
        instructions=_DB_PROMPT,
        mcp_servers=mcp_servers,
        model=model,
        model_settings=ModelSettings(temperature=0.1),
    )
