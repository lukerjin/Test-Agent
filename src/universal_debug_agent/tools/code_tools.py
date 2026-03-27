"""Local code browsing tools — read files, grep, list directories.

These are registered as @function_tool so the agent can browse the
project codebase without an MCP server. All paths are sandboxed to
the project's code.root_dir.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

from agents import function_tool

# Will be set by the orchestrator before the agent runs.
_root_dir: str = ""

MAX_READ_LINES = 200
MAX_GREP_RESULTS = 50


def configure(root_dir: str) -> None:
    """Set the code root directory. Must be called before agent runs."""
    global _root_dir
    _root_dir = root_dir


def _safe_path(relative: str) -> Path:
    """Resolve a relative path and ensure it stays within root_dir."""
    if not _root_dir:
        raise RuntimeError("code_tools.configure() has not been called")

    root = Path(_root_dir).resolve()
    target = (root / relative).resolve()

    if not str(target).startswith(str(root)):
        raise PermissionError(f"Path escapes root directory: {relative}")

    return target


@function_tool
def read_file(path: str, start_line: int = 1, end_line: int = 200) -> str:
    """Read lines from a code file.

    Args:
        path: Relative path from the project root.
        start_line: First line to read (1-based).
        end_line: Last line to read (1-based, max 200 lines per call).
    """
    target = _safe_path(path)
    if not target.is_file():
        return f"Error: not a file: {path}"

    # Clamp range
    end_line = min(end_line, start_line + MAX_READ_LINES - 1)

    lines = target.read_text(errors="replace").splitlines()
    selected = lines[start_line - 1 : end_line]

    numbered = [f"{i}: {line}" for i, line in enumerate(selected, start=start_line)]
    header = f"# {path} (lines {start_line}-{min(end_line, len(lines))} of {len(lines)})"
    return header + "\n" + "\n".join(numbered)


@function_tool
def grep_code(pattern: str, directory: str = "", file_glob: str = "*") -> str:
    """Search for a pattern in code files using grep.

    Args:
        pattern: Regex pattern to search for.
        directory: Subdirectory to search in (relative to project root). Empty = entire root.
        file_glob: File glob pattern, e.g. '*.py', '*.ts'. Default '*' matches all.
    """
    search_dir = _safe_path(directory) if directory else Path(_root_dir).resolve()
    if not search_dir.is_dir():
        return f"Error: not a directory: {directory}"

    try:
        result = subprocess.run(
            ["grep", "-rn", "--include", file_glob, "-E", pattern, str(search_dir)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 10s"

    lines = result.stdout.strip().splitlines()
    if not lines:
        return f"No matches for pattern: {pattern}"

    # Make paths relative to root
    root_str = str(Path(_root_dir).resolve())
    output_lines: list[str] = []
    for line in lines[:MAX_GREP_RESULTS]:
        output_lines.append(line.replace(root_str + "/", ""))

    suffix = ""
    if len(lines) > MAX_GREP_RESULTS:
        suffix = f"\n... ({len(lines) - MAX_GREP_RESULTS} more matches)"

    return "\n".join(output_lines) + suffix


@function_tool
def list_directory(path: str = "") -> str:
    """List files and directories at the given path.

    Args:
        path: Relative path from project root. Empty = project root.
    """
    target = _safe_path(path) if path else Path(_root_dir).resolve()
    if not target.is_dir():
        return f"Error: not a directory: {path}"

    entries: list[str] = []
    for entry in sorted(target.iterdir()):
        # Skip hidden files and common noise
        if entry.name.startswith(".") or entry.name == "node_modules" or entry.name == "__pycache__":
            continue
        marker = "/" if entry.is_dir() else ""
        entries.append(f"  {entry.name}{marker}")

    rel = path or "."
    return f"{rel}/\n" + "\n".join(entries) if entries else f"{rel}/ (empty)"
