# Universal Test Agent

An AI-powered E2E test execution and data verification agent. Supports two execution engines: [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) (default) and [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code).

Give it a business scenario in natural language. It walks through the entire flow, handles obstacles on its own, and verifies that the right data landed in the database. **Any scenario, not limited to a fixed flow.**

## What It Does

- **Execute any business flow** — checkout, registration, refunds, permissions, search... anything you can describe in natural language
- **Handle obstacles automatically** — not logged in? logs in. popup? dismisses it. slow load? waits
- **Collect evidence** — auto-screenshots at key steps, records page state
- **Verify data** — after the flow completes, queries the database to confirm data persisted correctly; supports scenario-level `db_checks` for precise verification
- **Capture form submissions** — traditional `<form>` POSTs (page reload) are captured via `browser_evaluate` before click, so DB agent sees the submitted field values
- **Structured reports** — per-step pass/fail + per-check data verification pass/fail
- **Test memory (RAG)** — auto-extracts lessons after each run, injects relevant ones into future runs of the same scenario
- **Multi-LLM support** — OpenAI / Gemini / DeepSeek / Groq, switch with one line
- **Multi-project** — one codebase, one YAML config per project

## Architecture

### Dual Execution Mode

The agent supports two execution engines, selected via `execution_mode` in the profile or `--mode` CLI flag:

| | Agent Mode (default) | CLI Mode |
|---|---|---|
| **Engine** | OpenAI Agents SDK ReAct loop | `claude -p` (Claude Code CLI) |
| **UI execution** | Brain Agent + Playwright MCP (in-process) | `claude -p` + Playwright MCP (subprocess, `--mcp-config`) |
| **DB verification** | DB Sub-Agent via `Runner.run()` | `claude -p` + DB MCP (subprocess) |
| **Lesson generation** | LessonWriter via `Runner.run()` | `claude -p` (no MCP) |
| **LLM provider** | OpenAI / Gemini / DeepSeek / Groq | Anthropic (Claude) |
| **Context management** | `MCPToolOutputFilter` + StuckDetector | Claude Code handles internally |
| **MCP lifecycle** | Parent process connects/disconnects | Each `claude -p` subprocess manages its own |

```
execution_mode: agent (default)
+------------------------------------------------------------+
|         UI Agent (ReAct / Analysis) -- no DB access        |
|        Orchestrator + StuckDetector control mode switching  |
+----------+----------+----------+---------------------------+
|Playwright|  Code    |Memory    |  verify_in_db (tool)      |
|  MCP     |  Tools   |  RAG     |  +----------------------+ |
| browse   | read_file|JSONL+tag |  |   DB Sub-Agent       | |
| snapshot | grep_code|lesson    |  |  independent Runner  | |
| auto-snap|          |injection |  |  DB MCP (read-only)  | |
| form     |          |          |  |  Live Schema         | |
| capture  |          |          |  |  Network Log         | |
|          |          |          |  |  Form Capture Data   | |
|          |          |          |  |  db_checks (YAML)    | |
|          |          |          |  |  DB Verify Memory    | |
|          |          |          |  +----------------------+ |
+----------+----------+----------+---------------------------+
|       LLM Provider (OpenAI / Gemini / DeepSeek / ...)      |
+------------------------------------------------------------+

execution_mode: cli
+------------------------------------------------------------+
|              Orchestrator (_run_pipeline_cli)               |
|   3 independent claude -p calls, each a subprocess         |
+------------------------------------------------------------+
|  Step 1: UI Execution        |  claude -p + Playwright MCP |
|  (run_scenario_cli)          |  --mcp-config <playwright>  |
|                              |  → CLIResult JSON           |
+------------------------------+-----------------------------+
|  Step 2: DB Verification     |  claude -p + DB MCP         |
|  (verify_db_cli)             |  --mcp-config <database>    |
|                              |  → DataVerification[]       |
+------------------------------+-----------------------------+
|  Step 3: Lesson Generation   |  claude -p (no MCP)         |
|  (generate_lesson_cli)       |  → {lesson, tags}           |
+------------------------------+-----------------------------+
|                 stderr streamed in real-time                |
|            stdout collected → JSON parsed                   |
+------------------------------------------------------------+
|                  Project Profile (YAML)                     |
|     per-project config: env, auth, LLM, boundaries, tools  |
+------------------------------------------------------------+
```

