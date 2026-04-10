# Integrating Claude Code CLI as a Subprocess Execution Engine

A comprehensive guide for embedding `claude -p` into Python applications, with a real-world case study of building an E2E test agent. Every pitfall documented here was encountered and debugged in production.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Core Principle: File-Based Result Exchange](#core-principle-file-based-result-exchange)
- [Critical Pitfalls (Ranked by Pain)](#critical-pitfalls-ranked-by-pain)
- [Subprocess Runner](#subprocess-runner)
- [MCP Server Integration](#mcp-server-integration)
- [Prompt Engineering for CLI](#prompt-engineering-for-cli)
- [Multi-Step Pipeline Design](#multi-step-pipeline-design)
- [Debugging Playbook](#debugging-playbook)
- [CLI Flags Reference](#cli-flags-reference)
- [Case Study: E2E Test Agent](#case-study-e2e-test-agent)

---

## Architecture Overview

```
Parent Process (Python / asyncio)
  |
  |-- Build prompt (include result file path + JSON schema)
  |-- Build MCP config (write to temp JSON file)
  |-- Construct command:
  |     claude -p "<prompt>"
  |       --output-format text
  |       --verbose
  |       --dangerously-skip-permissions
  |       --mcp-config /tmp/mcp_xxxx.json
  |       --allowedTools mcp__playwright__* Write
  |
  |-- Launch subprocess (asyncio.create_subprocess_exec)
  |-- Stream stderr in real-time (progress visibility)
  |-- Wait for exit (with timeout)
  |-- Read result from file written by Claude
  |-- Clean up temp files
```

The parent process is the orchestrator. Claude Code CLI is a stateless worker — each `claude -p` invocation is independent, has no memory of prior calls, and communicates results exclusively through files.

---

## Core Principle: File-Based Result Exchange

**Never parse structured data from stdout.** This is the single most important lesson.

### Why stdout doesn't work

| Approach | Problem |
|----------|---------|
| `--output-format text` | Stdout contains prose, thinking, tool descriptions mixed with results |
| `--output-format json` | Returns a ~1MB event stream of all internal messages, not your result |
| `--json-schema` | Unreliable — Claude may not comply, and output is still wrapped in events |

### The file pattern

Tell Claude to write its result to a specific file path using the built-in `Write` tool:

```python
result_file = Path("artifacts/cli_results/ui_143022.json")
prompt += (
    f"\n\nIMPORTANT: Write your JSON result to this file: {result_file}\n"
    f"Use the Write tool to save the JSON. Do NOT print it to stdout."
)
```

After the subprocess exits, read the file:

```python
if not result_file.exists():
    return {"error": "Claude did not write result file"}

raw = result_file.read_text()
data = json.loads(raw)

# Keep a debug copy — the original gets deleted
(RESULTS_DIR / "debug_last.json").write_text(raw)
result_file.unlink()
```

### Why keep debug copies

Result files are cleaned up after reading. This means the results directory is always empty after a run. Without debug copies, you can't tell if the file was never created (bug) or was created and cleaned up (success). Always persist a `debug_last_*.json`.

---

## Critical Pitfalls (Ranked by Pain)

### 1. `--allowedTools` is SPACE-separated, not comma-separated

**Impact: Total pipeline failure, silent — no error message.**

```python
# WRONG — treated as ONE pattern "mcp__playwright__*,Write", matches nothing
cmd.extend(["--allowedTools", "mcp__playwright__*,Write"])

# CORRECT — each tool is a separate CLI argument
cmd.extend(["--allowedTools", "mcp__playwright__*", "Write"])
```

Claude gets zero tools and silently produces empty results. Everything downstream (DB verification, codegen) fails because the upstream data is empty. No error is raised.

**Always split:**

```python
if allowed_tools:
    cmd.append("--allowedTools")
    for tool in allowed_tools.split(","):
        t = tool.strip()
        if t:
            cmd.append(t)
```

### 2. Always include `Write` in `--allowedTools`

`Write` is a built-in tool, not part of any MCP server. When you restrict tools with `--allowedTools`, you must explicitly add it — otherwise Claude can execute MCP actions but cannot save the result file.

```python
# UI execution: Playwright MCP + Write
allowed_tools = "mcp__playwright__*,Write"

# DB verification: Database MCP + Write
allowed_tools = "mcp__database__*,Write"

# Codegen: no restriction — needs Write + Read + Edit
# Don't pass --allowedTools at all
```

### 3. Never parse stdout for structured data

(See [Core Principle](#core-principle-file-based-result-exchange) above.)

### 4. MCP config must be a temp file

`--mcp-config` expects a file path. Create a temp file and always clean up:

```python
with tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", prefix="mcp_", delete=False
) as f:
    json.dump({"mcpServers": servers}, f)
    mcp_config_file = f.name

cmd.extend(["--mcp-config", mcp_config_file])

# Always clean up in finally block
try:
    ...
finally:
    os.unlink(mcp_config_file)
```

### 5. Session state leaks between Playwright MCP runs

If a Playwright MCP server is reused across calls, the browser session persists. Claude will find it's already logged in and skip login steps. But generated tests run in a fresh browser and need the full login flow.

**Fix**: Force login in the prompt regardless of current session state:

```
IMPORTANT — Login first: The test results will be replayed in a fresh browser
with NO existing session. You MUST start by navigating to the login page,
filling in credentials, and clicking Sign In — even if the current Playwright
session appears to be already logged in. Record every login step with its locator
so that codegen can reproduce the full flow from scratch.
```

### 6. Cross-domain login redirects

Some apps redirect the login URL to a different domain for authentication (e.g. SSO, legacy backend). The generated test must handle this:

```
After login, ALWAYS use `await page.waitForTimeout(3000)` instead of
`waitForURL` because:
(a) login may redirect through a different domain before landing back
(b) the final URL may be exactly BASE_URL with no trailing path, which
    won't match BASE_URL/** glob patterns
```

**Real example**: Navigating to `vueadmin.local.test/admin/login` redirects to `backend.local.test/admin_xxx/login.php?redirect=...`. After login, it redirects back to `vueadmin.local.test/admin`. The glob `**/admin/**` doesn't match `/admin` (no trailing path).

### 7. Database aliases

MCP database servers may use alias mappings (e.g. `DB_ALIASES=inkstation_barcode_db:warehouse_management`). The MCP server resolves these transparently, but generated standalone tests connect directly to MySQL and don't know about aliases.

**Fix**: Resolve aliases at codegen time:

```python
def _load_db_aliases() -> dict[str, str]:
    raw = os.environ.get("DB_ALIASES", "")
    aliases = {}
    for pair in raw.split(","):
        if ":" in pair:
            alias, real = pair.split(":", 1)
            aliases[alias.strip()] = real.strip()
    return aliases

# When building the codegen prompt:
alias_name = "inkstation_barcode_db"  # from profile db_checks
real_name = aliases.get(alias_name, alias_name)  # "warehouse_management"
```

### 8. Error logging must happen before loop break

In a generate -> validate -> fix loop, the error output must be captured and logged BEFORE checking if max attempts is reached:

```python
# WRONG — error swallowed on final attempt
if attempt >= max_attempts:
    break
error_output = stderr + stdout  # never reached

# CORRECT
error_output = stderr + stdout
logger.info(f"Attempt {attempt} failed: {error_output[:2000]}")
if attempt >= max_attempts:
    break
```

### 9. Shared constants between execution modes

If your project supports multiple execution modes (e.g. OpenAI agent mode + Claude CLI mode), don't share constants that should differ:

```python
# WRONG — changing CLI retry count breaks agent mode
MAX_FIX_ATTEMPTS = 1  # intended for CLI, but agent mode uses it too

# CORRECT
MAX_FIX_ATTEMPTS = 3        # agent mode: generate + fix + rerun
MAX_FIX_ATTEMPTS_CLI = 1    # CLI mode: generate only
```

### 10. Dynamic data in text-based locators

Claude's UI execution records locators like `page.getByText('test 80')` where `80` is a live stock quantity. If codegen hardcodes this, the test breaks on the next run when the quantity changes.

**Prompt rule**: Tell Claude to never hardcode dynamic values in text matchers:

```
NEVER hardcode quantities, counts, or other numbers that change between runs
into getByText() locators. Instead:
- Build the locator dynamically from a pre-check DB query value
- Or use a partial text match: page.locator(':text("test")').first()
- Or use a structural selector (CSS class, container + nth-child)
```

---

## Subprocess Runner

```python
import asyncio
import shutil
import logging

logger = logging.getLogger(__name__)

async def run_claude_cli(
    prompt: str,
    *,
    mcp_config: dict | None = None,
    allowed_tools: str | None = None,
    timeout_seconds: int = 600,
    label: str = "cli",
) -> tuple[str, str, int]:
    """Run claude -p and return (stdout, stderr, returncode).

    Streams stderr via logger for real-time progress visibility.
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
            import tempfile, json
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="mcp_", delete=False
            ) as f:
                json.dump(mcp_config, f)
                mcp_config_file = f.name
            cmd.extend(["--mcp-config", mcp_config_file])

        # --allowedTools: MUST be space-separated args
        if allowed_tools:
            cmd.append("--allowedTools")
            for tool in allowed_tools.split(","):
                t = tool.strip()
                if t:
                    cmd.append(t)

        # Log the command for debugging (skip prompt which is arg[2])
        logger.info(f"[{label}] cmd args: {cmd[3:]}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

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
        import os
        if mcp_config_file and os.path.exists(mcp_config_file):
            os.unlink(mcp_config_file)
```

---

## MCP Server Integration

### Building MCP config from a project profile

```python
def build_mcp_config(profile, role_filter=None):
    """Build mcpServers dict, optionally filtering by server role."""
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

### Separating MCP servers by role

Run UI execution and DB verification as separate `claude -p` calls with different MCP configs:

```python
# UI execution — only Playwright
ui_config = build_mcp_config(profile, role_filter="browser")
await run_claude_cli(ui_prompt, mcp_config=ui_config, allowed_tools="mcp__playwright__*,Write")

# DB verification — only Database
db_config = build_mcp_config(profile, role_filter="database")
await run_claude_cli(db_prompt, mcp_config=db_config, allowed_tools="mcp__database__*,Write")
```

---

## Prompt Engineering for CLI

### Result format

Always specify the exact JSON schema with a full example:

```
Output a JSON object with this structure:
{
  "success": true,
  "extracted_data": {"order_ref": "ABC123", "total": "268.45"},
  "steps": [
    {
      "step": 1,
      "action": "Navigate to product page",
      "status": "pass",
      "locator": "page.goto('https://example.com/product-123.html')",
      "url_after": "https://example.com/product-123.html"
    }
  ],
  "error": ""
}
```

### Step metadata for codegen consumption

Request rich metadata per step so downstream codegen has precise information:

- **locator**: The actual Playwright locator used (`page.locator('#email')`, `page.getByRole(...)`)
- **context**: DOM context — modals, dialogs, iframes that appeared
- **url_after**: Page URL after the action completed

These flow directly from UI execution results into the codegen prompt.

### Scenario description quality

The quality of generated test code is **directly proportional to the specificity of the scenario description**. Compare:

```yaml
# Bad — vague, Claude has to guess everything
description: "Transfer stock from ground to label"

# Good — explicit IDs, values, directions
description: |
  Transfer 1 unit from ground location "test" to label "a2".
  Steps:
  - Navigate to inventory page for cga_inventory_id=2039
  - Under GROUND section, click the card named "test"
  - In the Transfer tab, set Inbox=1, Box=1, Total=1
  - In TO section, select Label radio, type "a2", press Enter
  - Click "Confirm Transfer"
  Key info for DB verification:
  - inventory_id=2039
  - Ground source: type_id=3, ref_id=3
  - Label destination: type_id=1, ref_id=2
  - Transfer qty = 1, action_type=2
```

### DB check descriptions

Avoid internal field names (`payload.data.type_id`). Use concrete values:

```yaml
# Bad — codegen can't resolve payload references
db_checks:
  - find_by: "type_id=payload.data.type_id, ref_id=payload.data.ref_id"
    verify: "total decreased by payload.totalTransferQty"

# Good — concrete values, codegen knows exactly what to query
db_checks:
  - table: "inkstation_barcode_db.inventory_stock_details"
    find_by: "inventory_id=2039, type_id=3, ref_id=3"
    verify: "total decreased by 1"
    hint: "filter log entries by id > pre-transfer max log id"
```

---

## Multi-Step Pipeline Design

### Data chain

```
UI Execution (claude -p + Playwright MCP)
  → writes JSON result file
  → parent reads CLIResult (steps, extracted_data)
       |
       v
DB Verification (claude -p + DB MCP)
  → receives extracted_data as context
  → writes JSON result file
  → parent reads verification results
       |
       v
Report Assembly
  → combines steps + DB results into ScenarioReport
  → packs locator/context/url_after into step notes
       |
       v
Codegen (claude -p, no MCP)
  → receives report with rich step data
  → writes .js test file directly
       |
       v
Validation
  → runs generated test with node
  → captures pass/fail + error output
```

### Failure cascade

If UI execution produces empty results (e.g. due to `--allowedTools` bug), everything downstream fails silently:
- DB verification has no `extracted_data` to verify against
- Codegen receives "(no steps recorded)" and has to guess the entire test flow
- Generated test uses wrong selectors, wrong URLs, wrong assertions

**Always validate each step's output before proceeding.**

### Dual-mode architecture

When supporting multiple execution engines (e.g. OpenAI Agents SDK + Claude CLI):

1. **Shared code**: Prompt templates, report schemas, test validators
2. **Separate code**: Subprocess runner (CLI only), MCP connection (agent only), result parsing
3. **Separate constants**: Retry counts, timeouts
4. **Lazy imports**: Don't import CLI modules at the top of shared files — if the CLI binary isn't installed, agent mode would fail to import

---

## Debugging Playbook

### Symptom → Cause → Fix

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `cli_results/` empty, no `debug_last_*.json` | `--allowedTools` doesn't include `Write` | Add `Write` to allowed tools list |
| `cli_results/` empty, `debug_last_*.json` exists | Normal — result files are cleaned up after reading | Inspect `debug_last_*.json` |
| Claude does nothing (empty results, rc=0) | `--allowedTools` comma-separated instead of space-separated | Split into separate args |
| Steps missing login flow | Playwright session already logged in | Add login-first prompt instruction |
| `waitForURL` timeout after login | Login redirects through different domain, or URL has no trailing path | Use `waitForTimeout(3000)` instead |
| Codegen uses wrong selectors | UI steps were empty (upstream failure) | Check `debug_last_ui.json` step count |
| DB query "Unknown database" | Database alias not resolved | Load `DB_ALIASES` env and map to real names |
| `mysqlQuery` "No database selected" | Template default database is empty/wrong | Set default from profile db_checks |
| Generated test hardcodes dynamic values | Codegen prompt lacks guidance | Add "never hardcode quantities" rule |
| Error output not visible in logs | Error logged after loop `break` | Move logging before the break check |
| Regex captures wrong text (e.g. "is") | `/i` flag + `[A-Z0-9]+` matches lowercase | Use min-length `{5,}` or skip filler words |
| Test passes but DB checks fail | Transfer clicked wrong element (e.g. label instead of ground) | Make scenario description explicit about which UI element to click |

### Essential debug logging

```python
# Log the actual CLI command (without prompt content)
logger.info(f"[{label}] cmd args: {cmd[3:]}")

# Log result file status
if result_file.exists():
    logger.info(f"Result file: {len(raw)} chars, {len(data.get('steps', []))} steps")
else:
    logger.warning(f"Result file NOT created: {result_file}")
    logger.warning(f"Directory contents: {list(RESULTS_DIR.iterdir())}")
```

---

## CLI Flags Reference

| Flag | Purpose | Notes |
|------|---------|-------|
| `-p "<prompt>"` | Non-interactive single-prompt mode | Required for subprocess use |
| `--output-format text` | Human-readable stdout | Don't parse it for data |
| `--verbose` | Detailed stderr output | Essential for debugging |
| `--dangerously-skip-permissions` | Auto-approve all tool calls | Required for `-p` mode |
| `--mcp-config <path>` | MCP server config file | Must be a file path, not inline JSON |
| `--allowedTools <t1> <t2> ...` | Restrict available tools | **Space-separated**, not comma-separated |

### Built-in tool names for `--allowedTools`

`Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebFetch`, `WebSearch`

### MCP tool patterns

- All tools from a server: `mcp__<serverName>__*`
- Specific tool: `mcp__<serverName>__<toolName>`

---

## Case Study: E2E Test Agent

### Project overview

An AI-powered E2E test execution agent. Given a natural language scenario, it:
1. Executes the scenario in a browser (via Playwright MCP)
2. Verifies database state (via DB MCP)
3. Generates a standalone replayable test script

The agent supports two execution engines: OpenAI Agents SDK ("agent" mode) and Claude Code CLI ("cli" mode).

### The integration journey

#### Phase 1: Basic subprocess integration

Initial approach: run `claude -p` with Playwright MCP, parse JSON from stdout.

**What went wrong**: Claude's stdout contains thinking text, tool call descriptions, and results mixed together. Regex parsing (`_parse_cli_output`, `_extract_json`) was fragile — sometimes Claude returned prose instead of JSON, or wrapped JSON in markdown fences inconsistently.

**Lesson**: Stdout is not a data channel. It's a human-readable log.

#### Phase 2: File-based result exchange

Switched all three CLI calls (UI execution, DB verification, lesson generation) to write results to files under `artifacts/cli_results/`.

**What went wrong**: `cli_results/` was always empty. Initial diagnosis: Claude isn't writing files. Real cause: `--allowedTools "mcp__playwright__*,Write"` — the comma-separated string was treated as a single pattern matching nothing. Claude had zero tools available.

**Lesson**: The #1 pitfall. Space-separated, not comma-separated. And the failure is completely silent.

#### Phase 3: Pipeline data chain

With file exchange working, the full pipeline ran: UI execution produced steps with locators, DB verification passed, and codegen generated test files.

**What went wrong**: Generated tests failed validation. Multiple causes:
- **Missing login flow**: Playwright session was already logged in, Claude skipped login steps. Codegen had no login info.
- **Wrong selectors**: UI execution returned generic selectors because Claude combined multiple actions into one step.
- **Order reference regex**: `/Order Reference ID\s*[:]?\s*([A-Z0-9]+)/i` captured "is" instead of the order ID.

**Lessons**: Force login-first in prompt. Request one action per step. Be careful with `/i` flag in regex patterns.

#### Phase 4: Cross-project support

Extending from Inkstation (e-commerce) to Vue Admin (warehouse management).

**What went wrong**:
- **Database alias**: Profile used `inkstation_barcode_db.inventory_stock_details` but the actual MySQL database was `warehouse_management`. The MCP server resolved the alias transparently, but standalone tests didn't.
- **Cross-domain login**: `vueadmin.local.test/admin/login` redirected to `backend.local.test/...login.php`. Generated test used `waitForURL('**/admin/**')` which didn't match `/admin` (no trailing path).
- **Dynamic data**: Codegen hardcoded `getByText('test 80')` where 80 was a stock quantity that changed every run.
- **Wrong element clicked**: Scenario said "click a ground location card" without specifying which one. Codegen clicked the wrong card.

**Lessons**:
- Resolve DB aliases at codegen time using `DB_ALIASES` env var.
- Use `waitForTimeout` after login, never `waitForURL`.
- Never hardcode changing values in locators.
- Scenario descriptions must be explicit: which element, what value, expected result.

#### Phase 5: Agent-mode regression

While fixing CLI mode, accidentally broke OpenAI agent mode.

**What went wrong**:
- `MAX_FIX_ATTEMPTS` changed from 3 to 1 for CLI mode, but was a shared constant — agent mode's fix loop became dead code.
- `mysqlQuery` template default database changed from `'inkstation'` to `''` for cross-database support — agent mode had no database extraction logic, so all DB queries failed.
- Error logging fix applied to CLI's `generate_and_validate_cli` but not agent's `generate_and_validate`.

**Lessons**:
- Separate constants for separate modes.
- When fixing one code path, audit the parallel path for the same bug.
- Run automated tests (`pytest`) before committing cross-cutting changes.

### Key metrics

| Metric | Before (stdout parsing) | After (file exchange) |
|--------|------------------------|----------------------|
| UI execution success rate | ~20% (JSON parsing failures) | ~95% (file always written) |
| Codegen quality | Guessing (no step data) | Accurate (real locators) |
| DB verification | Often skipped (empty data) | Reliable (file exchange) |
| Debug visibility | Blind (stdout dumped) | Clear (`debug_last_*.json`) |

### Architecture diagram (final)

```
test-agent test -p profile.yaml -s "scenario" --mode cli
  |
  |-- InvestigationOrchestrator.run()
  |     |
  |     |-- _run_pipeline_cli()
  |           |
  |           |-- run_scenario_cli()
  |           |     |-- Build prompt (scenario + auth + login-first)
  |           |     |-- _run_claude_cli(prompt, mcp=playwright, tools=playwright+Write)
  |           |     |-- Read artifacts/cli_results/ui_HHMMSS.json
  |           |     |-- Return CLIResult(steps, extracted_data)
  |           |
  |           |-- cli_result_to_report()
  |           |     |-- Pack locator/context/url_after into step notes
  |           |
  |           |-- verify_db_cli()
  |           |     |-- _run_claude_cli(prompt, mcp=database, tools=database+Write)
  |           |     |-- Read artifacts/cli_results/db_HHMMSS.json
  |           |     |-- Return list[dict] (check results)
  |           |
  |           |-- Build ScenarioReport
  |
  |-- generate_lesson_cli()
  |     |-- _run_claude_cli(prompt, no MCP)
  |     |-- Read artifacts/cli_results/lesson_HHMMSS.json
  |
  |-- generate_and_validate_cli()
        |-- _build_codegen_prompt(report, profile, db_checks)
        |     |-- Resolve DB aliases
        |     |-- Include step locators, DB SQL, auth credentials
        |-- _run_claude_cli(prompt, no MCP, no allowedTools)
        |     |-- Claude writes .js file directly via Write tool
        |-- Run with node, check pass/fail
        |-- (Optional) Fix loop: send error to Claude, re-run
```
