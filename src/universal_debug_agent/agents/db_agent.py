"""DB verification agent — verifies extracted UI data against the database."""

from __future__ import annotations

from typing import Any

from agents import Agent, AgentOutputSchema, ModelSettings
from agents.mcp import MCPServerStdio
from pydantic import BaseModel

from universal_debug_agent.schemas.report import DataVerification

DB_MAX_TURNS = 8


class DBVerificationOutput(BaseModel):
    verifications: list[DataVerification]


_DB_PROMPT = """You are a database verification agent. You receive key business values extracted from a UI test (order IDs, amounts, user emails, etc.) and verify them in the database.

## How to work (IMPORTANT — follow this exactly)

**Step 1 — Plan**: Read the input JSON. Decide which 2-3 high-value SQL queries to run. Write them all out mentally before calling any tools.

**Step 2 — Execute all queries in ONE turn**: Call all query tools in a single response. Do not wait for one result before deciding the next query — emit all tool calls at once.

**Step 3 — Output**: After receiving all results, output the final DBVerificationOutput immediately.

This 3-step approach means you should finish in 3 LLM turns. Do not add extra exploration turns.

## Rules
- Only SELECT queries — never INSERT, UPDATE, DELETE, DROP
- 2-3 checks maximum — focus on the most critical business facts
- If a check cannot be completed, set status="blocked" and explain in "actual"
- status: pass | fail | blocked
- severity: high | medium | low
"""


def create_db_agent(
    mcp_servers: list[MCPServerStdio],
    model: Any = None,
    schema_cache: str = "",
) -> Agent:
    """Create a focused DB verification agent with no UI tools."""
    instructions = _DB_PROMPT + ("\n" + schema_cache if schema_cache else "")
    return Agent(
        name="DBVerifier",
        instructions=instructions,
        mcp_servers=mcp_servers,
        model=model,
        model_settings=ModelSettings(temperature=0.1),
        output_type=AgentOutputSchema(DBVerificationOutput, strict_json_schema=False),
    )
