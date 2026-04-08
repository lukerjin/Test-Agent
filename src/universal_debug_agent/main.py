"""CLI entry point for the Universal Test Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()  # Load .env before anything reads os.environ

import re

import typer
from agents import set_default_openai_client
from agents.tracing import set_tracing_disabled
from openai import APIStatusError, AsyncOpenAI, RateLimitError
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from universal_debug_agent.config import load_profile
from universal_debug_agent.schemas.profile import DBCheckItem, ScenarioConfig
from universal_debug_agent.mcp.factory import create_mcp_servers
from universal_debug_agent.memory.store import MemoryRecord, MemoryStore, resolve_memory_path
from universal_debug_agent.models.factory import create_model
from universal_debug_agent.observability.llm_usage import (
    JsonlUsageStore,
    LLMUsageTracker,
    default_usage_dir,
)
from universal_debug_agent.observability.trace_recorder import ExecutionTraceRecorder
from universal_debug_agent.orchestrator.state_machine import InvestigationOrchestrator
from universal_debug_agent.tools.auth_tools import configure_test_accounts, resolve_test_accounts
from universal_debug_agent.tools import code_tools

if not os.environ.get("OPENAI_API_KEY"):
    set_tracing_disabled(True)

_TAG_RE = re.compile(r"^\[(\w+)\]\s*")
_TAG_COLORS: dict[str, str] = {
    "LLM":    "\033[36m",   # cyan
    "action": "\033[33m",   # yellow
    "result": "\033[32m",   # green
    "stuck":  "\033[31m",   # red
}
_RESET = "\033[0m"


class _TagFormatter(logging.Formatter):
    """Move [tag] to the front of the log line and colorize it."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        m = _TAG_RE.match(msg)
        if m:
            tag_word = m.group(1)
            rest = msg[m.end():]
            color = _TAG_COLORS.get(tag_word, "")
            tag_str = f"{color}[{tag_word}]{_RESET}"
            timestamp = self.formatTime(record, self.datefmt)
            return f"{tag_str:<28} {timestamp} {record.name}: {rest}"
        return super().format(record)


app = typer.Typer(
    name="test-agent",
    help="Universal Test Agent — execute test scenarios and verify data across any project.",
)
console = Console()


