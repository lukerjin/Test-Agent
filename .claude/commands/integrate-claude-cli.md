# Integrate Claude Code CLI as Execution Engine

A battle-tested guide for embedding `claude -p` as a subprocess execution engine in Python projects. Derived from real integration experience — every pitfall listed here was hit in production.

---

## Architecture Pattern

```
Parent Process (Python)
  ├── Build prompt (with result file path)
  ├── Build MCP config (temp JSON file)
  ├── Launch: claude -p "<prompt>" --mcp-config <file> --allowedTools <tools...>
  ├── Stream stderr for real-time progress
  ├── Wait for exit
  └── Read result from file (NOT stdout)
```

**Core principle**: All structured results go through files, never stdout. Claude writes JSON to a known path via its built-in Write tool; the parent process reads it after the subprocess exits.

---

## Critical Pitfalls

### 1. `--allowedTools` is SPACE-separated, not comma-separated

```python
# WRONG — treated as a single pattern, matches NOTHING
cmd.extend(["--allowedTools", "mcp__playwright__*,Write"])

# CORRECT — each tool is a separate argument
cmd.extend(["--allowedTools", "mcp__playwright__*", "Write"])
```

This is the #1 gotcha. If you pass a comma-separated string, Claude gets zero tools and silently does nothing useful. Always split:

```python
if allowed_tools:
    cmd.append("--allowedTools")
    for tool in allowed_tools.split(","):
        tool = tool.strip()
        if tool:
            cmd.append(tool)
```

### 2. Always include `Write` in allowedTools

When you restrict tools with `--allowedTools`, you must explicitly include `Write` — it's a built-in tool, not part of any MCP server. Without it, Claude can execute actions but cannot save the result file.

```python
allowed_tools="mcp__playwright__*,Write"   # for UI execution
allowed_tools="mcp__database__*,Write"      # for DB verification
```

### 3. Never parse structured data from stdout

Claude's stdout in `--output-format text` mode contains prose, thinking, and tool call descriptions mixed together. `--output-format json` returns a massive event stream (~1MB). Neither is reliably parseable.

**Instead**: Tell Claude to write results to a specific file path:

```python
result_file = Path("artifacts/cli_results/ui_143022.json")
prompt += (
    f"\n\nIMPORTANT: Write your JSON result to this file: {result_file}\n"
    f"Use the Write tool to save the JSON. Do NOT print it to stdout."
)
```

### 4. MCP config must be a temp file

`--mcp-config` expects a file path, not inline JSON. Create a temp file and clean up after:

```python
import tempfile, json

with tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", prefix="mcp_config_", delete=False
) as f:
    json.dump({"mcpServers": servers}, f)
    mcp_config_file = f.name

cmd.extend(["--mcp-config", mcp_config_file])

# Clean up in finally block
os.unlink(mcp_config_file)
```

### 5. Session state leaks between runs

If a Playwright MCP session is reused, the browser may already be logged in. Claude will skip login steps. But downstream consumers (like codegen) need the full flow.

**Fix**: Explicitly instruct Claude to perform login regardless:

```
IMPORTANT — Login first: The test results will be replayed in a fresh browser
with NO existing session. You MUST start by navigating to the login page,
filling in credentials, and clicking Sign In — even if the current session
appears to be already logged in. Record every login step with its locator.
```

---

## Subprocess Runner Template

```python
import asyncio
import shutil

async def run_claude_cli(
    prompt: str,
    *,
    mcp_config: dict | None = None,
    allowed_tools: str | None = None,
    timeout_seconds: int = 600,
    label: str = "cli",
) -> tuple[str, str, int]:
    """Run claude -p and return (stdout, stderr, returncode)."""
    claude_path = shutil.which("claude")
    if not claude_path:
        raise FileNotFoundError("claude CLI not found")

    cmd = [
        claude_path,
        "-p", prompt,
        "--output-format", "text",
        "--verbose",
        "--dangerously-skip-permissions",
    ]

    mcp_config_file = None
    try:
        # MCP config
        if mcp_config and mcp_config.get("mcpServers"):
            import tempfile, json, os
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="mcp_", delete=False
            ) as f:
                json.dump(mcp_config, f)
                mcp_config_file = f.name
            cmd.extend(["--mcp-config", mcp_config_file])

        # Allowed tools — MUST be space-separated args
        if allowed_tools:
            cmd.append("--allowedTools")
            for tool in allowed_tools.split(","):
                t = tool.strip()
                if t:
                    cmd.append(t)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Stream stderr for real-time visibility
        async def stream_stderr() -> str:
            lines = []
            async for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                print(f"[{label}] {line}")  # or use logger
            return "\n".join(lines)

        async def collect_stdout() -> str:
            data = await proc.stdout.read()
            return data.decode("utf-8", errors="replace")

        try:
            stderr, stdout, rc = await asyncio.wait_for(
                asyncio.gather(stream_stderr(), collect_stdout(), proc.wait()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"claude -p timed out after {timeout_seconds}s")

        return stdout, stderr, rc
    finally:
        if mcp_config_file:
            import os
            if os.path.exists(mcp_config_file):
                os.unlink(mcp_config_file)
```

