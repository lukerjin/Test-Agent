"""CLI entry point for the Universal Debug Agent."""

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

from universal_debug_agent.config import load_profile
from universal_debug_agent.mcp.factory import create_mcp_servers
from universal_debug_agent.orchestrator.state_machine import InvestigationOrchestrator
from universal_debug_agent.tools import code_tools

app = typer.Typer(
    name="debug-agent",
    help="Universal Debug Agent — investigate issues across any project.",
)
console = Console()


async def _run_investigation(
    profile_path: str,
    issue: str,
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

    # Create MCP servers
    mcp_servers = create_mcp_servers(profile)
    if mcp_servers:
        console.print(f"[green]MCP servers:[/green] {', '.join(s.name for s in mcp_servers)}")
    else:
        console.print("[yellow]No MCP servers configured[/yellow]")

    # Run investigation
    console.print(Panel(issue, title="Issue", border_style="blue"))

    orchestrator = InvestigationOrchestrator(
        profile=profile,
        mcp_servers=mcp_servers,
    )

    report = await orchestrator.run(issue)

    # Output report
    report_json = report.model_dump_json(indent=2)

    if output:
        Path(output).write_text(report_json)
        console.print(f"\n[green]Report saved to:[/green] {output}")
    else:
        console.print("\n")
        console.print(Panel("Investigation Report", style="bold green"))
        console.print(Syntax(report_json, "json", theme="monokai"))


@app.command()
def investigate(
    profile: str = typer.Option(..., "--profile", "-p", help="Path to project profile YAML"),
    issue: Optional[str] = typer.Option(None, "--issue", "-i", help="Issue description text"),
    issue_url: Optional[str] = typer.Option(None, "--issue-url", help="GitHub issue URL"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output report file path"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Override max investigation steps"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """Investigate an issue using the debug agent."""

    if not issue and not issue_url:
        console.print("[red]Error: provide --issue or --issue-url[/red]")
        raise typer.Exit(1)

    # For v1, issue_url support is deferred; require --issue
    if issue_url and not issue:
        console.print("[yellow]--issue-url support coming in v2. Please use --issue for now.[/yellow]")
        raise typer.Exit(1)

    asyncio.run(_run_investigation(
        profile_path=profile,
        issue=issue or "",
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