---

## Quick Start

### 1. Install

```bash
cd Test-Agent
pip install -e .
```

### 2. Set API Key

```bash
# Agent mode (OpenAI)
export OPENAI_API_KEY=your_key

# Or Gemini (free tier available)
export GEMINI_API_KEY=your_key

# CLI mode (Claude) — requires Claude Code CLI installed
# npm install -g @anthropic-ai/claude-code
```

### 3. Create a Project Profile

```bash
cp profiles/example_project.yaml profiles/my_project.yaml
```

Edit key fields (full reference below):

```yaml
project:
  name: "My E-commerce"
model:
  provider: "openai"
  model_name: "gpt-4o"
environment:
  base_url: "https://staging.myapp.com"
code:
  root_dir: "/home/user/my-project"
```

### 4. Validate the Profile

```bash
test-agent validate-profile profiles/my_project.yaml
```

### 5. Run a Test

```bash
# Pass scenario directly
test-agent test \
  -p profiles/my_project.yaml \
  -s "Add product to cart, checkout, fill address, pay, confirm order"

# Use a named scenario from the profile
test-agent test -p profiles/my_project.yaml -s checkout

# Run with Claude Code CLI instead of OpenAI Agents SDK
test-agent test -p profiles/my_project.yaml -s checkout --mode cli

# List all available scenarios
test-agent test -p profiles/my_project.yaml
```

---

## Workflow

```
       You write                        Agent does automatically
+---------------------+     +------------------------------------------+
|  1. Profile YAML    |     |  3. Load Memory -- retrieve past lessons |
|  (one-time, per     |     |  4. Break scenario into steps            |
|   project)          |     |  5. Execute each step via Playwright     |
|                     |---->|  6. Handle obstacles (login/popup/wait)  |
|  2. --scenario      |     |  7. Screenshot key steps                 |
|  (one sentence per  |     |  8. Query DB to verify data correctness  |
|   test run)         |     |  9. Output structured report (pass/fail) |
+---------------------+     |  10. LessonWriter extracts lesson to     |
                             |      memory for next run                 |
                             +------------------------------------------+
```

### Detailed Execution Flow

```
Input: -s "Buy product A, verify order data"
        |
        v
1. Load Profile
   +-- Read project config (env / auth / boundaries)
   +-- Create LLM (OpenAI / Gemini / ...)
   +-- Retrieve Memory -- match past lessons by tags, inject into prompt
   +-- Start MCP servers (Playwright + DB)
        |
        v
2. ReAct Execution Loop
   +-- Think: "First step: open product page"
   +-- Act:   browser_navigate -> product page
   +-- Observe: snapshot (full ARIA tree for current turn, URL/title only for old turns)
   +-- Think: "Found Add to Cart button ref=e144"
   +-- Act:   browser_click ref=e144 (auto-snapshot provides fresh refs after click)
   +-- Observe: click result with updated snapshot
   +-- ...continue until flow is complete...
   |
   |   On obstacles:
   |   +-- Needs login -> auto-login with profile test account
   |   +-- Popup -> auto-dismiss
   |   +-- Slow page load -> wait
   |
   +-- Each step records: step_number, action, status, screenshot
        |
        v
3. Data Verification (triggered after flow completes)
   +-- UI agent extracts business data from success page (order_id / total / email)
   +-- Calls verify_in_db(extracted_data) tool
   |   +-- Spawns independent DB Sub-Agent (new Runner.run, fresh context)
   |       +-- Injects Network Log (browser mutation API requests + request/response body)
   |       +-- Injects Form Capture data (traditional form POSTs captured before click)
   |       +-- Injects Live Schema (real-time describe_table for db_checks tables)
   |       +-- Injects db_checks verification checklist (if configured in scenario)
   |       +-- Optional grep_code to look up coded values in the codebase
   |       +-- Executes SELECT queries to verify each check
   |       +-- Returns DataVerification JSON array to UI agent
   +-- UI agent includes results in the report's data_verifications (unmodified)
        |
        v
4. Stuck Detection (StuckDetector, monitors throughout)
   +-- 3 consecutive identical tool calls -> stuck
   +-- Last 5 results all identical -> stuck
   +-- Budget ratio exceeded without report -> stuck
   -> Auto-switch to Analysis mode: stop tools, synthesize evidence, output report
        |
        v
5. Output Report
   +-- Terminal: colored table (Steps + Verifications)
   +-- JSON: full structured report
   +-- trace.md / trace.jsonl: full execution trace (for debugging)
   +-- usage summary: LLM token usage stats
        |
        v
6. LessonWriter (independent LLM call)
   +-- Analyzes this run's steps / issues / next_steps
   +-- Generates 1 actionable lesson + tags (checkout, p-16227, bank-transfer...)
   +-- Writes to Memory JSONL -- auto-retrieved for the same scenario next time
```