def _extract_retry_delay(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if response is None:
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    if isinstance(payload, list) and payload:
        payload = payload[0]

    if not isinstance(payload, dict):
        return None

    details = payload.get("error", {}).get("details", [])
    for detail in details:
        if isinstance(detail, dict) and detail.get("@type", "").endswith("RetryInfo"):
            retry_delay = detail.get("retryDelay")
            if retry_delay:
                return str(retry_delay)
    return None


def _format_api_error(error: Exception, provider: str) -> str:
    if isinstance(error, RateLimitError):
        retry_delay = _extract_retry_delay(error)
        lines = [
            f"{provider} API 返回 429，当前配额或限流已触发。",
            "",
            "常见原因：",
            "1. 当前 key 对应项目的免费额度已耗尽。",
            "2. 当前模型的每分钟请求数或 token 数超限。",
            "3. 该项目尚未开通可用 billing，导致 quota 为 0。",
            "",
            "建议处理：",
            "1. 去 Google AI Studio / GCP 检查 Gemini API quota 和 billing。",
            "2. 换一个有额度的 key 或项目。",
            "3. 暂时切到其他 provider，例如 openai / deepseek / groq。",
        ]
        if retry_delay:
            lines.append(f"4. 服务端建议至少等待 {retry_delay} 后重试。")
        return "\n".join(lines)

    if isinstance(error, APIStatusError):
        return f"{provider} API 请求失败，HTTP {error.status_code}。{error}"

    return str(error)


async def _run_test(
    profile_path: str,
    scenario: str,
    output: str | None,
    max_steps: int | None,
    verbose: bool,
    db_checks: list[DBCheckItem] | None = None,
    scenario_name: str | None = None,
) -> None:
    # Setup logging
    log_level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(_TagFormatter(datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=log_level, handlers=[handler], force=True)

    # Load profile
    console.print(f"[bold]Loading profile:[/bold] {profile_path}")
    profile = load_profile(profile_path)
    console.print(f"[green]Project:[/green] {profile.project.name}")

    # Override max_steps if specified
    if max_steps is not None:
        profile.boundaries.max_steps = max_steps

    # Configure code tools with the project root
    code_tools.configure(profile.code.root_dir)
    configure_test_accounts(resolve_test_accounts(profile.auth.test_accounts))

    # Create model
    model = create_model(profile.model)
    model_desc = profile.model.model_name or profile.model.provider
    console.print(f"[green]Model:[/green] {profile.model.provider} / {model_desc}")

    # For native OpenAI (model is a string), override the default client with max_retries=5
    # so the SDK handles 429 rate limits with exponential backoff instead of failing fast.
    if isinstance(model, str):
        set_default_openai_client(AsyncOpenAI(max_retries=5))

    # Load memory
    memory_context = ""
    memory_store = None
    if profile.memory.enabled:
        memory_path = resolve_memory_path(profile.memory.path, profile.project.name)
        memory_store = MemoryStore(memory_path)
        memory_store.load()
        memory_context = memory_store.build_prompt_context(
            max_entries=profile.memory.max_entries_in_prompt,
            scenario=scenario,
        )
        record_count = len(memory_store._records)
        console.print(f"[green]Memory:[/green] {memory_path} ({record_count} past records)")
    else:
        console.print("[dim]Memory: disabled[/dim]")

    # Create MCP servers
    mcp_servers = create_mcp_servers(profile)
    if mcp_servers:
        console.print(f"[green]MCP servers:[/green] {', '.join(s.name for s in mcp_servers)}")
    else:
        console.print("[yellow]No MCP servers configured[/yellow]")

    usage_dir = default_usage_dir(profile.project.name)
    usage_tracker = LLMUsageTracker(
        project_name=profile.project.name,
        scenario=scenario,
        provider=profile.model.provider,
        model=model_desc,
        store=JsonlUsageStore(usage_dir),
    )
    trace_recorder = ExecutionTraceRecorder(
        Path(usage_dir) / "runs" / usage_tracker.run_id
    )
    console.print(f"[green]LLM usage:[/green] {usage_dir}")

    # Run test
    console.print(Panel(scenario, title="Test Scenario", border_style="blue"))

    orchestrator = InvestigationOrchestrator(
        profile=profile,
        mcp_servers=mcp_servers,
        model=model,
        memory_context=memory_context,
        usage_tracker=usage_tracker,
        trace_recorder=trace_recorder,
        db_checks=db_checks,
        scenario_name=scenario_name,
    )

    try:
        report = await orchestrator.run(scenario)
    except Exception:
        if orchestrator.last_error_output_path:
            console.print(f"[dim]Run error output: {orchestrator.last_error_output_path}[/dim]")
        if orchestrator.last_raw_output_path:
            console.print(f"[dim]Raw final output: {orchestrator.last_raw_output_path}[/dim]")
        raise
    finally:
        usage_summary = usage_tracker.write_summary()

    # Save to memory
    if memory_store is not None:
        memory_store.save(MemoryRecord(
            issue=report.scenario_summary,
            root_cause=report.issues_found[0] if report.issues_found else "",
            classification=report.overall_status.value,
            lesson=orchestrator.last_lesson,
            tags=orchestrator.last_lesson_tags,
        ))
        console.print("[green]Memory updated[/green]")

    # Print summary table
    _print_summary(report)
    console.print(
        f"[dim]LLM calls: {usage_summary.call_count}, "
        f"tokens in/out/total: "
        f"{usage_summary.input_tokens}/{usage_summary.output_tokens}/{usage_summary.total_tokens}[/dim]"
    )
    if orchestrator.last_raw_output_path:
        console.print(f"[dim]Raw final output: {orchestrator.last_raw_output_path}[/dim]")
    console.print(f"[dim]Execution trace: {trace_recorder.md_path}[/dim]")

    # Output report
    report_json = report.model_dump_json(indent=2)

    if output:
        Path(output).write_text(report_json)
        console.print(f"\n[green]Report saved to:[/green] {output}")
    else:
        console.print("\n")
        console.print(Syntax(report_json, "json", theme="monokai"))


def _print_summary(report) -> None:
    """Print a concise summary table of the test results."""
    console.print()

    # Overall status
    status_color = "green" if report.overall_status.value == "pass" else "red"
    console.print(
        Panel(
            f"[{status_color} bold]{report.overall_status.value.upper()}[/{status_color} bold]",
            title=report.scenario_summary,
            border_style=status_color,
        )
    )

    # Steps table
    if report.steps_executed:
        table = Table(title="Steps Executed")
        table.add_column("#", width=4)
        table.add_column("Action")
        table.add_column("Status", width=10)
        table.add_column("Notes")

        for step in report.steps_executed:
            color = "green" if step.status.value == "pass" else "red"
            table.add_row(
                str(step.step_number),
                step.action,
                f"[{color}]{step.status.value}[/{color}]",
                step.notes or step.actual_result[:60],
            )
        console.print(table)

    # Data verification table
    if report.data_verifications:
        table = Table(title="Data Verifications")
        table.add_column("Check")
        table.add_column("Expected")
        table.add_column("Actual")
        table.add_column("Status", width=10)

        for v in report.data_verifications:
            color = "green" if v.status.value == "pass" else "red"
            table.add_row(
                v.check_name,
                v.expected[:40],
                v.actual[:40],
                f"[{color}]{v.status.value}[/{color}]",
            )
        console.print(table)

    # Issues
    if report.issues_found:
        console.print("\n[red bold]Issues Found:[/red bold]")
        for issue in report.issues_found:
            console.print(f"  - {issue}")


@app.command()
def test(
    profile: str = typer.Option(..., "--profile", "-p", help="Path to project profile YAML"),
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Test scenario description"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output report file path"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Override max steps"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """Execute a test scenario on the target application."""

    # Resolve named scenario from profile
    db_checks: list[DBCheckItem] | None = None
    scenario_name: str | None = None
    try:
        _profile = load_profile(profile)
        if scenario and scenario in _profile.scenarios:
            scenario_name = scenario
            cfg = _profile.scenarios[scenario]
            if isinstance(cfg, ScenarioConfig):
                scenario = cfg.description
                db_checks = cfg.db_checks or None
            else:
                scenario = cfg
        elif not scenario:
            if _profile.scenarios:
                table = Table(title="Available Scenarios", show_header=True)
                table.add_column("Name", style="cyan")
                table.add_column("Description", style="white")
                for name, cfg in _profile.scenarios.items():
                    desc = cfg.description if isinstance(cfg, ScenarioConfig) else cfg
                    table.add_row(name, desc)
                console.print(table)
                console.print("\n[yellow]Run with:[/yellow] -s <name>")
            else:
                console.print("[red]Error: provide --scenario / -s[/red]")
            raise typer.Exit(0)
    except typer.Exit:
        raise
    except Exception:
        if not scenario:
            console.print("[red]Error: provide --scenario / -s[/red]")
            raise typer.Exit(1)

    try:
        asyncio.run(_run_test(
            profile_path=profile,
            scenario=scenario,
            output=output,
            max_steps=max_steps,
            verbose=verbose,
            db_checks=db_checks,
            scenario_name=scenario_name,
        ))
    except (RateLimitError, APIStatusError) as e:
        provider = "LLM"
        try:
            provider = load_profile(profile).model.provider
        except Exception:
            pass

        console.print(Panel(
            _format_api_error(e, provider),
            title="LLM Request Failed",
            border_style="red",
        ))
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]Run failed:[/red] {e}")
        raise


@app.command()
def validate_profile(
    profile: str = typer.Argument(..., help="Path to project profile YAML to validate"),
) -> None:
    """Validate a project profile YAML file."""
    try:
        p = load_profile(profile)
        console.print(f"[green]Valid profile:[/green] {p.project.name}")
        console.print(f"  Environment: {p.environment.type} @ {p.environment.base_url}")
        console.print(f"  Model: {p.model.provider} / {p.model.model_name or 'default'}")
        console.print(f"  Code root: {p.code.root_dir}")
        console.print(f"  MCP servers: {', '.join(p.mcp_servers.keys()) or 'none'}")
        console.print(f"  Max steps: {p.boundaries.max_steps}")
    except Exception as e:
        console.print(f"[red]Invalid profile:[/red] {e}")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