---

## File-Based Result Exchange Pattern

```python
from pathlib import Path
from datetime import datetime

RESULTS_DIR = Path("artifacts/cli_results")

def result_path(prefix: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR / f"{prefix}_{datetime.now().strftime('%H%M%S')}.json"

async def execute_with_file_result(prompt: str, **kwargs) -> dict:
    result_file = result_path("task")
    prompt += (
        f"\n\nIMPORTANT: Write your JSON result to this file: {result_file}\n"
        f"Use the Write tool to save the JSON. Do NOT print it to stdout."
    )

    _, _, rc = await run_claude_cli(prompt, **kwargs)

    if rc != 0 or not result_file.exists():
        return {"error": f"CLI failed (rc={rc}) or no result file"}

    try:
        import json
        data = json.loads(result_file.read_text())
        # Keep debug copy
        (RESULTS_DIR / f"debug_last.json").write_text(
            result_file.read_text()
        )
        return data
    finally:
        result_file.unlink(missing_ok=True)
```

---

## File-Write Pattern (Codegen)

When you want Claude to produce a file (not JSON data), let it write directly:

```python
prompt = f"""Generate a Playwright test script.
...
IMPORTANT: Write the generated code to this file: {file_path.resolve()}
Use the Write tool to save the complete .js file."""

# No --allowedTools restriction — Claude needs Write + Read + Edit
await run_claude_cli(prompt, timeout_seconds=300)

if not file_path.exists():
    raise RuntimeError("Claude did not create the file")
```

For fix loops, tell Claude to read, fix, and overwrite:

```python
fix_prompt = f"""The test at {file_path.resolve()} is failing.
Error: {error_output}
Read the file, fix the specific error, and save the corrected version."""
```

---

## MCP Config Builder

```python
def build_mcp_config(profile, role_filter=None):
    """Build mcpServers dict, optionally filtering by role."""
    servers = {}
    for name, config in profile.mcp_servers.items():
        if not config.enabled:
            continue
        if role_filter and config.role != role_filter:
            continue

        server_def = {
            "command": config.command,
            "args": config.args,
        }
        if config.env:
            server_def["env"] = resolve_env(config.env)
        if config.cwd:
            server_def["cwd"] = str(Path(config.cwd).resolve())

        servers[name] = server_def

    return {"mcpServers": servers}
```

---

## Prompt Engineering Checklist

1. **Result format**: Always specify exact JSON schema with examples
2. **File output**: Always end with "Write your JSON to this file: {path}"
3. **Login-first**: If auth is involved, force full login flow
4. **Rich step metadata**: Request locator, context (modals/popups), url_after per step
5. **Boundaries**: Specify allowed domains, max steps, forbidden actions
6. **DB safety**: "Only SELECT — never INSERT, UPDATE, DELETE"

---

## Debugging Checklist

When things go wrong:

1. **cli_results empty?** → Check `--allowedTools` includes `Write`
2. **Claude does nothing?** → Check `--allowedTools` format (space-separated, not comma)
3. **Steps missing login?** → Add login-first instruction to prompt
4. **Result file has wrong data?** → Keep `debug_last_*.json` copies to inspect
5. **Timeout?** → Default 600s for UI, 300s for DB/codegen, 60s for simple tasks
6. **MCP tools not found?** → Check `--mcp-config` file exists and has correct server names

Add command logging for diagnostics:
```python
logger.info(f"cmd args: {cmd[3:]}")  # skip claude path and -p prompt
```

---

## CLI Flags Reference

| Flag | Purpose |
|------|---------|
| `-p "<prompt>"` | Non-interactive single-prompt mode |
| `--output-format text` | Human-readable stdout (don't parse it) |
| `--verbose` | More detail in stderr |
| `--dangerously-skip-permissions` | Auto-approve all tool calls (required for `-p`) |
| `--mcp-config <path>` | JSON file with MCP server definitions |
| `--allowedTools <tool1> <tool2>` | Restrict available tools (space-separated) |

Built-in tool names for `--allowedTools`: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`

MCP tool pattern: `mcp__<serverName>__*` or `mcp__<serverName>__<toolName>`
