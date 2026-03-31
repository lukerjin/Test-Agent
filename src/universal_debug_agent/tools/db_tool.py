"""DB verification tool — runs a fresh DB agent to verify extracted UI data."""

from __future__ import annotations

import json
import logging
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Runner, RunConfig, Tool, function_tool
from agents.mcp import MCPServerStdio

from universal_debug_agent.observability.trace_recorder import ExecutionTraceRecorder

logger = logging.getLogger(__name__)

# Module-level state — configured by state_machine before the run starts
_db_mcp_servers: list[MCPServerStdio] = []
_model: Any = None
_max_turns: int = 15
_trace_recorder: ExecutionTraceRecorder | None = None


def configure(
    db_mcp_servers: list[MCPServerStdio],
    model: Any,
    max_turns: int = 15,
    trace_recorder: ExecutionTraceRecorder | None = None,
) -> None:
    """Configure the DB tool. Call this before running the UI agent."""
    global _db_mcp_servers, _model, _max_turns, _trace_recorder
    _db_mcp_servers = db_mcp_servers
    _model = model
    _max_turns = max_turns
    _trace_recorder = trace_recorder


class _DBHooks(RunHooks):
    """Lightweight hooks for the DB agent — logs to terminal and trace."""

    def __init__(self, trace_recorder: ExecutionTraceRecorder | None):
        self.trace_recorder = trace_recorder

    async def on_tool_start(
        self, context: RunContextWrapper, agent: Agent, tool: Tool
    ) -> None:
        tool_name = getattr(tool, "name", str(tool))
        tool_args = getattr(context, "tool_arguments", "") or ""
        logger.info(f"[db][action] {tool_name}({tool_args[:160]})")
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_start",
                f"[DB] Tool Start: {tool_name}",
                f"Args: {tool_args[:1000] or '(none)'}",
            )

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        tool_name = getattr(tool, "name", str(tool))
        result_str = str(result) if result else ""
        preview = result_str[:200].replace("\n", " ")
        logger.info(f"[db][result] {tool_name} -> {preview}")
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_end",
                f"[DB] Tool End: {tool_name}",
                f"Result:\n{result_str[:2000] or '(empty)'}",
            )


@function_tool
async def verify_in_db(data_json: str) -> str:
    """Verify business data in the database using key values extracted from the UI.

    Call this after completing UI steps that create or modify data, or at the end
    of the scenario as a final verification pass.

    Args:
        data_json: JSON object with key business values to verify, e.g.
            '{"order_id": "1234", "total": "268.45", "user_email": "test@example.com"}'

    Returns:
        JSON array of DataVerification results (check_name, query, expected,
        actual, status, severity). Include these in data_verifications when
        calling submit_report.
    """
    from universal_debug_agent.agents.db_agent import create_db_agent

    if not _db_mcp_servers:
        return json.dumps([{
            "check_name": "DB verification",
            "query": "",
            "expected": "",
            "actual": "DB tool not configured — no database MCP server available",
            "status": "blocked",
            "severity": "high",
        }])

    db_agent = create_db_agent(mcp_servers=_db_mcp_servers, model=_model)
    logger.info(f"[db] starting DB agent with data={data_json[:200]}")

    try:
        result = await Runner.run(
            db_agent,
            data_json,
            max_turns=_max_turns,
            hooks=_DBHooks(_trace_recorder),
            run_config=RunConfig(),
        )
        output = result.final_output
        if isinstance(output, str):
            return output
        return json.dumps(output)
    except Exception as e:
        logger.error(f"[db] DB agent error: {e}")
        return json.dumps([{
            "check_name": "DB verification",
            "query": "",
            "expected": "",
            "actual": f"DB agent error: {e}",
            "status": "blocked",
            "severity": "high",
        }])
