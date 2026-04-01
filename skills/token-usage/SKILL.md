---
name: token-usage
description: Analyze and compare LLM token consumption across test agent runs. Use when the user asks about cost, token usage, LLM calls, or wants to compare efficiency between runs.
---

# Token Usage Analysis

## Overview

Analyze LLM token consumption from the test agent's usage logs. Supports comparing runs, breaking down UI vs DB agent costs, and tracking efficiency improvements over time.

## Data Source

```
usage/inkstation_agent_testing/llm_calls.jsonl
```

Each line is a JSON object with: `run_id`, `phase` (react | db_verify), `call_index`, `timestamp`, `input_tokens`, `output_tokens`, `cached_tokens`, `scenario`, etc.

## How to Analyze

Run this Python script, adjusting the filters as needed:

```python
import json
from collections import defaultdict

lines = open('usage/inkstation_agent_testing/llm_calls.jsonl').readlines()
calls = [json.loads(l) for l in lines if l.strip()]

runs = defaultdict(list)
for c in calls:
    runs[c['run_id']].append(c)

sorted_runs = sorted(runs.items(), key=lambda kv: kv[1][-1]['timestamp'], reverse=True)
```

## Standard Output Format

When the user asks about token usage, produce a table like:

```
Time                  UI  DB  Tot  UI Input  DB Input     Total Cache%  Scenario
```

For a single run breakdown:

```
=== UI Agent (react) ===
  #1  input=  4695  cached=  3712  output=  28
  ...
=== DB Agent (db_verify) ===
  #1  input=  3104  cached=     0  output=  82
  ...
=== Total ===
  Calls: 15 (UI=10, DB=5)
  Input: 133,816  Cached: 87,808 (66%)
  Output: 1,629
```

## Common Queries

1. **"看看最近的 token 消耗"** — Show last 10 runs with UI/DB breakdown
2. **"对比 checkout 和 newsletter 的消耗"** — Filter by scenario, group and average
3. **"修复前后对比"** — Compare runs before/after a specific timestamp
4. **"DB agent 花了多少"** — Filter phase=db_verify, show call-by-call breakdown
5. **"成本估算"** — Apply pricing: input $0.10/1M, cached $0.025/1M, output $0.40/1M

## Cost Estimation

```python
INPUT_PRICE = 0.10 / 1_000_000   # per token
CACHED_PRICE = 0.025 / 1_000_000
OUTPUT_PRICE = 0.40 / 1_000_000

uncached = total_input - total_cached
cost = uncached * INPUT_PRICE + total_cached * CACHED_PRICE + total_output * OUTPUT_PRICE
```

## Key Metrics to Watch

- **UI calls**: Should be 8-12 for newsletter, 18-22 for checkout
- **DB calls**: Should be 5-7
- **Cache hit rate**: Should be >60%
- **Input per call**: >15K suggests snapshot bloat