---

## Memory System (RAG)

After each run, the `LessonWriter` agent extracts an actionable lesson with tags and writes it to JSONL. On the next run of the same scenario, relevant lessons are retrieved by tag matching and injected into the system prompt.

**Lessons take priority over scenario steps.** If a lesson says "don't use the modal View Cart button, navigate directly to /cart", the agent will skip the corresponding scenario step.

```yaml
# Profile config
memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"   # {project_name} auto-replaced
  max_entries_in_prompt: 2                 # max lessons injected per run
```

---

## Token Control

Playwright product pages can produce 900+ line ARIA trees (~55K chars). `MCPToolOutputFilter` handles this in two layers:

| Turn | Strategy |
|------|----------|
| **Current turn (recent)** | Keep full interactive ARIA tree (buttons, links, textboxes + refs), model sees all actionable elements |
| **Old turns** | Drop snapshot ARIA tree, keep only `### Page` (URL + title), mark `[snapshot omitted]` |

Old turns only need "which page, what action" -- not every element on the page. This reduces old snapshots from ~7,000 tokens/each to ~50 tokens/each, significantly lowering TPM pressure on long runs.

**DB Sub-Agent has independent context**: DB verification runs in its own `Runner.run()`, starting from scratch, carrying no UI flow history. A single request is only ~3-5K tokens.

**Rate limit retry**: All LLM clients set `max_retries=5`, SDK handles 429 with automatic exponential backoff, no scenario restart needed.

## Auto-Snapshot

`browser_click` and `browser_navigate` may trigger page reloads, making snapshot element refs stale. The system automatically calls `browser_snapshot` after these actions via hooks, injecting fresh refs into the next turn's context to replace stale ones.

The agent prompt relies on auto-snapshot and does **not** require a manual `browser_snapshot` before each click. The agent only calls `browser_snapshot` explicitly when it needs a different depth or the snapshot appears incomplete.

The `MCPToolOutputFilter`'s same-page boundary logic ensures snapshots within the same page URL are not truncated by old-turn compression (e.g. `browser_fill_form` without URL change is treated as the same page).

## Form Capture

Traditional `<form method="POST">` submissions cause full-page navigation. Playwright MCP's `browser_network_requests` only captures fetch/XHR resource types, not document-type navigation requests. This means traditional form POSTs (login forms, newsletter subscriptions, address forms) would be invisible to the DB agent.

**Solution**: Before every `browser_click`, the hooks check if the clicked element is inside a `<form>` using `browser_evaluate`. If yes, all field values are extracted via `FormData` and recorded. These form captures are merged into the network log passed to the DB agent, formatted as:

```
[POST] https://example.com/newsletter => [form submit]
  Request body: field1=value1&field2=value2
```

