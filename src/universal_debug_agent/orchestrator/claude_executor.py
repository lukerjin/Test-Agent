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

# Directory for CLI result files (in project, not system temp)
_CLI_RESULTS_DIR = Path("artifacts/cli_results")


def _cli_result_path(prefix: str) -> Path:
    """Generate a result file path under artifacts/cli_results/."""
    _CLI_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return _CLI_RESULTS_DIR / f"{prefix}_{datetime.now().strftime('%H%M%S')}.json"


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
    if profile.auth.login_url:
        parts.append(
            "\n**IMPORTANT — Login first**: The test results will be replayed in a "
            "fresh browser with NO existing session. You MUST start by navigating to "
            f"the login page ({profile.auth.login_url}), filling in the credentials, "
            "and clicking Sign In — even if the current Playwright session appears "
            "to be already logged in. Record every login step with its locator so "
            "that codegen can reproduce the full flow from scratch."
        )

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
        '    {\n'
        '      "step": 1,\n'
        '      "action": "Navigate to product page",\n'
        '      "status": "pass",\n'
        '      "locator": "page.goto(\'https://example.com/product-p-123.html\')",\n'
        '      "url_after": "https://example.com/product-p-123.html"\n'
        '    },\n'
        '    {\n'
        '      "step": 2,\n'
        '      "action": "Click Add to Cart",\n'
        '      "status": "pass",\n'
        '      "locator": "page.getByRole(\'button\', { name: \'Add To Cart\' })",\n'
        '      "context": "A modal #popup-cart-modal appeared after clicking"\n'
        '    },\n'
        '    {\n'
        '      "step": 3,\n'
        '      "action": "Click View Cart in popup modal",\n'
        '      "status": "pass",\n'
        '      "locator": "page.locator(\'#popup-cart-modal\').getByRole(\'link\', { name: \'View Cart\' })",\n'
        '      "url_after": "https://example.com/cart"\n'
        '    }\n'
        "  ],\n"
        '  "error": ""\n'
        "}\n"
        "```\n\n"
        "IMPORTANT for steps:\n"
        "- **locator**: The Playwright locator you used or would use (getByRole, locator, getByText, etc.)\n"
        "- **context**: Any DOM context that matters — modals/dialogs that appeared, iframes, popups\n"
        "- **url_after**: The page URL after the action completed (for navigation actions)\n"
        "- Be specific about WHERE you clicked — if an element is inside a modal, dialog, or specific container, say so\n\n"
        'Set "success": false and fill "error" if the flow could not complete.'
    )

    return "\n".join(parts)



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

        if allowed_tools is not None:
            cmd.append("--allowedTools")
            # --allowedTools expects space-separated args, not comma-separated
            for tool in allowed_tools.split(","):
                tool = tool.strip()
                if tool:
                    cmd.append(tool)

        logger.info(f"[{label}] cmd: {' '.join(cmd[:6])}... ({len(prompt)} chars prompt, timeout={timeout_seconds}s)")
        logger.info(f"[{label}] full cmd args: {cmd[3:]}")
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
            raise TimeoutError(f"claude -p timed out after {timeout_seconds}s")

        return stdout, stderr, rc

    finally:
        if mcp_config_file and os.path.exists(mcp_config_file):
            os.unlink(mcp_config_file)




