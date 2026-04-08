"""Claude Code CLI executor — runs UI test scenarios via Claude Code CLI.

Replaces the Brain Agent + OpenAI Agents SDK loop with a single Claude Code
CLI invocation. Claude Code handles context management, Playwright MCP, and
obstacle handling internally.

Architecture:
    Orchestrator → claude_executor.run_scenario_cli() → claude -p "..." → parse result
    Then: orchestrator runs DB verification separately using existing db_tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from universal_debug_agent.schemas.profile import ProjectProfile, DBCheckItem
from universal_debug_agent.schemas.report import (
    DataVerification,
    ScenarioReport,
    ScenarioStep,
    StepStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class CLIResult:
    """Parsed result from Claude Code CLI execution."""

    success: bool
    extracted_data: dict = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    network_mutations: list[dict] = field(default_factory=list)
    error: str = ""
    raw_output: str = ""


def _build_cli_prompt(
    scenario: str,
    profile: ProjectProfile,
    db_checks: list[DBCheckItem] | None = None,
) -> str:
    """Build the prompt sent to Claude Code CLI for UI test execution.

    The prompt instructs Claude Code to:
    1. Execute the UI scenario using Playwright
    2. Extract business data from the confirmation page
    3. Return structured JSON with results

    DB verification is NOT included — it runs separately after this call.
    """
    parts = []

    # Project context
    parts.append(f"## Project: {profile.project.name}")
    if profile.project.description:
        parts.append(f"{profile.project.description}")
    if profile.environment.base_url:
        parts.append(f"Environment: {profile.environment.type} @ {profile.environment.base_url}")

    # Auth
    if profile.auth.login_url:
        parts.append(f"\n## Authentication")
        parts.append(f"Login URL: {profile.auth.login_url}")
        parts.append(f"Method: {profile.auth.method}")
        if profile.auth.test_accounts:
            import os
            for acct in profile.auth.test_accounts:
                username = os.environ.get(acct.username_env, acct.username_env)
                password = os.environ.get(acct.password_env, acct.password_env)
                parts.append(f"Test account ({acct.role}): {username} / {password}")

    # Boundaries
    parts.append(f"\n## Boundaries")
    if profile.boundaries.allowed_domains:
        parts.append(f"Stay within domains: {', '.join(profile.boundaries.allowed_domains)}")
    parts.append(f"Max steps: {profile.boundaries.max_steps}")

    # Task
    parts.append(f"\n## Task")
    parts.append(f"Execute this E2E test scenario using Playwright:")
    parts.append(f"**{scenario}**")

    # What to extract
    parts.append(f"\n## After completing the scenario")
    parts.append(
        "Extract all business data visible on the confirmation/success page "
        "(order IDs, totals, reference numbers, status, email, etc)."
    )

    if db_checks:
        parts.append("\nThe following data will be verified in the database afterwards:")
        for i, check in enumerate(db_checks, 1):
            if isinstance(check, str):
                parts.append(f"  {i}. {check}")
            else:
                parts.append(f"  {i}. Table: {check.table} — {check.verify or check.find_by}")
        parts.append("Make sure to extract the values needed for these DB checks from the page.")

    # Output format
    parts.append(f"\n## Output format")
    parts.append(
        "When done, output ONLY a JSON block (no other text) with this structure:\n"
        "```json\n"
        "{\n"
        '  "success": true,\n'
        '  "extracted_data": {"order_ref": "ABC123", "total": "268.45", ...},\n'
        '  "steps": [\n'
        '    {"step": 1, "action": "Navigate to homepage", "status": "pass"},\n'
        '    {"step": 2, "action": "Click Add to Cart", "status": "pass"}\n'
        "  ],\n"
        '  "error": ""\n'
        "}\n"
        "```\n"
        'Set "success": false and fill "error" if the flow could not complete.'
    )

    return "\n".join(parts)


def _parse_cli_output(raw: str) -> CLIResult:
    """Parse Claude Code CLI output to extract the JSON result.

    Claude Code may output thinking text before/after the JSON block.
    We find the JSON block and parse it.
    """
    if not raw.strip():
        return CLIResult(success=False, error="Empty CLI output", raw_output=raw)

    # Try to find JSON block in output
    json_str = None

    # Look for ```json ... ``` block first
    import re

    json_match = re.search(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find raw JSON object
        # Find the last { ... } block (most likely the result)
        brace_depth = 0
        start = -1
        end = -1
        for i, ch in enumerate(raw):
            if ch == "{":
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and start >= 0:
                    end = i + 1
        if start >= 0 and end > start:
            json_str = raw[start:end]

    if not json_str:
        return CLIResult(
            success=False,
            error="Could not find JSON in CLI output",
            raw_output=raw,
        )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return CLIResult(
            success=False,
            error=f"Invalid JSON in CLI output: {e}",
            raw_output=raw,
        )

    return CLIResult(
        success=data.get("success", False),
        extracted_data=data.get("extracted_data", {}),
        steps=data.get("steps", []),
        error=data.get("error", ""),
        raw_output=raw,
    )


async def run_scenario_cli(
    scenario: str,
    profile: ProjectProfile,
    db_checks: list[DBCheckItem] | None = None,
    timeout_seconds: int = 300,
) -> CLIResult:
    """Execute a UI test scenario via Claude Code CLI.

    Args:
        scenario: Natural language test scenario description.
        profile: Project profile with auth, boundaries, etc.
        db_checks: Optional DB checks (used to guide data extraction).
        timeout_seconds: Max time for CLI execution (default 5 minutes).

    Returns:
        CLIResult with extracted data, steps, and success status.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        return CLIResult(
            success=False,
            error="Claude Code CLI not found. Install it: npm install -g @anthropic-ai/claude-code",
        )

    prompt = _build_cli_prompt(scenario, profile, db_checks)
    logger.info(f"[cli] executing scenario via Claude Code CLI ({len(prompt)} chars prompt)")

    cmd = [
        claude_path,
        "-p", prompt,
        "--output-format", "text",
        "--verbose",
    ]

    # Add allowed tools for Playwright
    cmd.extend(["--allowedTools", "mcp__playwright__*"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            logger.warning(f"[cli] Claude Code CLI returned non-zero: {proc.returncode}")
            logger.warning(f"[cli] stderr: {stderr[:500]}")
            return CLIResult(
                success=False,
                error=f"CLI exited with code {proc.returncode}: {stderr[:500]}",
                raw_output=stdout,
            )

        logger.info(f"[cli] CLI completed, output: {len(stdout)} chars")
        return _parse_cli_output(stdout)

    except asyncio.TimeoutError:
        logger.error(f"[cli] CLI timed out after {timeout_seconds}s")
        if proc:
            proc.kill()
        return CLIResult(
            success=False,
            error=f"CLI timed out after {timeout_seconds} seconds",
        )
    except Exception as e:
        logger.error(f"[cli] CLI execution error: {e}")
        return CLIResult(success=False, error=str(e))


def cli_result_to_report(
    cli_result: CLIResult,
    scenario: str,
    db_verifications: list[dict] | None = None,
) -> ScenarioReport:
    """Convert CLIResult + DB verifications into a ScenarioReport."""
    steps = []
    for s in cli_result.steps:
        steps.append(ScenarioStep(
            step_number=s.get("step", 0),
            action=s.get("action", ""),
            status=StepStatus(s.get("status", "pass")) if s.get("status") in ("pass", "fail", "skip", "blocked") else StepStatus.PASS,
            actual_result=s.get("actual_result", ""),
            screenshot=s.get("screenshot", ""),
            notes=s.get("notes", ""),
        ))

    data_verifications = []
    if db_verifications:
        for v in db_verifications:
            data_verifications.append(DataVerification(
                check_name=v.get("check_name", ""),
                query=v.get("query", ""),
                expected=v.get("expected", ""),
                actual=v.get("actual", ""),
                status=StepStatus(v.get("status", "blocked")),
                severity=v.get("severity", "high"),
            ))

    # Determine overall status
    if not cli_result.success:
        overall = StepStatus.FAIL
    elif db_verifications and any(v.get("status") == "fail" for v in db_verifications):
        overall = StepStatus.FAIL
    else:
        overall = StepStatus.PASS

    issues = []
    if cli_result.error:
        issues.append(cli_result.error)
    if db_verifications:
        for v in db_verifications:
            if v.get("status") == "fail":
                issues.append(f"DB check failed: {v.get('check_name', '?')}")

    return ScenarioReport(
        scenario_summary=scenario[:80],
        overall_status=overall,
        steps_executed=steps,
        extracted_data=cli_result.extracted_data,
        data_verifications=data_verifications,
        issues_found=issues,
    )