Form captures go through the same `allowed_domains` filter and are deduplicated against XHR mutations (if the form was also submitted via AJAX, the XHR entry takes precedence).

## Network Log

When the UI agent calls `verify_in_db`, the system automatically fetches the browser's network request log via Playwright MCP, filters it to business API mutations, and injects it into the DB agent's prompt.

The DB agent sees actual API request bodies (e.g. `{"orders_ref": "116NZXM27", "payment_method_id": 5}`), giving it precise field names and values for WHERE conditions.

- **Mutation-only**: Only POST/PUT/PATCH/DELETE requests are kept; GET requests are excluded
- **Domain filter**: Only requests to `allowed_domains` are kept
- **Noise filter**: Known third-party / tracking URLs are excluded (Google Analytics, Forter, Facebook, etc.) via `_NOISE_PATTERNS`
- **Polling dedup**: URLs appearing 4+ times are treated as polling and collapsed
- **Form capture merge**: Traditional form POST data is appended to the network log
- **Zero overhead**: UI agent doesn't need to do anything; `verify_in_db` handles it internally

## DB Schema Cache + Live Schema

### Schema Cache

The DB Sub-Agent's first run calls `describe_table` to explore table structures, automatically cached to a local JSON file:

```
memory/db_schema_{project_name}.json
```

On subsequent runs, `verify_in_db` keyword-matches relevant tables from the cache (using UI data + network log), injecting up to 20 table schemas into the DB agent prompt. Cache is keyed by `database.table` and auto-appends new tables.

### Pre-fetch Schema Cache

Batch-fetch all table structures before the first run to avoid wasting DB agent turns on `describe_table`:

```bash
# Fetch all tables for a specific database
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml -d inkstation

# Incremental (skip already cached)
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml -d inkstation --skip-cached

# All databases (auto-skips mysql/sys/information_schema/performance_schema)
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml
```

### Live Schema (for db_checks)

When `db_checks` are configured, tables referenced in the checks are fetched in real-time via MCP `describe_table` at run time, bypassing the local cache. This ensures the DB agent always sees the latest column definitions even if the schema has changed.

## Scenario-Level db_checks

Profile scenarios can include a DB verification checklist. The DB agent receives this checklist and executes exactly those checks -- no more, no less -- significantly reducing LLM calls.

### Plain-string format (simple checks)

```yaml
scenarios:
  checkout:
    description: |
      "Test checkout flow..."
    db_checks:
      - "orders table contains the order, order_total is correct"
      - "payment_method is Bank Transfer"
```

### Structured format (precise checks with hints)

```yaml
scenarios:
  transferGroundToLabel:
    description: |
      "Transfer inventory from ground to label..."
    db_checks:
      - table: "inkstation_barcode_db.inventory_stock_details"
        find_by: "type_id=payload.data.type_id, ref_id=payload.data.ref_id, inventory_id=payload.data.inventory_id"
        verify: "total decreased by payload.totalTransferQty"
        hint: "use diff_qty from logs to confirm, do NOT infer from current total alone"

      - table: "inkstation_barcode_db.inventory_stock_details"
        find_by: "type_id=payload.transferTo.inventoryTypeId, ref_id=payload.transferTo.id"
        verify: "row exists with total increased"

      - table: "inkstation_barcode_db.inventory_stock_details_logs"
        find_by: "inventory_id from payload, action_type=2, diff_qty=-totalTransferQty"
        verify: "created_at is recent"
```

Structured fields:

| Field | Purpose | Required |
|-------|---------|----------|
| `table` | Which table to query (auto describe_table) | Yes |
| `find_by` | WHERE conditions; `payload.*` references network log values | No |
| `verify` | Expected result -- supports operators like `decreased_by`, `>=` | No |
| `hint` | Judgment guidance for the DB agent (e.g. which approach to use/avoid) | No |

Both formats can be mixed. Scenarios without `db_checks` still use auto-discovery (network log + schema hints + code grep).

## DB Verify Memory

