"""Claude Code CLI executor — runs the full test pipeline via Claude Code CLI.

All LLM work (UI execution, DB verification, lesson generation) goes through
``claude -p`` so that choosing ``execution_mode=cli`` never touches the OpenAI
Agents SDK.

Architecture:
    Orchestrator → run_scenario_cli()    → claude -p + Playwright MCP → CLIResult
                 → verify_db_cli()       → claude -p + DB MCP        → list[dict]
                 → generate_lesson_cli() → claude -p (no MCP)        → (lesson, tags)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from universal_debug_agent.schemas.profile import ProjectProfile, DBCheckItem
from universal_debug_agent.mcp.factory import _resolve_env
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


def _build_mcp_config(profile: ProjectProfile) -> dict:
    """Build a Claude Code --mcp-config JSON from the profile's mcp_servers.

    Only includes the Playwright server (role != "database") since DB
    verification runs separately via db_tool.
    """
    servers: dict[str, Any] = {}
    for name, config in profile.mcp_servers.items():
        if not config.enabled:
            continue
        # Skip DB servers — DB verification runs separately
        if config.role == "database":
            continue

        server_def: dict[str, Any] = {
            "command": config.command,
            "args": config.args,
        }
        if config.env:
            server_def["env"] = _resolve_env(config.env)

        # Resolve cwd (mirrors factory._resolve_cwd logic for playwright)
        cwd = config.cwd
        if not cwd and name == "playwright":
            cwd = "./artifacts/playwright"
        if cwd:
            path = Path(cwd).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            if name == "playwright":
                timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
                path = path / timestamp
            path.mkdir(parents=True, exist_ok=True)
            server_def["cwd"] = str(path.resolve())

        servers[name] = server_def

    return {"mcpServers": servers}


def _build_mcp_config_for_db(profile: ProjectProfile) -> dict:
    """Build MCP config containing only the DB server(s)."""
    servers: dict[str, Any] = {}
    for name, config in profile.mcp_servers.items():
        if not config.enabled:
            continue
        is_db = config.role == "database" or (
            config.role is None and "database" in name.lower()
        )
        if not is_db:
            continue

        server_def: dict[str, Any] = {
            "command": config.command,
            "args": config.args,
        }
        if config.env:
            server_def["env"] = _resolve_env(config.env)
        if config.cwd:
            path = Path(config.cwd).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            path.mkdir(parents=True, exist_ok=True)
            server_def["cwd"] = str(path.resolve())

        servers[name] = server_def

    return {"mcpServers": servers}


# ---------------------------------------------------------------------------
# Shared CLI runner
# ---------------------------------------------------------------------------

async def _run_claude_cli(
    prompt: str,
    *,
    mcp_config: dict | None = None,
    allowed_tools: str | None = None,
    timeout_seconds: int = 600,
    label: str = "cli",
) -> tuple[str, str, int]:
    """Run ``claude -p`` and return (stdout, stderr, returncode).

    Streams stderr lines via logger in real-time.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        raise FileNotFoundError(
            "Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code"
        )

    cmd = [
        claude_path,
        "-p", prompt,
        "--output-format", "text",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    mcp_config_file = None

    try:
        if mcp_config and mcp_config.get("mcpServers"):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="mcp_config_", delete=False
            ) as f:
                json.dump(mcp_config, f)
                mcp_config_file = f.name
            cmd.extend(["--mcp-config", mcp_config_file])
            logger.info(
                f"[{label}] MCP config: {list(mcp_config['mcpServers'].keys())}"
            )

        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _stream_stderr() -> str:
            lines: list[str] = []
            assert proc.stderr is not None
            async for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                logger.info(f"[{label}] {line}")
            return "\n".join(lines)

        async def _collect_stdout() -> str:
            assert proc.stdout is not None
            data = await proc.stdout.read()
            return data.decode("utf-8", errors="replace")

        try:
            stderr, stdout, rc = await asyncio.wait_for(
                asyncio.gather(_stream_stderr(), _collect_stdout(), proc.wait()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

        return stdout, stderr, rc

    finally:
        if mcp_config_file and os.path.exists(mcp_config_file):
            os.unlink(mcp_config_file)


def _extract_json(raw: str) -> dict | list | None:
    """Extract the first JSON object or array from CLI text output."""
    m = re.search(r"```json\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Fallback: find last top-level { ... } or [ ... ]
    brace_depth = 0
    start = -1
    end = -1
    for i, ch in enumerate(raw):
        if ch in "{[":
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch in "}]":
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                end = i + 1
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            pass
    return None


async def run_scenario_cli(
    scenario: str,
    profile: ProjectProfile,
    db_checks: list[DBCheckItem] | None = None,
    timeout_seconds: int = 600,
) -> CLIResult:
    """Execute a UI test scenario via Claude Code CLI."""
    prompt = _build_cli_prompt(scenario, profile, db_checks)
    logger.info(f"[cli-ui] executing scenario ({len(prompt)} chars prompt)")

    try:
        stdout, stderr, rc = await _run_claude_cli(
            prompt,
            mcp_config=_build_mcp_config(profile),
            allowed_tools="mcp__playwright__*",
            timeout_seconds=timeout_seconds,
            label="cli-ui",
        )
    except FileNotFoundError as e:
        return CLIResult(success=False, error=str(e))
    except asyncio.TimeoutError:
        return CLIResult(
            success=False,
            error=f"CLI timed out after {timeout_seconds} seconds",
        )
    except Exception as e:
        return CLIResult(success=False, error=str(e))

    if rc != 0:
        return CLIResult(
            success=False,
            error=f"CLI exited with code {rc}: {stderr[-500:]}",
            raw_output=stdout,
        )

    logger.info(f"[cli-ui] completed, output: {len(stdout)} chars")
    return _parse_cli_output(stdout)


# ---------------------------------------------------------------------------
# DB verification via CLI
# ---------------------------------------------------------------------------

def _build_db_verify_prompt(
    data_json: str,
    db_checks: list[DBCheckItem],
    live_schema: str = "",
    network_log: str = "",
) -> str:
    """Build a prompt for DB verification via Claude Code CLI."""
    parts = [
        "You are a database verification agent.",
        "",
        "## Verification checklist",
    ]
    for i, check in enumerate(db_checks, 1):
        if isinstance(check, str):
            parts.append(f"  {i}. {check}")
        else:
            parts.append(f"  {i}. Table: {check.table} — find_by: {check.find_by}, verify: {check.verify}")
            if check.hint:
                parts.append(f"     Hint: {check.hint}")

    if live_schema:
        parts.append(f"\n## Live schema\n{live_schema}")

    if network_log:
        parts.append(f"\n## Network log (mutations captured during UI test)\n{network_log}")

    parts.append(f"\n## UI data extracted from the page\n{data_json}")

    parts.append(
        "\n## Instructions\n"
        "1. Use the database MCP tools to run SELECT queries and verify each check.\n"
        "2. Only SELECT — never INSERT, UPDATE, DELETE.\n"
        "3. Semantic equivalence is a pass: 'Bank Transfer' ≈ 'bank_transfer'.\n"
        "\n## Output format\n"
        "Output ONLY a JSON array (no other text):\n"
        "```json\n"
        "[\n"
        '  {"check_name": "...", "query": "SELECT ...", "expected": "...", '
        '"actual": "...", "status": "pass|fail|blocked", "severity": "high|medium|low"}\n'
        "]\n"
        "```"
    )
    return "\n".join(parts)


async def verify_db_cli(
    data_json: str,
    profile: ProjectProfile,
    db_checks: list[DBCheckItem],
    live_schema: str = "",
    network_log: str = "",
    timeout_seconds: int = 300,
) -> list[dict]:
    """Run DB verification via Claude Code CLI + DB MCP server."""
    if not db_checks:
        return []

    prompt = _build_db_verify_prompt(data_json, db_checks, live_schema, network_log)
    logger.info(f"[cli-db] starting DB verification ({len(prompt)} chars prompt)")

    db_mcp_config = _build_mcp_config_for_db(profile)
    if not db_mcp_config.get("mcpServers"):
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": "No DB MCP server configured",
            "status": "blocked", "severity": "high",
        }]

    # Allow all tools from DB MCP servers
    db_server_names = list(db_mcp_config["mcpServers"].keys())
    allowed = ",".join(f"mcp__{name}__*" for name in db_server_names)

    try:
        stdout, stderr, rc = await _run_claude_cli(
            prompt,
            mcp_config=db_mcp_config,
            allowed_tools=allowed,
            timeout_seconds=timeout_seconds,
            label="cli-db",
        )
    except Exception as e:
        logger.error(f"[cli-db] error: {e}")
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": f"CLI error: {e}",
            "status": "blocked", "severity": "high",
        }]

    if rc != 0:
        logger.warning(f"[cli-db] non-zero exit: {rc}")
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": f"CLI exited {rc}: {stderr[-300:]}",
            "status": "blocked", "severity": "high",
        }]

    parsed = _extract_json(stdout)
    if isinstance(parsed, list):
        return parsed
    logger.warning(f"[cli-db] could not parse JSON array from output ({len(stdout)} chars)")
    return [{
        "check_name": "DB verification",
        "query": "", "expected": "", "actual": "Could not parse DB CLI output",
        "status": "blocked", "severity": "high",
    }]


