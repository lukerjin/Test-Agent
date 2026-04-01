# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (development)
pip install -e ".[dev]"

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_state_machine.py -v

# Run a specific test
pytest tests/test_state_machine.py::TestStuckDetector::test_consecutive_calls -v

# Validate a project profile
test-agent validate-profile profiles/my_project.yaml

# Run a test scenario
test-agent test -p profiles/my_project.yaml -s "scenario description" --verbose
```

No linter is configured. Tests are the only quality gate.

## Architecture

This is an AI-powered E2E test execution agent. Given a natural language scenario, the agent executes it in a browser, collects evidence, and verifies database state.

### Dual-Mode Execution

The agent operates in two modes, managed by `orchestrator/state_machine.py`:

1. **ReAct Mode** (`temperature=0.2`): Default execution mode. The agent iteratively calls tools (Playwright, DB, code tools) to work through the scenario.
2. **Analysis Mode** (`temperature=0.7`, `output_type=ScenarioReport`): Fallback when the agent gets stuck. Synthesizes a structured report from collected evidence.

Mode switching is triggered by `StuckDetector` raising `SwitchToAnalysisMode`. Stuck conditions: 3+ consecutive identical tool calls, last 5 results all identical, or 85%+ budget used without submitting a report.

### Key Data Flow

```
CLI (main.py) → load ProjectProfile (YAML) → create LLM model
→ connect MCP servers (Playwright + DB) → InvestigationOrchestrator.run()
→ ReAct loop with InvestigationHooks monitoring → StuckDetector
→ (if stuck) Analysis mode → ScenarioReport → Rich terminal output + JSON file
```

### Module Responsibilities

- **`schemas/profile.py`**: Pydantic models for project YAML config (`ProjectProfile`, `ModelConfig`, `BoundariesConfig`, etc.)
- **`schemas/report.py`**: Output types — `ScenarioReport`, `ScenarioStep`, `DataVerification`, `Evidence`
- **`models/factory.py`**: Multi-provider LLM factory. Non-OpenAI providers use `_CompatTransport` to strip OpenAI-specific fields (`strict`, `parallel_tool_calls`) from requests.
- **`mcp/factory.py`**: Creates Playwright and Database MCP server instances; resolves `_ENV`-suffixed values to environment variables.
- **`orchestrator/hooks.py`**: `InvestigationHooks` feeds tool call data to `StuckDetector` and records traces.
- **`orchestrator/input_filters.py`**: Truncates large MCP responses to prevent context bloat.
- **`memory/store.py`**: JSONL-based memory of past investigations; injected into the system prompt to avoid repeating dead ends.
- **`observability/trace_recorder.py`**: Writes `trace.md` + `trace.jsonl` to `artifacts/` for post-run debugging.

### Project Profile (YAML)

Every project needs a YAML profile (see `profiles/example_project.yaml`). Key sections:
- `model`: LLM provider and model name
- `environment`: `base_url`, optional `start_command` and `health_check_url`
- `auth`: Login method, URL, test accounts (can reference env vars)
- `code`: `root_dir` for the project being tested (enables code browsing tools)
- `mcp_servers`: Playwright and DB MCP server configs
- `boundaries`: `max_steps`, `max_turns`, `forbidden_actions` (SQL deny list), `allowed_domains`

### Agent Tools

The agent has access to three sets of tools beyond MCP servers:
- **`tools/auth_tools.py`**: `get_test_account(role)` — retrieves test credentials by role
- **`tools/code_tools.py`**: `read_file`, `grep_code`, `list_directory` — sandboxed to `code.root_dir`
- **`tools/report_tool.py`**: `submit_report(report_json)` — structured output submission (exits ReAct loop)
