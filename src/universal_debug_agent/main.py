"""CLI entry point for the Universal Test Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from universal_debug_agent.config import load_profile
from universal_debug_agent.mcp.factory import create_mcp_servers
from universal_debug_agent.memory.store import MemoryRecord, MemoryStore, resolve_memory_path
from universal_debug_agent.models.factory import create_model
from universal_debug_agent.orchestrator.state_machine import InvestigationOrchestrator
from universal_debug_agent.tools import code_tools

app = typer.Typer(
    name="test-agent",
    help="Universal Test Agent — execute test scenarios and verify data across any project.",
)
console = Console()


async def _run_test(
    profile_path: str,
    scenario: str,
    output: str | None,
    max_steps: int | None,
    verbose: bool,
) -> None:
    # Setup logging
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load profile
    console.print(f"[bold]Loading profile:[/bold] {profile_path}")
    profile = load_profile(profile_path)
    console.print(f"[green]Project:[/green] {profile.project.name}")

    # Override max_steps if specified
    if max_steps is not None:
        profile.boundaries.max_steps = max_steps

    # Configure code tools with the project root
    code_tools.configure(profile.code.root_dir)

    # Create model
    model = create_model(profile.model)
    model_desc = profile.model.model_name or profile.model.provider
    console.print(f"[green]Model:[/green] {profile.model.provider} / {model_desc}")

    # Load memory
    memory_context = ""
    memory_store = None
    if profile.memory.enabled:
        memory_path = resolve_memory_path(profile.memory.path, profile.project.name)
        memory_store = MemoryStore(memory_path)
        memory_context = memory_store.build_prompt_context(
            max_entries=profile.memory.max_entries_in_prompt
        )
        record_count = len(memory_store.load())
        console.print(f"[green]Memory:[/green] {memory_path} ({record_count} past records)")
    else:
        console.print("[dim]Memory: disabled[/dim]")

    # Create MCP servers
    mcp_servers = create_mcp_servers(profile)
    if mcp_servers:
        console.print(f"[green]MCP servers:[/green] {', '.join(s.name for s in mcp_servers)}")
    else:
        console.print("[yellow]No MCP servers configured[/yellow]")

    # Run test
    console.print(Panel(scenario, title="Test Scenario", border_style="blue"))

    orchestrator = InvestigationOrchestrator(
        profile=profile,
        mcp_servers=mcp_servers,
        model=model,
        memory_context=memory_context,
    )

    report = await orchestrator.run(scenario)

    # Save to memory
    if memory_store is not None:
        issues = "; ".join(report.issues_found) if report.issues_found else ""
        failed_steps = [
            s.action for s in report.steps_executed if s.status != "pass"
        ]
        failed_checks = [
            v.check_name for v in report.data_verifications if v.status != "pass"
        ]

        memory_store.save(MemoryRecord(
            issue=report.scenario_summary,
            root_cause=issues,
            classification=report.overall_status.value,
            key_findings=[f"Steps: {len(report.steps_executed)}, Verifications: {len(report.data_verifications)}"]
                + [f"FAIL step: {s}" for s in failed_steps]
                + [f"FAIL check: {c}" for c in failed_checks],
            dead_ends=[],
        ))
        console.print("[green]Memory updated[/green]")

    # Print summary table
    _print_summary(report)

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

    if not scenario:
        console.print("[red]Error: provide --scenario / -s[/red]")
        raise typer.Exit(1)

    asyncio.run(_run_test(
        profile_path=profile,
        scenario=scenario,
        output=output,
        max_steps=max_steps,
        verbose=verbose,
    ))


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