# ---------------------------------------------------------------------------
# Lesson generation via CLI
# ---------------------------------------------------------------------------

_LESSON_CLI_PROMPT = """\
You are a QA memory system. A test execution agent just completed a run.
Here is the run report:

{report_summary}

Output ONLY a JSON object (no markdown fences, no explanation):
{{
  "lesson": "<ONE paragraph, 3-5 sentences. Actionable guidance for the next attempt.>",
  "tags": ["<2-6 lowercase tags>"]
}}
"""


async def generate_lesson_cli(
    report: ScenarioReport,
    scenario: str,
    timeout_seconds: int = 60,
) -> tuple[str, list[str]]:
    """Generate a lesson from the run report via Claude Code CLI (no MCP needed)."""
    from universal_debug_agent.memory.lesson import _build_report_summary

    summary = _build_report_summary(report, scenario)
    prompt = _LESSON_CLI_PROMPT.format(report_summary=summary)

    try:
        stdout, _, rc = await _run_claude_cli(
            prompt, timeout_seconds=timeout_seconds, label="cli-lesson",
        )
    except Exception as e:
        logger.warning(f"[cli-lesson] failed: {e}")
        return "", []

    if rc != 0:
        logger.warning(f"[cli-lesson] non-zero exit: {rc}")
        return "", []

    parsed = _extract_json(stdout)
    if isinstance(parsed, dict):
        lesson = str(parsed.get("lesson", "")).strip()
        tags = [str(t).lower().strip() for t in parsed.get("tags", []) if t]
        if lesson:
            logger.info(f"Lesson generated ({len(lesson)} chars), tags: {tags}")
        return lesson, tags

    logger.warning("[cli-lesson] could not parse lesson JSON")
    return "", []


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