After the DB agent passes all checks, the LLM auto-generates a structured `.md` knowledge document stored under `memory/db_verify/{project}/`. On subsequent runs, the system scans these files' frontmatter, lets the LLM pick the most relevant one, and injects it into the DB agent prompt.

**Memory file contents (all LLM-generated):**
- Frontmatter: `name`, `description`, `tags`, `tables`, `confidence`
- Body: Primary tables (roles + key columns), Common joins, Verification flow (generic placeholders, no hardcoded values), Common pitfalls (column name traps, etc.)

**Workflow:**

```
First checkout run (no memory):
  DB agent explores on its own -> all pass
  -> LLM generates memory .md file (table relationships, joins, pitfalls)
  -> Saved to memory/db_verify/My_Project/checkout_20260402T130646.md

Second checkout run (with memory):
  Scan memory frontmatter -> LLM picks most relevant -> inject into DB agent prompt
  -> DB agent sees "use products_quantity not products_qty"
  -> Writes correct SQL on first try, zero retries
```

```
memory/db_verify/
+-- My_Project/                       <- isolated per project
    +-- checkout_20260402T130646.md   <- LLM-generated verification knowledge
```

---

## CLI Reference

```bash
# Execute a test scenario
test-agent test [OPTIONS]

Options:
  -p, --profile TEXT     Project Profile YAML path (required)
  -s, --scenario TEXT    Test scenario description or named scenario from profile
  -o, --output TEXT      Report output file path (defaults to terminal)
  -m, --mode TEXT        Execution mode: "agent" (default) or "cli" (Claude Code CLI)
  --max-steps INT        Override max_steps from profile
  -v, --verbose          Enable verbose logging

# Validate a profile
test-agent validate-profile profiles/my_project.yaml
```

---

## LLM Switching

Change the `model` field in your profile:

```yaml
# OpenAI
model:
  provider: "openai"
  model_name: "gpt-4o"

# Gemini (free tier available)
model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

# DeepSeek
model:
  provider: "deepseek"
  model_name: "deepseek-chat"

# Groq
model:
  provider: "groq"
  model_name: "llama-3.3-70b-versatile"

# Any OpenAI-compatible API
model:
  provider: "custom"
  model_name: "your-model"
  base_url: "https://your-api.example.com/v1"
  api_key_env: "YOUR_API_KEY"
```

API keys are auto-detected by provider (`GEMINI_API_KEY`, `OPENAI_API_KEY`, etc.), or explicitly set via `api_key_env`.

---

## Claude Code CLI Mode

When `execution_mode: cli` (or `--mode cli`), the entire pipeline runs through Claude Code CLI (`claude -p`) instead of the OpenAI Agents SDK. No OpenAI API key is needed — only a working Claude Code CLI installation.

### How It Works

The orchestrator makes 3 sequential `claude -p` subprocess calls:

1. **UI Execution** (`run_scenario_cli`) — Claude receives the scenario prompt + Playwright MCP config, executes the browser flow, and returns a structured JSON with extracted business data and step results.

2. **DB Verification** (`verify_db_cli`) — Claude receives the extracted data + db_checks + live schema + network log, connects to the DB MCP server, runs SELECT queries, and returns a DataVerification JSON array.

3. **Lesson Generation** (`generate_lesson_cli`) — Claude receives the run report summary (no MCP needed) and returns a lesson + tags JSON for the memory system.

### Key Differences from Agent Mode

- **MCP lifecycle**: Each `claude -p` subprocess starts its own MCP servers via `--mcp-config` and tears them down on exit. The parent process does not connect/disconnect MCP servers.
- **Streaming output**: stderr is streamed line-by-line in real-time via `logger.info`, so you can follow tool calls, MCP connections, and thinking progress live in the terminal.
- **Permissions**: Uses `--dangerously-skip-permissions` since `-p` mode is non-interactive and cannot prompt for tool approval.
- **Timeout**: Default 600 seconds (10 minutes) for UI execution, 300 seconds for DB verification, 60 seconds for lesson generation.
- **No StuckDetector**: Claude Code manages its own context and retry logic internally.