async def run_scenario_cli(
    scenario: str,
    profile: ProjectProfile,
    db_checks: list[DBCheckItem] | None = None,
    timeout_seconds: int = 600,
) -> CLIResult:
    """Execute a UI test scenario via Claude Code CLI.

    Result is written to a temp file by Claude, then read back — no stdout parsing.
    """
    result_file = _cli_result_path("ui")
    prompt = _build_cli_prompt(scenario, profile, db_checks)
    prompt += (
        f"\n\nIMPORTANT: Write your JSON result to this file: {result_file}\n"
        f"Use the Write tool to save the JSON. Do NOT print it to stdout."
    )
    logger.info(f"[cli-ui] executing scenario ({len(prompt)} chars prompt)")

    try:
        _, stderr, rc = await _run_claude_cli(
            prompt,
            mcp_config=_build_mcp_config(profile),
            allowed_tools="mcp__playwright__*,Write",
            timeout_seconds=timeout_seconds,
            label="cli-ui",
        )
    except FileNotFoundError as e:
        return CLIResult(success=False, error=str(e))
    except (asyncio.TimeoutError, TimeoutError) as e:
        return CLIResult(success=False, error=str(e) or f"CLI timed out after {timeout_seconds}s")
    except Exception as e:
        return CLIResult(success=False, error=str(e))
    finally:
        # Cleanup will happen after we read the file below
        pass

    if rc != 0:
        result_file.unlink(missing_ok=True)
        return CLIResult(success=False, error=f"CLI exited with code {rc}: {stderr[-500:]}")

    if not result_file.exists():
        logger.warning(f"[cli-ui] Claude did not write result file: {result_file}")
        logger.warning(f"[cli-ui] cli_results dir contents: {list(_CLI_RESULTS_DIR.iterdir())}")
        return CLIResult(success=False, error=f"Claude did not write result file: {result_file}")

    try:
        raw = result_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        logger.info(f"[cli-ui] result file read OK ({len(raw)} chars, {len(data.get('steps', []))} steps)")
        # Keep a debug copy
        debug_copy = _CLI_RESULTS_DIR / f"debug_last_ui.json"
        debug_copy.write_text(raw, encoding="utf-8")
        return CLIResult(
            success=data.get("success", False),
            extracted_data=data.get("extracted_data", {}),
            steps=data.get("steps", []),
            error=data.get("error", ""),
            raw_output=raw,
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[cli-ui] failed to read result file: {e}")
        # Keep file for debugging
        return CLIResult(success=False, error=f"Failed to read result file: {e}")
    finally:
        result_file.unlink(missing_ok=True)


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
    """Run DB verification via Claude Code CLI + DB MCP server.

    Result is written to a temp file by Claude, then read back.
    """
    if not db_checks:
        return []

    result_file = _cli_result_path("db")
    prompt = _build_db_verify_prompt(data_json, db_checks, live_schema, network_log)
    prompt += (
        f"\n\nIMPORTANT: Write your JSON array result to this file: {result_file}\n"
        f"Use the Write tool to save the JSON array. Do NOT print it to stdout."
    )
    logger.info(f"[cli-db] starting DB verification ({len(prompt)} chars prompt)")

    db_mcp_config = _build_mcp_config_for_db(profile)
    if not db_mcp_config.get("mcpServers"):
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": "No DB MCP server configured",
            "status": "blocked", "severity": "high",
        }]

    db_server_names = list(db_mcp_config["mcpServers"].keys())
    allowed = ",".join(f"mcp__{name}__*" for name in db_server_names) + ",Write"

    try:
        _, stderr, rc = await _run_claude_cli(
            prompt,
            mcp_config=db_mcp_config,
            allowed_tools=allowed,
            timeout_seconds=timeout_seconds,
            label="cli-db",
        )
    except Exception as e:
        logger.error(f"[cli-db] error: {e}")
        result_file.unlink(missing_ok=True)
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": f"CLI error: {e}",
            "status": "blocked", "severity": "high",
        }]

    if rc != 0:
        logger.warning(f"[cli-db] non-zero exit: {rc}")
        result_file.unlink(missing_ok=True)
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": f"CLI exited {rc}: {stderr[-300:]}",
            "status": "blocked", "severity": "high",
        }]

    if not result_file.exists():
        logger.warning(f"[cli-db] Claude did not write result file: {result_file}")
        return [{
            "check_name": "DB verification",
            "query": "", "expected": "", "actual": f"Claude did not write result file: {result_file}",
            "status": "blocked", "severity": "high",
        }]

    try:
        raw = result_file.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        # Keep debug copy
        debug_copy = _CLI_RESULTS_DIR / "debug_last_db.json"
        debug_copy.write_text(raw, encoding="utf-8")
        if isinstance(parsed, list):
            logger.info(f"[cli-db] result read from file ({len(parsed)} checks)")
            return parsed
        logger.warning(f"[cli-db] result file is not a JSON array")
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[cli-db] failed to read result file: {e}")
    finally:
        result_file.unlink(missing_ok=True)

    logger.warning(f"[cli-db] could not parse result file")
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

    result_file = _cli_result_path("lesson")
    prompt += (
        f"\n\nIMPORTANT: Write your JSON result to this file: {result_file}\n"
        f"Use the Write tool to save the JSON object. Do NOT print it to stdout."
    )

    try:
        _, _, rc = await _run_claude_cli(
            prompt, timeout_seconds=timeout_seconds, label="cli-lesson",
        )
    except Exception as e:
        logger.warning(f"[cli-lesson] failed: {e}")
        result_file.unlink(missing_ok=True)
        return "", []

    if rc != 0:
        logger.warning(f"[cli-lesson] non-zero exit: {rc}")
        result_file.unlink(missing_ok=True)
        return "", []

    if not result_file.exists():
        logger.warning("[cli-lesson] Claude did not write result file")
        return "", []

    try:
        raw = result_file.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            lesson = str(parsed.get("lesson", "")).strip()
            tags = [str(t).lower().strip() for t in parsed.get("tags", []) if t]
            if lesson:
                logger.info(f"Lesson generated ({len(lesson)} chars), tags: {tags}")
            return lesson, tags
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[cli-lesson] failed to read result file: {e}")
    finally:
        result_file.unlink(missing_ok=True)

    return "", []


def cli_result_to_report(
    cli_result: CLIResult,
    scenario: str,
    db_verifications: list[dict] | None = None,
) -> ScenarioReport:
    """Convert CLIResult + DB verifications into a ScenarioReport."""
    steps = []
    for s in cli_result.steps:
        # Combine locator/context/url_after into notes for codegen consumption
        note_parts = []
        if s.get("locator"):
            note_parts.append(f"locator: {s['locator']}")
        if s.get("context"):
            note_parts.append(f"context: {s['context']}")
        if s.get("url_after"):
            note_parts.append(f"url_after: {s['url_after']}")
        if s.get("notes"):
            note_parts.append(s["notes"])
        steps.append(ScenarioStep(
            step_number=s.get("step", 0),
            action=s.get("action", ""),
            status=StepStatus(s.get("status", "pass")) if s.get("status") in ("pass", "fail", "skip", "blocked") else StepStatus.PASS,
            actual_result=s.get("actual_result", ""),
            screenshot=s.get("screenshot", ""),
            notes=" | ".join(note_parts),
        ))

    data_verifications = []
    if db_verifications:
        for v in db_verifications:
            data_verifications.append(DataVerification(
                check_name=str(v.get("check_name", "")),
                query=str(v.get("query", "")),
                expected=str(v.get("expected", "")),
                actual=str(v.get("actual", "")),
                status=StepStatus(v.get("status", "blocked")),
                severity=str(v.get("severity", "high")),
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
