"""MCP server factory — creates MCPServerStdio instances from profile config."""

from __future__ import annotations

import os

from agents.mcp import MCPServerStdio

from universal_debug_agent.schemas.profile import MCPServerConfig, ProjectProfile


def _resolve_env(env_config: dict[str, str]) -> dict[str, str]:
    """Resolve environment variable references.

    Keys ending with '_ENV' are treated as references: the value is an env var
    name, and we resolve it to the actual value.  Other keys are passed through
    as-is.
    """
    resolved: dict[str, str] = {}
    for key, value in env_config.items():
        if key.endswith("_ENV"):
            actual_key = key.removesuffix("_ENV")
            env_value = os.environ.get(value, "")
            resolved[actual_key] = env_value
        else:
            resolved[key] = value
    return resolved


def create_mcp_server(name: str, config: MCPServerConfig) -> MCPServerStdio:
    """Create a single MCP server from config."""
    env = _resolve_env(config.env) if config.env else None
    return MCPServerStdio(
        name=name,
        command=config.command,
        args=config.args,
        env=env,
    )


def create_mcp_servers(profile: ProjectProfile) -> list[MCPServerStdio]:
    """Create all enabled MCP servers defined in the profile."""
    servers: list[MCPServerStdio] = []
    for name, config in profile.mcp_servers.items():
        if config.enabled:
            servers.append(create_mcp_server(name, config))
    return servers