### Prerequisites

```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Verify installation
claude --version
```

### Usage

```bash
# Via CLI flag (overrides profile)
test-agent test -p profiles/my_project.yaml -s checkout --mode cli

# Via profile (set once)
# boundaries:
#   execution_mode: "cli"
```

---

## Full Profile Reference

```yaml
project:
  name: "Project Name"                          # required
  description: "Project description"

# --- LLM ---
model:
  provider: "openai"                            # openai | gemini | deepseek | groq | custom
  model_name: "gpt-4o"
  api_key_env: "OPENAI_API_KEY"                # auto-detected by provider if omitted
  base_url: null                                # custom endpoint (for custom provider)

# --- Environment ---
environment:
  type: "web"
  base_url: "https://staging.example.com"
  start_command: "npm run dev"                 # optional, local start command
  health_check_url: "/api/health"              # optional

# --- Auth ---
auth:
  method: "form"                                # form | token | cookie | none
  login_url: "/login"
  test_accounts:
    - role: "admin"
      username_env: "TEST_ADMIN_USER"          # env var name (not plaintext password)
      password_env: "TEST_ADMIN_PASS"
    - role: "user"
      username_env: "TEST_USER_USER"
      password_env: "TEST_USER_PASS"

# --- Code ---
code:
  root_dir: "/absolute/path/to/project"        # required, local repo absolute path
  branch: "main"
  entry_dirs:
    - "src/pages"
    - "src/api"
  config_files:
    - ".env.staging"

# --- Memory ---
memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"        # {project_name} auto-replaced
  max_entries_in_prompt: 2                     # max lessons injected per run

# --- MCP Servers ---
mcp_servers:
  playwright:
    enabled: true
    command: "npx"
    args: ["@playwright/mcp@latest", "--browser", "chromium", "--timeout-action", "15000"]
    cwd: "./artifacts/playwright"              # snapshot/screenshot output directory
    cache_tools_list: true
    client_session_timeout_seconds: 30         # must be > timeout-action / 1000
    allowed_tools:                             # restrict browser tools available to agent
      - browser_navigate
      - browser_snapshot
      - browser_click
      - browser_type
      - browser_fill_form
      - browser_select_option
      - browser_press_key
      - browser_handle_dialog
      - browser_wait_for
      - browser_take_screenshot
      - browser_network_requests
  database:
    enabled: true
    role: database                              # marks as DB server, routed to DB Sub-Agent
    command: "node"
    args: ["./mcp-servers/db-mcp/index.js"]
    env:
      DB_TYPE: "mysql"
      DB_HOST_ENV: "DB_HOST"
      DB_PORT_ENV: "DB_PORT"
      DB_USER_ENV: "DB_USER"
      DB_PASSWORD_ENV: "DB_PASSWORD"
      DB_ALIASES_ENV: "DB_ALIASES"             # optional, database name aliasing

# --- Boundaries ---
boundaries:
  execution_mode: "agent"                       # "agent" (OpenAI Agents SDK) or "cli" (Claude Code CLI)
  readonly: true
  forbidden_actions:                            # SQL blocklist
    - "DELETE FROM"
    - "DROP TABLE"
    - "INSERT INTO"
    - "UPDATE"
  max_steps: 40
  max_turns: 20
  stuck_budget_ratio: 0.85                      # switch to Analysis mode above this ratio
  allowed_domains:
    - "staging.example.com"

# --- Predefined Scenarios (optional) ---
scenarios:
  # Simple format (plain description)
  login: |
    Test login flow with valid and invalid credentials.

  # Structured format with DB verification checklist
  checkout:
    description: |
      Add product to cart and complete checkout via Bank Transfer.
    db_checks:
      - "orders table contains the order, order_total is correct"
      - "payment_method is Bank Transfer"

  # Structured db_checks with table/find_by/verify/hint
  transfer:
    description: |
      Transfer inventory from ground to label.
    db_checks:
      - table: "warehouse_db.inventory_stock_details"
        find_by: "type_id=payload.data.type_id, ref_id=payload.data.ref_id"
        verify: "total decreased by payload.totalTransferQty"
        hint: "use logs diff_qty to confirm"
```

