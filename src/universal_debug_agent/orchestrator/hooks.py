"""RunHooks implementation — monitors tool calls and detects stuck state."""

from __future__ import annotations

import json
import hashlib
import logging
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Tool
from agents.exceptions import UserError
from universal_debug_agent.observability.trace_recorder import ExecutionTraceRecorder

logger = logging.getLogger(__name__)


class SwitchToAnalysisMode(Exception):
    """Raised by hooks when the agent is detected as stuck."""

    def __init__(self, evidence_summary: str, reason: str):
        self.evidence_summary = evidence_summary
        self.reason = reason
        super().__init__(reason)


class InvestigationHooks(RunHooks):
    """Hooks that track tool calls and trigger mode switch when stuck."""

    def __init__(
        self,
        stuck_detector: Any,
        evidence_collector: Any,
        trace_recorder: ExecutionTraceRecorder | None = None,
    ):
        self.stuck_detector = stuck_detector
        self.evidence_collector = evidence_collector
        self.trace_recorder = trace_recorder

    async def on_llm_end(
        self, context: RunContextWrapper, agent: Agent, response
    ) -> None:
        if self.trace_recorder is not None:
            self.trace_recorder.record_llm_response(response)

    async def on_tool_start(
        self, context: RunContextWrapper, agent: Agent, tool: Tool
    ) -> None:
        tool_name = getattr(tool, "name", str(tool))

        self._apply_playwright_defaults(context, tool_name)
        self._validate_playwright_click_args(context, tool_name)
        tool_args = self._tool_args(context, tool)

        self.stuck_detector.record(tool_name, tool_args)
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_start",
                f"Tool Start: {tool_name}",
                f"Args: {tool_args[:1000] or '(none)'}",
            )
        logger.debug(f"Tool start: {tool_name}({tool_args[:100]})")

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        result_str = str(result) if result else ""
        result_hash = hashlib.md5(result_str.encode()).hexdigest()

        tool_name = getattr(tool, "name", str(tool))
        tool_args = self._tool_args(context, tool)

        self.stuck_detector.update_last_result(result_hash)
        self.evidence_collector.collect(tool_name, tool_args, result_str)
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_end",
                f"Tool End: {tool_name}",
                f"Args: {tool_args[:1000] or '(none)'}\n\nResult:\n{result_str[:2000] or '(empty)'}",
            )

        logger.debug(f"Tool end: {tool_name} -> {result_hash}")

        if self.stuck_detector.is_stuck():
            reason = self.stuck_detector.stuck_reason()
            logger.warning(f"Agent stuck detected: {reason}")
            if self.trace_recorder is not None:
                self.trace_recorder.record(
                    "mode_switch",
                    "Switch To Analysis",
                    reason,
                )
            raise SwitchToAnalysisMode(
                evidence_summary=self.evidence_collector.build_summary(),
                reason=reason,
            )

    def _tool_args(self, context: RunContextWrapper, tool: Tool) -> str:
        if hasattr(context, "tool_arguments"):
            return getattr(context, "tool_arguments") or "(none)"
        return str(getattr(tool, "args", "")) or "(none)"

    def _parse_tool_args(self, context: RunContextWrapper) -> dict[str, Any] | None:
        if not hasattr(context, "tool_arguments"):
            return None
        try:
            parsed = json.loads(getattr(context, "tool_arguments"))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _write_tool_args(self, context: RunContextWrapper, args: dict[str, Any]) -> None:
        if not hasattr(context, "tool_arguments"):
            return
        payload = json.dumps(args, ensure_ascii=False)
        context.tool_arguments = payload
        if getattr(context, "tool_call", None) is not None:
            context.tool_call.arguments = payload

    def _apply_playwright_defaults(self, context: RunContextWrapper, tool_name: str) -> None:
        if tool_name != "browser_take_screenshot":
            return
        args = self._parse_tool_args(context)
        if args is None:
            return
        if not args.get("type"):
            args["type"] = "png"
            self._write_tool_args(context, args)

    def _validate_playwright_click_args(self, context: RunContextWrapper, tool_name: str) -> None:
        if tool_name != "browser_click":
            return
        args = self._parse_tool_args(context)
        if args is None:
            return

        candidates = [
            args.get("selector"),
            args.get("locator"),
            args.get("element"),
            args.get("target"),
        ]
        candidate_text = " ".join(str(v).strip() for v in candidates if isinstance(v, str)).lower()
        if not candidate_text:
            return

        ambiguous_patterns = {
            "button",
            "form button",
            "form >> button",
            "locator('form').getbyrole('button')",
            'locator("form").getbyrole("button")',
        }
        if candidate_text in ambiguous_patterns:
            raise UserError(
                "Ambiguous browser_click target blocked. Use a named button, stable selector, "
                "or refreshed page snapshot before clicking."
            )
