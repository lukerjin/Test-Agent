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

### Pipeline Data Chain

When building a multi-step pipeline (e.g. UI execution → DB verification → codegen), each step feeds the next. **If any upstream step returns empty results, everything downstream fails silently.** Always validate the output of each step before proceeding:

```python
if not cli_result.steps:
    logger.error("UI execution returned no steps — downstream codegen will have no data")
    # Don't proceed to codegen with empty input
```

---

## Critical Pitfalls

### 1. `--allowedTools` is SPACE-separated, not comma-separated

```python
# WRONG — treated as a single pattern, matches NOTHING
cmd.extend(["--allowedTools", "mcp__playwright__*,Write"])

# CORRECT — each tool is a separate argument
cmd.extend(["--allowedTools", "mcp__playwright__*", "Write"])
```

This is the #1 gotcha. If you pass a comma-separated string, Claude gets zero tools and **silently does nothing** — no error, no warning, just empty results that cascade through your entire pipeline. Always split:

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

### 6. Result files are cleaned up — keep debug copies

Result files are deleted after reading (`result_file.unlink()`). This means `cli_results/` will always appear empty after a run — you can't tell if the file was never written or was written and cleaned up. **Always keep a debug copy**:

```python
# After reading the result file successfully:
debug_copy = RESULTS_DIR / "debug_last_ui.json"
debug_copy.write_text(raw, encoding="utf-8")
```

This gives you a persistent record of what Claude actually produced, invaluable for diagnosing codegen issues.

### 7. Error logging must happen before loop break

In a generate → validate → fix loop, if you check `attempt >= max_attempts` before logging the error, the error gets swallowed on the final attempt:

```python
# WRONG — error never logged when max_attempts reached
if attempt >= max_attempts:
    break
logger.info(f"Error: {error_output}")  # never reached

# CORRECT — always log the error first
logger.info(f"Error: {error_output}")
if attempt >= max_attempts:
    break
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

        # Log the actual command for debugging
        logger.info(f"[{label}] cmd args: {cmd[3:]}")

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
                logger.info(f"[{label}] {line}")
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
        raw = result_file.read_text()
        data = json.loads(raw)
        # Keep debug copy — result file gets deleted, this persists
        (RESULTS_DIR / f"debug_last.json").write_text(raw)
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

## Prompt Engineering Checklist

### Structure
1. **Result format**: Always specify exact JSON schema with examples
2. **File output**: Always end with "Write your JSON to this file: {path}"
3. **Boundaries**: Specify allowed domains, max steps, forbidden actions
4. **DB safety**: "Only SELECT — never INSERT, UPDATE, DELETE"

### Step Recording
5. **Login-first**: If auth is involved, force full login flow even if session exists
6. **Rich step metadata**: Request locator, context (modals/popups), url_after per step
7. **One action per step**: Tell Claude "record each action as a separate step" — it tends to combine related actions (e.g. "Fill email and password") into one step, which loses locator granularity for codegen
8. **Locator specificity**: Request the actual Playwright locator used (`page.locator('#email')`, `page.getByRole(...)`) not descriptions. These flow directly into codegen.

### Codegen-Specific
9. **Regex `/i` flag trap**: When generating regex for data extraction, the `/i` flag makes `[A-Z0-9]+` match lowercase too. `/Order Reference ID\s*[:]?\s*([A-Z0-9]+)/i` will capture "is" from "Order Reference ID is ABC123". Fix: add `(?:is\s+)?` or use min-length `{5,}`
10. **Fresh browser context**: Generated tests run standalone — prompt must include full auth credentials (login URL, email, password), not just role names
11. **Dynamic data in locators**: Never hardcode quantities/counts in `getByText()` — build locators dynamically from pre-check DB values or use partial text matches
12. **Cross-domain login**: Use `waitForTimeout` after login submit, never `waitForURL` — login may redirect through a different domain and land on BASE_URL with no trailing path
13. **DB aliases**: MCP servers may resolve aliases transparently (e.g. `inkstation_barcode_db` → `warehouse_management`), but standalone tests need the real database name. Load `DB_ALIASES` env var and resolve before injecting into generated code.
14. **Separate mode constants**: Don't share retry counts or timeouts between agent and CLI modes — changing one silently breaks the other
15. **Scenario specificity**: Vague scenarios produce wrong tests. Specify which element to click, exact values to enter, and include DB field IDs for verification

---

## Debugging Checklist

When things go wrong:

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `cli_results/` empty, no debug files | `--allowedTools` missing `Write` | Add `Write` to allowed tools |
| `cli_results/` empty, debug files exist | Normal — result files are cleaned up after reading | Check `debug_last_*.json` |
| Claude does nothing | `--allowedTools` comma-separated instead of space-separated | Split into separate args |
| Steps missing login | Session already logged in, Claude skipped it | Add login-first instruction |
| Codegen uses wrong locators | UI execution steps were empty | Check `debug_last_ui.json` for step count |
| Error output not visible | Error logged after loop break | Move logging before `if attempt >= max` check |
| Generated regex captures wrong text | `/i` flag on `[A-Z0-9]+` pattern | Use min-length or skip common words |
| Timeout | Default too short | UI: 600s, DB/codegen: 300s, simple: 60s |
| MCP tools not found | Config file path wrong or servers misconfigured | Log and check `--mcp-config` file contents |

Add command logging for diagnostics:
```python
logger.info(f"[{label}] cmd args: {cmd[3:]}")
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
| `--allowedTools <tool1> <tool2>` | Restrict available tools (**space-separated**) |

Built-in tool names for `--allowedTools`: `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`

MCP tool pattern: `mcp__<serverName>__*` or `mcp__<serverName>__<toolName>`