---

## Project Structure

```
src/universal_debug_agent/
+-- main.py                  # CLI entry point (typer) -- test / validate-profile
+-- config.py                # YAML Profile loading
+-- schemas/
|   +-- profile.py           # ProjectProfile + ScenarioConfig + DBCheck + ModelConfig + BoundariesConfig
|   +-- report.py            # ScenarioReport + ScenarioStep + DataVerification
+-- agents/
|   +-- brain.py             # create_brain_agent (ReAct + Analysis modes)
|   +-- db_agent.py          # DB Sub-Agent (called inside verify_in_db tool)
|   +-- prompts.py           # System prompts (ReAct + Analysis dual-mode)
+-- orchestrator/
|   +-- state_machine.py     # InvestigationOrchestrator + StuckDetector
|   +-- claude_executor.py   # Claude Code CLI pipeline: run_scenario_cli,
|   |                        #   verify_db_cli, generate_lesson_cli
|   |                        #   shared _run_claude_cli helper (stderr streaming)
|   +-- hooks.py             # InvestigationHooks: tool monitoring, auto-snapshot,
|   |                        #   form capture (browser_evaluate before click),
|   |                        #   stuck detection, trace recording
|   +-- input_filters.py     # MCPToolOutputFilter:
|                            #   current turn -> full interactive ARIA tree
|                            #   old turns    -> snapshot dropped, URL/title only
|                            #   same-page boundary -> don't truncate within same URL
+-- models/
|   +-- factory.py           # LLM factory (OpenAI/Gemini/DeepSeek/...)
+-- memory/
|   +-- store.py             # JSONL storage: tag inverted index + scenario similarity search
|   +-- lesson.py            # LessonWriter: extract lesson + tags after run
+-- observability/
|   +-- llm_usage.py         # Per-run token usage stats (JSONL)
|   +-- trace_recorder.py    # Execution trace to disk (trace.md + trace.jsonl)
+-- mcp/
|   +-- factory.py           # MCP server factory (Playwright + DB)
+-- tools/
    +-- auth_tools.py        # get_test_account(role) -- read test accounts from profile
    +-- code_tools.py        # read_file / grep_code / list_directory (sandboxed)
    +-- db_tool.py           # verify_in_db -- spawns DB Sub-Agent; manages schema cache;
    |                        #   auto-fetches network log + form captures + live schema
    +-- report_tool.py       # submit_report -- structured report submission, exits ReAct loop
```

---

## V1 Scope

| In Scope | Out of Scope |
|----------|--------------|
| Execute any business flow (Playwright) | Auto-modify code |
| Data verification (DB read-only queries) | Write to database |
| Scenario-level db_checks (plain + structured) | Auto-create PRs |
| Dual execution engine: OpenAI Agents SDK or Claude Code CLI | Solve CAPTCHA / 2FA |
| Auto-snapshot + same-page boundary | Unlimited external domain browsing |
| Form capture for traditional POST submissions | |
| Network log + form data auto-injection to DB agent | |
| Auto-handle login / popup / loading | |
| Read local code to assist understanding | |
| Structured pass/fail reports | |
| JSONL test memory + RAG lesson retrieval | |
| Multi-LLM provider switching | |

## Roadmap

- **V2**: Contract solidification -- LLM explores once, generates verification contract, deterministic reuse afterwards (no ReAct needed per run)
- **V3**: Multi-agent parallel execution + intelligent test strategy generation

See [docs/PLAN_V1.md](docs/PLAN_V1.md) | [docs/PLAN_V2.md](docs/PLAN_V2.md) | [docs/PLAN_V3.md](docs/PLAN_V3.md).

## License

MIT
