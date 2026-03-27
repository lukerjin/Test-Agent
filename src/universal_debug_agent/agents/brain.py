"""Agent Brain — single agent with ReAct and Analysis modes."""

from __future__ import annotations

from typing import Any

from agents import Agent, ModelSettings
from agents.mcp import MCPServerStdio

from universal_debug_agent.agents.prompts import build_analysis_prompt, build_react_prompt
from universal_debug_agent.schemas.profile import ProjectProfile
from universal_debug_agent.schemas.report import InvestigationReport
from universal_debug_agent.tools.code_tools import grep_code, list_directory, read_file
from universal_debug_agent.tools.report_tool import submit_report


def create_brain_agent(
    profile: ProjectProfile,
    mcp_servers: list[MCPServerStdio],
    model: Any = "gpt-4o",
    mode: str = "react",
    evidence_summary: str = "",
    memory_context: str = "",
) -> Agent:
    """Create the debug agent in the given mode.

    Args:
        profile: The project profile with context and boundaries.
        mcp_servers: List of MCP servers (Playwright, DB, etc.).
        model: Model string or OpenAIChatCompletionsModel instance.
        mode: "react" for normal investigation, "analysis" for deep reasoning.
        evidence_summary: Collected evidence text (only used in analysis mode).
        memory_context: Formatted past investigation memory for prompt injection.
    """
    if mode == "react":
        instructions = build_react_prompt(profile, memory_context=memory_context)
        tools = [read_file, grep_code, list_directory, submit_report]
        output_type = None
        temperature = 0.2
    else:
        instructions = build_analysis_prompt(profile, evidence_summary, memory_context=memory_context)
        tools = [submit_report]
        output_type = InvestigationReport
        temperature = 0.7

    return Agent(
        name="DebugBrain",
        instructions=instructions,
        mcp_servers=mcp_servers,
        tools=tools,
        output_type=output_type,
        model=model,
        model_settings=ModelSettings(temperature=temperature),
    )
