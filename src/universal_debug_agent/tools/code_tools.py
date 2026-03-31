"""Local code browsing tools — read files, grep, list directories.

These are registered as @function_tool so the agent can browse the
project codebase without an MCP server. All paths are sandboxed to
the project's code.root_dir.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from agents import function_tool

# Will be set by the orchestrator before the agent runs.
_root_dir: str = ""

MAX_READ_LINES = 200
MAX_GREP_FILES = 8
MAX_GREP_MATCHES_PER_FILE = 2
MAX_GREP_OUTPUT_CHARS = 2500


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


def _format_grep_discovery(
    lines: list[str],
    *,
    pattern: str,
    directory: str,
    file_glob: str,
    root_dir: str,
) -> str:
    if not lines:
        return f"No matches for pattern: {pattern}"

    root_str = str(Path(root_dir).resolve())
    grouped: dict[str, list[str]] = defaultdict(list)

    for line in lines:
        normalized = line.replace(root_str + "/", "")
        file_path, sep, remainder = normalized.partition(":")
        if not sep:
            continue
        grouped[file_path].append(remainder)

    file_paths = sorted(grouped)
    summary_lines = [
        "# grep_code discovery summary",
        f"pattern: {pattern}",
        f"directory: {directory or '.'}",
        f"file_glob: {file_glob}",
        f"matched_files: {len(file_paths)}",
    ]

    if len(file_paths) > MAX_GREP_FILES:
        summary_lines.append(
            f"note: too many matching files; showing top {MAX_GREP_FILES}. Narrow directory or file_glob before reading files."
        )

    for file_path in file_paths[:MAX_GREP_FILES]:
        matches = grouped[file_path]
        summary_lines.append(f"\n- {file_path} ({len(matches)} matches)")
        for match in matches[:MAX_GREP_MATCHES_PER_FILE]:
            line_no, _, text = match.partition(":")
            compact = " ".join(text.strip().split())
            summary_lines.append(f"  {line_no}: {compact[:160]}")
        if len(matches) > MAX_GREP_MATCHES_PER_FILE:
            summary_lines.append(
                f"  ... {len(matches) - MAX_GREP_MATCHES_PER_FILE} more matches in this file"
            )

    output = "\n".join(summary_lines)
    if len(output) > MAX_GREP_OUTPUT_CHARS:
        output = output[: MAX_GREP_OUTPUT_CHARS - 120].rstrip()
        output += "\n\nnote: search summary truncated; narrow the search before reading files."

    return output


def _resolve_search_command() -> list[str]:
    rg_path = shutil.which("rg")
    if rg_path:
        return [
            rg_path,
            "--line-number",
            "--with-filename",
        ]

    grep_path = shutil.which("grep")
    if grep_path:
        return [
            grep_path,
            "-rnE",
        ]

    raise FileNotFoundError("Neither 'rg' nor 'grep' is available in PATH")


@function_tool
def grep_code(pattern: str, directory: str = "", file_glob: str = "*") -> str:
    """Search for a pattern in code files using ripgrep and return compact discovery results.

    Args:
        pattern: Regex pattern to search for.
        directory: Subdirectory to search in (relative to project root). Empty = entire root.
        file_glob: File glob pattern, e.g. '*.py', '*.ts'. Default '*' matches all.
    """
    search_dir = _safe_path(directory) if directory else Path(_root_dir).resolve()
    if not search_dir.is_dir():
        return f"Error: not a directory: {directory}"

    try:
        command = _resolve_search_command()
        if command[0].endswith("rg"):
            search_cmd = [
                *command,
                "--glob",
                file_glob,
                "--regexp",
                pattern,
                str(search_dir),
            ]
        else:
            search_cmd = [
                *command,
                "--include",
                file_glob,
                pattern,
                str(search_dir),
            ]
        result = subprocess.run(
            search_cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        return f"Error: code search tool unavailable — {exc}"
    except subprocess.TimeoutExpired:
        return "Error: code search timed out after 10s"

    return _format_grep_discovery(
        lines=result.stdout.strip().splitlines(),
        pattern=pattern,
        directory=directory,
        file_glob=file_glob,
        root_dir=_root_dir,
    )


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
