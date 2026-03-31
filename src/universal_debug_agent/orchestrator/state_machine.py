"""State machine — StuckDetector, evidence collector, and orchestrator."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agents import RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.mcp import MCPServerStdio

from pathlib import Path

from universal_debug_agent.agents.brain import create_brain_agent
from universal_debug_agent.tools import db_tool
from universal_debug_agent.memory.lesson import generate_lesson
from universal_debug_agent.orchestrator.hooks import InvestigationHooks, SwitchToAnalysisMode
from universal_debug_agent.orchestrator.input_filters import MCPToolOutputFilter
from universal_debug_agent.observability.llm_usage import LLMUsageTracker
from universal_debug_agent.observability.trace_recorder import ExecutionTraceRecorder
from universal_debug_agent.schemas.profile import ProjectProfile
from universal_debug_agent.schemas.report import (
    ReportMetadata,
    StepStatus,
    ScenarioReport,
)

logger = logging.getLogger(__name__)


class InvestigationState(Enum):
    REACT = "react"
    ANALYZING = "analyzing"
    DONE = "done"


@dataclass
class ToolCall:
    name: str
    args: str
    result_hash: str = ""


class StuckDetector:
    """Deterministic stuck detection based on tool call history."""

    REPEAT_THRESHOLD = 3
    SAME_RESULT_WINDOW = 5

    def __init__(self, max_steps: int, stuck_budget_ratio: float = 0.85):
        self.max_steps = max_steps
        self.stuck_budget_ratio = stuck_budget_ratio
        self.history: list[ToolCall] = []
        self._stuck_reason: str = ""

    @property
    def step_count(self) -> int:
        return len(self.history)

    def record(self, tool_name: str, tool_args: str) -> None:
        self.history.append(ToolCall(name=tool_name, args=tool_args))

    def update_last_result(self, result_hash: str) -> None:
        if self.history:
            self.history[-1].result_hash = result_hash

    def is_stuck(self) -> bool:
        self._stuck_reason = ""

        # Rule 1: consecutive identical tool calls
        if len(self.history) >= self.REPEAT_THRESHOLD:
            recent = self.history[-self.REPEAT_THRESHOLD :]
            signatures = [(tc.name, tc.args) for tc in recent]
            if len(set(signatures)) == 1:
                self._stuck_reason = (
                    f"Repeated identical tool call {self.REPEAT_THRESHOLD} times: "
                    f"{recent[0].name}({recent[0].args[:80]})"
                )
                return True

        # Rule 2: last N results all identical
        if len(self.history) >= self.SAME_RESULT_WINDOW:
            recent = self.history[-self.SAME_RESULT_WINDOW :]
            hashes = [tc.result_hash for tc in recent if tc.result_hash]
            if len(hashes) == self.SAME_RESULT_WINDOW and len(set(hashes)) == 1:
                self._stuck_reason = (
                    f"Last {self.SAME_RESULT_WINDOW} tool calls returned identical results"
                )
                return True

        # Rule 3: used > 70% of budget with no report submitted
        budget_threshold = int(self.max_steps * self.stuck_budget_ratio)
        if self.step_count > budget_threshold:
            has_report = any(tc.name == "submit_report" for tc in self.history)
            if not has_report:
                self._stuck_reason = (
                    f"Used {self.step_count}/{self.max_steps} steps "
                    f"without submitting a report"
                )
                return True

        return False

    def stuck_reason(self) -> str:
        return self._stuck_reason


@dataclass
class EvidenceCollector:
    """Collects evidence from tool calls during test execution."""

    items: list[dict] = field(default_factory=list)

    def collect(self, tool_name: str, tool_args: str, result: str) -> None:
        self.items.append({
            "tool": tool_name,
            "args": tool_args,
            "result_preview": result[:500] if result else "",
        })

    def build_summary(self) -> str:
        if not self.items:
            return "No evidence collected."

        parts: list[str] = []
        for i, item in enumerate(self.items, 1):
            parts.append(
                f"### Step #{i}: {item['tool']}\n"
                f"**Args**: {item['args'][:200]}\n"
                f"**Result**: {item['result_preview']}\n"
            )
        return "\n".join(parts)


class InvestigationOrchestrator:
    """Main orchestrator — runs single agent in two modes."""

    def __init__(
        self,
        profile: ProjectProfile,
        mcp_servers: list[MCPServerStdio],
        model: Any = None,
        memory_context: str = "",
        usage_tracker: LLMUsageTracker | None = None,
        trace_recorder: ExecutionTraceRecorder | None = None,
    ):
        self.profile = profile
        self.mcp_servers = mcp_servers
        self.model = model
        self.memory_context = memory_context
        self.usage_tracker = usage_tracker
        self.trace_recorder = trace_recorder
        self.last_raw_output_path: str = ""
        self.last_error_output_path: str = ""
        self.last_lesson: str = ""
        self.last_lesson_tags: list[str] = []
        self.state = InvestigationState.REACT
        self.stuck_detector = StuckDetector(
            max_steps=profile.boundaries.max_steps,
            stuck_budget_ratio=profile.boundaries.stuck_budget_ratio,
        )
        self.evidence_collector = EvidenceCollector()
        self._run_config = RunConfig(
            call_model_input_filter=MCPToolOutputFilter(
                snapshot_dir=self._find_playwright_cwd(mcp_servers),
            )
        )

        # Identify DB MCP servers: prefer explicit role="database", fall back to name-based detection
        db_server_names = {
            name for name, cfg in profile.mcp_servers.items()
            if cfg.role == "database" or (cfg.role is None and "database" in name.lower())
        }
        db_servers = [s for s in mcp_servers if s.name in db_server_names]
        db_tool.configure(
            db_mcp_servers=db_servers,
            model=model,
            max_turns=profile.boundaries.max_turns,
            trace_recorder=trace_recorder,
        )

    @staticmethod
    def _find_playwright_cwd(mcp_servers: list[MCPServerStdio]) -> Path | None:
        """Return the working directory of the playwright MCP server, if any."""
        for server in mcp_servers:
            if server.name == "playwright":
                cwd = getattr(server.params, "cwd", None)
                if cwd:
                    return Path(cwd)
        return None


    async def run(self, scenario: str) -> ScenarioReport:
        """Run the full test execution pipeline."""

        # Connect all MCP servers before running
        for server in self.mcp_servers:
            try:
                await server.connect()
                logger.info(f"Connected MCP server: {server.name}")
            except Exception as e:
                logger.error(f"Failed to connect MCP server {server.name}: {e}")
                raise

        try:
            report = await self._run_pipeline(scenario)
            # Generate lesson BEFORE cleanup so anyio scopes from MCP servers are still clean
            self.last_lesson, self.last_lesson_tags = await generate_lesson(report, scenario, model=self.model)
            return report
        except BaseException as e:
            if self.usage_tracker is not None:
                self.usage_tracker.record_error(e, phase=self.state.value)
            if self.usage_tracker is not None:
                error_path = self.usage_tracker.write_error_output(e)
                self.last_error_output_path = str(error_path) if error_path else ""
            raise
        finally:
            # Disconnect all MCP servers (suppress all errors to preserve original exception)
            for server in self.mcp_servers:
                try:
                    await server.cleanup()
                except BaseException as e:
                    logger.warning(f"Error cleaning up MCP server {server.name}: {e}")

    async def _run_pipeline(self, scenario: str) -> ScenarioReport:
        """Internal pipeline after MCP servers are connected."""

        # Phase 1: ReAct mode — execute the test scenario
        self.state = InvestigationState.REACT
        logger.info("Phase: UI execution")

        hooks = InvestigationHooks(
            stuck_detector=self.stuck_detector,
            evidence_collector=self.evidence_collector,
            trace_recorder=self.trace_recorder,
        )

        react_agent = create_brain_agent(
            profile=self.profile,
            mcp_servers=self.mcp_servers,
            model=self.model,
            mode="react",
            memory_context=self.memory_context,
        )

        try:
            result = await Runner.run(
                react_agent,
                scenario,
                max_turns=self.profile.boundaries.max_turns,
                hooks=hooks,
                run_config=self._run_config,
            )
            if self.usage_tracker is not None:
                self.usage_tracker.record_run_result(result, phase="react")
                raw_path = self.usage_tracker.write_final_output(result.final_output)
                self.last_raw_output_path = str(raw_path) if raw_path else ""

            # Try to parse the output as a report
            return self._extract_report(result)

        except SwitchToAnalysisMode as e:
            logger.info(f"Switching to analysis mode: {e.reason}")
            return await self._run_analysis(scenario, e.evidence_summary)
        except MaxTurnsExceeded as e:
            reason = str(e)
            logger.warning(f"Switching to analysis mode: {reason}")
            if self.trace_recorder is not None:
                self.trace_recorder.record("mode_switch", "Switch To Analysis", reason)
            return await self._run_analysis(scenario, self.evidence_collector.build_summary())
        except BaseException as e:
            mode_switch = self._unwrap_mode_switch(e)
            if mode_switch is not None:
                logger.info(f"Switching to analysis mode: {mode_switch.reason}")
                if self.trace_recorder is not None:
                    self.trace_recorder.record("mode_switch", "Switch To Analysis", mode_switch.reason)
                return await self._run_analysis(scenario, mode_switch.evidence_summary)
            raise

    async def _run_analysis(self, scenario: str, evidence_summary: str) -> ScenarioReport:
        """Phase 2: Analysis mode — analyze what happened when agent got stuck."""
        self.state = InvestigationState.ANALYZING
        logger.info("Phase: Analysis")

        analysis_agent = create_brain_agent(
            profile=self.profile,
            mcp_servers=self.mcp_servers,
            model=self.model,
            mode="analysis",
            evidence_summary=evidence_summary,
            memory_context=self.memory_context,
        )

        analysis_input = (
            f"## Test Scenario\n{scenario}\n\n"
            f"## Execution Context\n"
            f"The test execution agent was stopped because it appeared stuck. "
            f"Please analyze the execution log and produce a final test report.\n\n"
            f"Total steps taken: {self.stuck_detector.step_count}"
        )
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "analysis_start",
                "Analysis Mode",
                analysis_input[:2000],
            )

        result = await Runner.run(
            analysis_agent,
            analysis_input,
            max_turns=self.profile.boundaries.max_turns,
            run_config=self._run_config,
        )
        if self.usage_tracker is not None:
            self.usage_tracker.record_run_result(result, phase="analysis")
            raw_path = self.usage_tracker.write_final_output(result.final_output)
            self.last_raw_output_path = str(raw_path) if raw_path else ""
        return self._extract_report(result)

    def _unwrap_mode_switch(self, error: BaseException) -> SwitchToAnalysisMode | None:
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            if isinstance(current, SwitchToAnalysisMode):
                return current
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return None

    def _extract_report(self, result) -> ScenarioReport:
        """Extract ScenarioReport from Runner result."""
        # If output_type was set, result.final_output is already the model
        if isinstance(result.final_output, ScenarioReport):
            report = result.final_output
        elif isinstance(result.final_output, str):
            # Try to parse JSON from the output
            try:
                report = ScenarioReport.model_validate_json(result.final_output)
            except Exception:
                # Fallback: create a minimal report from the text
                next_steps = ["Review the raw agent output for details"]
                if self.last_raw_output_path:
                    next_steps.append(f"Raw output saved to: {self.last_raw_output_path}")
                report = ScenarioReport(
                    scenario_summary="Test execution completed (unstructured output)",
                    overall_status=StepStatus.FAIL,
                    issues_found=["Agent output could not be parsed as structured report"],
                    next_steps=next_steps,
                )
        else:
            report = ScenarioReport(
                scenario_summary="Test execution completed",
                overall_status=StepStatus.FAIL,
            )

        # Fill in metadata
        report.metadata = ReportMetadata(
            profile_name=self.profile.project.name,
            total_steps=self.stuck_detector.step_count,
            mode_switches=1 if self.state == InvestigationState.ANALYZING else 0,
        )

        self.state = InvestigationState.DONE
        return report
