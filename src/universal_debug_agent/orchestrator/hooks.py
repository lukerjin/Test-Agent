"""RunHooks implementation — monitors tool calls and detects stuck state."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Tool

logger = logging.getLogger(__name__)


class SwitchToAnalysisMode(Exception):
    """Raised by hooks when the agent is detected as stuck."""

    def __init__(self, evidence_summary: str, reason: str):
        self.evidence_summary = evidence_summary
        self.reason = reason
        super().__init__(reason)


class InvestigationHooks(RunHooks):
    """Hooks that track tool calls and trigger mode switch when stuck."""

    def __init__(self, stuck_detector: Any, evidence_collector: Any):
        self.stuck_detector = stuck_detector
        self.evidence_collector = evidence_collector

    async def on_tool_start(
        self, context: RunContextWrapper, agent: Agent, tool: Tool
    ) -> None:
        tool_args = str(getattr(tool, "args", ""))
        tool_name = getattr(tool, "name", str(tool))
        self.stuck_detector.record(tool_name, tool_args)
        logger.debug(f"Tool start: {tool_name}({tool_args[:100]})")

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        result_str = str(result) if result else ""
        result_hash = hashlib.md5(result_str.encode()).hexdigest()

        tool_name = getattr(tool, "name", str(tool))
        tool_args = str(getattr(tool, "args", ""))

        self.stuck_detector.update_last_result(result_hash)
        self.evidence_collector.collect(tool_name, tool_args, result_str)

        logger.debug(f"Tool end: {tool_name} -> {result_hash}")

        if self.stuck_detector.is_stuck():
            reason = self.stuck_detector.stuck_reason()
            logger.warning(f"Agent stuck detected: {reason}")
            raise SwitchToAnalysisMode(
                evidence_summary=self.evidence_collector.build_summary(),
                reason=reason,
            )
