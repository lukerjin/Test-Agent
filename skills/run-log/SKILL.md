---
name: run-log
description: Query and analyze test agent run logs — trace, errors, final output, LLM calls. Use when the user asks to check logs, investigate a run, see what happened, or debug a failed test.
---

# Run Log Analysis

## Overview

Investigate test agent runs: what tools were called, what errors occurred, what the agent output, and how many LLM calls were made. Covers trace, errors, final output, and per-call token usage.

## Data Sources

All data lives under `usage/{project_name}/` (default: `usage/inkstation_agent_testing/`).

### Per-run files (in `runs/{run_id}/`)

| File | Content |
|---|---|
| `trace.md` | Human-readable execution trace: every LLM response, tool call, tool result, mode switches, DB handoffs |
| `trace.jsonl` | Machine-readable trace (same data, JSON per line) |
| `final_output.txt` | Raw final output from the agent (ScenarioReport JSON or unstructured text) |
| `error.txt` | Stack trace + error details (only exists if the run crashed) |

### Aggregate files

| File | Content |
|---|---|
| `llm_calls.jsonl` | Every LLM call across all runs. Fields: `run_id`, `phase` (react/analysis/db_verify), `call_index`, `timestamp`, `input_tokens`, `output_tokens`, `cached_tokens`, `scenario` |
| `llm_runs.jsonl` | One summary per run. Fields: `run_id`, `timestamp`, `scenario`, `provider`, `model`, `phases`, `call_count`, `input_tokens`, `output_tokens`, `total_tokens`, `cached_tokens` |

## How to Query

### Find the latest N runs

```bash
# Latest 5 runs sorted by modification time
ls -td usage/inkstation_agent_testing/runs/*/ | head -5
```

### Find runs by scenario

```python
import json
lines = open('usage/inkstation_agent_testing/llm_runs.jsonl').readlines()
runs = [json.loads(l) for l in lines if l.strip()]
# Filter by scenario keyword
checkout_runs = [r for r in runs if 'checkout' in r.get('scenario', '').lower()]
# Sort by time, most recent first
checkout_runs.sort(key=lambda r: r['timestamp'], reverse=True)
```

### Find failed runs (with error.txt)

```bash
find usage/inkstation_agent_testing/runs -name "error.txt" -printf "%T@ %p\n" | sort -rn | head -10
# On macOS (no -printf):
find usage/inkstation_agent_testing/runs -name "error.txt" | xargs ls -lt | head -10
```

### Read execution trace

```bash
# Most recent run's trace
cat "$(ls -td usage/inkstation_agent_testing/runs/*/trace.md | head -1)"
```

### Check what tools were called in a run

```bash
# Extract tool names from trace
grep "Tool Start:" usage/inkstation_agent_testing/runs/{run_id}/trace.md
```

### Check if a specific tool/feature was used

```bash
# Was browser_snapshot called?
grep -l "browser_snapshot" usage/inkstation_agent_testing/runs/*/trace.md | tail -5

# Was DB verification triggered?
grep -l "db_handoff\|DB Agent Handoff" usage/inkstation_agent_testing/runs/*/trace.md | tail -5

# Was analysis mode triggered?
grep -l "mode_switch\|Switch To Analysis" usage/inkstation_agent_testing/runs/*/trace.md | tail -5
```

### Compare two runs

Read both `trace.md` files side by side. Key things to compare:
- Number of tool calls (more = less efficient)
- Whether the agent got stuck (repeated identical calls)
- Whether DB verification was attempted and succeeded
- Whether analysis mode was triggered (means the agent got stuck)

## Standard Analysis Flow

When the user asks "look at the latest log" or "check what happened":

1. **Find the run**: Use `llm_runs.jsonl` to find the latest run(s) matching the context
2. **Read the trace**: `trace.md` for the human-readable execution flow
3. **Check for errors**: Look for `error.txt` in the run directory
4. **Check final output**: `final_output.txt` for the ScenarioReport
5. **Check token usage**: Filter `llm_calls.jsonl` by `run_id` for per-call breakdown

## Common Questions

1. **"最新跑的 log"** — Find latest run, read trace.md, summarize key actions and result
2. **"为什么失败了"** — Check error.txt first, then trace.md for where it went wrong
3. **"playwright 有没有启动"** — Look for `browser_navigate` in trace.md
4. **"DB verify 做了什么"** — Look for `db_handoff` and `[DB]` entries in trace
5. **"agent 卡在哪里"** — Look for repeated tool calls or `mode_switch` in trace
6. **"用了什么模型"** — Check `llm_runs.jsonl` for the run's `model` and `provider` fields
7. **"对比最近几次"** — Read `llm_runs.jsonl`, sort by timestamp, compare call_count/tokens/phases
