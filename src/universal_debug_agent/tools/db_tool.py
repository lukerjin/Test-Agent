"""DB verification tool — runs a fresh DB agent to verify extracted UI data."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Runner, RunConfig, Tool, function_tool
from agents.mcp import MCPServerStdio

from universal_debug_agent.agents.db_agent import DB_MAX_TURNS, DBVerificationOutput
from universal_debug_agent.observability.trace_recorder import ExecutionTraceRecorder

logger = logging.getLogger(__name__)


def _serialize_tool_result(result: Any) -> str:
    """Extract text from an MCP tool result.

    MCP results may be structured content like [{"type": "text", "text": "..."}]
    or a plain string. Using str() on the structured form produces Python repr
    with single quotes, breaking JSON parsing downstream.
    """
    if not result:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            elif hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    if hasattr(result, "text"):
        return result.text
    return str(result)


# Module-level state — configured by state_machine before the run starts
_db_mcp_servers: list[MCPServerStdio] = []
_model: Any = None
_trace_recorder: ExecutionTraceRecorder | None = None
_cache_path: Path | None = None
_playwright_server: MCPServerStdio | None = None
_allowed_domains: list[str] = []
_code_root_dir: str = ""
_usage_tracker: Any = None  # LLMUsageTracker instance
_db_checks: list[str] = []  # Scenario-specific verification hints
_scenario_name: str | None = None  # For DB verify memory


def configure(
    db_mcp_servers: list[MCPServerStdio],
    model: Any,
    trace_recorder: ExecutionTraceRecorder | None = None,
    cache_path: Path | None = None,
    playwright_server: MCPServerStdio | None = None,
    allowed_domains: list[str] | None = None,
    evidence_collector: Any = None,
    code_root_dir: str = "",
    usage_tracker: Any = None,
    db_checks: list[str] | None = None,
    scenario_name: str | None = None,
) -> None:
    """Configure the DB tool. Call this before running the UI agent."""
    global _db_mcp_servers, _model, _trace_recorder, _cache_path, _playwright_server, _allowed_domains, _code_root_dir, _usage_tracker, _db_checks, _scenario_name
    _db_mcp_servers = db_mcp_servers
    _model = model
    _trace_recorder = trace_recorder
    _cache_path = cache_path
    _playwright_server = playwright_server
    _allowed_domains = allowed_domains or []
    _code_root_dir = code_root_dir
    _usage_tracker = usage_tracker
    _db_checks = db_checks or []
    _scenario_name = scenario_name


def _db_verify_memory_dir() -> Path | None:
    """Return the project-specific directory for DB verify memory files."""
    if _cache_path is None:
        return None
    # Use schema cache filename to derive project slug: db_schema_Inkstation_Agent_Testing.json → Inkstation_Agent_Testing
    project_slug = _cache_path.stem.replace("db_schema_", "")
    return _cache_path.parent / "db_verify" / project_slug



def _build_schema_index(cache: dict | None = None) -> tuple[dict[str, str], set[str]]:
    """Return cached table -> database mapping and known database names."""
    if cache is None:
        cache = _load_schema_cache()

    table_to_db: dict[str, str] = {}
    databases: set[str] = set()
    for key in cache:
        if "." not in key:
            continue
        database, table = key.split(".", 1)
        databases.add(database.lower())
        table_to_db.setdefault(table.lower(), database)

    return table_to_db, databases


def _extract_db_check_tables(db_checks: list[str] | None, cache: dict | None = None) -> list[str]:
    """Extract only explicit table names referenced in db_checks text.

    db_checks are verification goals, not free-form schema discovery hints. We
    only trust names that map exactly to known cached tables, including forms
    like:
    - ``orders``
    - ``orders_total.value``  -> table ``orders_total``
    - ``warehouse.orders``    -> table ``orders``
    """
    if not db_checks:
        return []

    table_to_db, databases = _build_schema_index(cache)
    if not table_to_db:
        return []

    known_tables = set(table_to_db)
    found: list[str] = []
    seen: set[str] = set()

    def _remember(table: str) -> None:
        if table in known_tables and table not in seen:
            seen.add(table)
            found.append(table)

    for check in db_checks:
        lowered = check.lower()

        # Dot-qualified references: db.table, table.column, db.table.column
        for dotted in re.findall(r"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)", lowered):
            parts = dotted.split(".")
            if len(parts) >= 2 and parts[0] in databases and parts[1] in known_tables:
                _remember(parts[1])
                continue
            if parts[0] in known_tables:
                _remember(parts[0])
                continue
            if len(parts) >= 3 and parts[1] in known_tables:
                _remember(parts[1])

        # Bare tokens: exact table names only, not substring keyword matches.
        for token in re.findall(r"[a-z][a-z0-9_]*", lowered):
            _remember(token)

    return found



def _get_model_client():
    """Extract AsyncOpenAI client and model name from the configured _model.

    Works with both native OpenAI (str) and OpenAIChatCompletionsModel instances,
    so memory LLM calls use the same provider/credentials as the main agent.
    """
    from openai import AsyncOpenAI

    if _model is None:
        return None, None

    # Native OpenAI: _model is a string like "gpt-4o"
    if isinstance(_model, str):
        import httpx
        return AsyncOpenAI(timeout=httpx.Timeout(30.0, connect=5.0)), _model

    # OpenAIChatCompletionsModel: stores client as ._client, model name as .model
    client = getattr(_model, "_client", None)
    model_name = getattr(_model, "model", None)
    if client and model_name:
        return client, model_name

    return None, None



async def _save_db_verify_memory(verifications: list[dict]) -> None:
    """Use LLM to generate rich verification memory from successful results.

    Always creates a new file. Old memories accumulate and are selected by
    relevance during load — no risk of overwriting unrelated memories.
    """
    mem_dir = _db_verify_memory_dir()
    if mem_dir is None:
        return

    # Only save when ALL checks passed
    has_query = [v for v in verifications if v.get("query")]
    if not has_query:
        logger.info("[db] memory save: no checks with queries, skipping")
        return
    all_pass = all(v.get("status") == "pass" for v in has_query)
    if not all_pass:
        statuses = [v.get("status") for v in has_query]
        logger.info(f"[db] memory save: not all pass ({statuses}), skipping")
        return

    client, model_name = _get_model_client()
    logger.info(f"[db] memory save: all_pass=True, client={'yes' if client else 'None'}, model={model_name}")

    # Build verification summary for LLM
    checks_text = ""
    for v in verifications:
        checks_text += f"- {v.get('check_name', '?')}\n"
        checks_text += f"  SQL: {v.get('query', '')}\n"
        checks_text += f"  Expected: {v.get('expected', '')}\n"
        checks_text += f"  Actual: {v.get('actual', '')}\n\n"

    scenario_label = _scenario_name or "unknown"

    prompt = f"""You are generating a DB verification memory document from successful test results.

## Scenario: {scenario_label}

## Successful verification checks

{checks_text}

## Instructions

Generate a markdown document starting with a YAML frontmatter block. The frontmatter MUST follow this exact format:

```
---
name: <short name, e.g. "Checkout DB verification mapping">
description: <one line under 120 chars, specific to business domain, e.g. "Stable table and column mapping for checkout order verification in legacy ecommerce flow">
type: project
scenario: {scenario_label}
domain: db_verification
status: active
tables: <comma-separated list of table names used, e.g. [orders, orders_products, orders_total]>
confidence: high
tags: <YAML list of relevant keywords for retrieval, include scenario name, table names, and business terms>
---
```

After the frontmatter, include these body sections:

## Summary
One sentence: which tables are used and for what.

## Primary tables
For each table used in the queries above:
- table name
- role (e.g. "main order record", "order line items")
- key columns (only the ones actually used in the queries)

## Common joins
List the JOIN conditions from the successful queries.

## Verification flow
Numbered steps: how to verify this scenario (derived from the actual queries).
IMPORTANT: Use generic placeholders (e.g. `<orders_ref>`, `<product_id>`, `<expected_total>`) instead of hardcoded values from this specific run. The flow should be reusable for any checkout, not just this one.

## Common pitfalls
List any column name gotchas (e.g. "use products_quantity not products_qty").
Only include pitfalls you can infer from the actual SQL — do not invent.

## Table ranking hint
Prioritize these tables during schema filtering (ordered by importance).

## When to reuse
Bullet list of scenario keywords where this memory applies.

## Freshness note
Always end with: "Treat this memory as guidance, not source of truth. If schema inspection or current code disagrees, trust current schema/code first."

Keep it concise. Only include facts supported by the queries above.
CRITICAL: Do NOT hardcode specific values from this run (order IDs, amounts, product IDs, emails). Use generic placeholders like `<orders_ref>`, `<total>`, `<product_id>`. This memory must be reusable across different runs of the same scenario."""

    if not client or not model_name:
        logger.warning("[db] no model client available, skipping memory generation")
        return

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=1000,
            temperature=0.2,
        )
        body = response.choices[0].message.content.strip()
        logger.info(f"[db] LLM generated memory body ({len(body)} chars)")
    except Exception as e:
        logger.error(f"[db] memory LLM generation failed: {type(e).__name__}: {e}")
        return

    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")

    # LLM generates full frontmatter + body. Inject `updated` timestamp.
    # Find the closing --- of frontmatter and insert updated field before it.
    if body.startswith("---"):
        end = body.find("---", 3)
        if end != -1:
            content = body[:end] + f"updated: {now}\n" + body[end:]
        else:
            content = body
    else:
        # LLM didn't generate frontmatter — wrap it
        content = f"---\nname: {scenario_label} DB verification mapping\nupdated: {now}\n---\n\n{body}"

    # Always create a new file — old memories accumulate, selected by relevance at load time
    mem_dir.mkdir(parents=True, exist_ok=True)
    slug = _scenario_name or now.replace(":", "-")
    # Add timestamp suffix to avoid overwriting previous memory for same scenario
    ts_suffix = now.replace(":", "").replace("-", "")[:15]
    file_path = mem_dir / f"{slug}_{ts_suffix}.md"
    try:
        file_path.write_text(content)
        logger.info(f"[db] created memory {file_path.name} ({len(content)} chars)")
    except Exception as e:
        logger.warning(f"[db] failed to save verify memory: {e}")


def _load_schema_cache() -> dict:
    if _cache_path is None or not _cache_path.exists():
        return {}
    try:
        return json.loads(_cache_path.read_text())
    except Exception:
        return {}


def _save_schema_cache(cache: dict) -> None:
    if _cache_path is None:
        return
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        _cache_path.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"[db] failed to save schema cache: {e}")


async def _fetch_network_log() -> str:
    """Call browser_network_requests on the Playwright MCP server to get API calls.

    Fetches all network requests with request bodies included, then filters to
    mutation requests (POST/PUT/PATCH/DELETE) which reveal the data contract
    between UI and backend — field names in payloads usually match DB columns.
    """
    if _playwright_server is None:
        return ""

    try:
        result = await _playwright_server.call_tool(
            "browser_network_requests",
            {"requestBody": True, "responseBody": True, "requestHeaders": False, "static": False},
        )
    except Exception as e:
        logger.warning(f"[db] failed to fetch network log: {e}")
        return ""

    # Extract text from the MCP result
    raw = _serialize_tool_result(getattr(result, "content", result))
    if not raw:
        return ""

    # Known third-party paths / domains to exclude from network log
    _NOISE_PATTERNS = (
        "/forter/", "forter.com",
        "google.com/", "google.com.au/",
        "googleads.", "googlesyndication.",
        "/tracking", "/collect", "/log",
        "/ccm/collect", "/rmkt/collect",
        "facebook.com", "facebook.net",
        "analytics", "gtag", "gtm",
    )

    # Filter to mutation requests (POST/PUT/PATCH/DELETE), skip third-party noise
    mutations: list[str] = []
    url_counts: dict[str, int] = {}
    last_was_mutation = False
    for line in raw.splitlines():
        line_stripped = line.strip()
        if any(line_stripped.startswith(f"[{m}]") for m in ("POST", "PUT", "PATCH", "DELETE")):
            # Skip known third-party / tracking requests
            lower = line_stripped.lower()
            if any(p in lower for p in _NOISE_PATTERNS):
                last_was_mutation = False
                continue
            # Domain filter: only keep requests to our app's domains
            if _allowed_domains and not any(d in line_stripped for d in _allowed_domains):
                last_was_mutation = False
                continue
            # Count URL occurrences for polling detection
            url_counts[line_stripped] = url_counts.get(line_stripped, 0) + 1
            mutations.append(line_stripped)
            last_was_mutation = True
        # Keep "Request body:" and "Response body:" lines that follow a kept mutation request
        elif (line_stripped.startswith("Request body:") or line_stripped.startswith("Response body:")) and last_was_mutation:
            mutations.append("  " + line_stripped)
        else:
            last_was_mutation = False

    if not mutations:
        return ""

    # Auto-blacklist high-frequency URLs (likely polling, not user actions)
    polling_urls = {url for url, count in url_counts.items() if count > 3}
    if polling_urls:
        filtered: list[str] = []
        skip_body = False
        for m in mutations:
            if m in polling_urls:
                skip_body = True  # skip this URL and its following body lines
                continue
            if skip_body and m.startswith("  "):
                continue  # orphan body line from a removed polling URL
            skip_body = False
            filtered.append(m)
        mutations = filtered
        logger.info(f"[db] filtered {len(polling_urls)} high-frequency polling URLs from network log")

    if not mutations:
        return ""

    result_str = "\n".join(mutations)
    # Truncate from HEAD to keep tail (final submit is usually most important)
    if len(result_str) > 3000:
        result_str = "... (earlier mutations truncated)\n" + result_str[-3000:]

    return result_str




def _parse_describe_result(result_str: str) -> str:
    """Parse describe_table result into a clean column summary string.

    The MCP tool returns a JSON-encoded list of dicts like:
      [{"Field": "id", "Type": "int(11)", "Key": "PRI", "Extra": "auto_increment"}, ...]

    This may be wrapped in a {'type': 'text', 'text': '...'} envelope.
    Returns a single line: "id int(11) PRI auto_increment, order_id int(11), ..."
    """
    text = result_str.strip()

    # Unwrap {'type': 'text', 'text': '...'} envelope if present
    if text.startswith("{") and '"type"' in text and '"text"' in text:
        try:
            envelope = json.loads(text)
            if isinstance(envelope, dict) and "text" in envelope:
                text = envelope["text"]
        except Exception:
            pass

    # Parse the column array
    try:
        columns = json.loads(text)
    except Exception:
        # Return truncated raw string as fallback
        return result_str[:500]

    if not isinstance(columns, list):
        return result_str[:500]

    parts: list[str] = []
    for col in columns:
        if not isinstance(col, dict):
            continue
        field = col.get("Field", "")
        col_type = col.get("Type", "")
        key = col.get("Key", "")
        extra = col.get("Extra", "")
        tokens = [f"{field} {col_type}"]
        if key:
            tokens.append(key)
        if extra:
            tokens.append(extra)
        parts.append(" ".join(t for t in tokens if t))

    return ", ".join(parts) if parts else result_str[:500]


class _DBHooks(RunHooks):
    """Lightweight hooks for the DB agent — logs to terminal, writes trace, and updates schema cache."""

    def __init__(self, trace_recorder: ExecutionTraceRecorder | None):
        self.trace_recorder = trace_recorder
        self._pending_args: dict[str, str] = {}

    async def on_tool_start(
        self, context: RunContextWrapper, agent: Agent, tool: Tool
    ) -> None:
        tool_name = getattr(tool, "name", str(tool))
        tool_args = getattr(context, "tool_arguments", "") or ""
        self._pending_args[tool_name] = tool_args
        logger.info(f"[db][action] {tool_name}({tool_args[:160]})")
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_start",
                f"[DB] Tool Start: {tool_name}",
                f"Args: {tool_args[:1000] or '(none)'}",
            )

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        tool_name = getattr(tool, "name", str(tool))
        # MCP tool results may be structured content ([{"type": "text", "text": "..."}]).
        # Extract the text properly instead of using str() which produces Python repr.
        result_str = _serialize_tool_result(result)
        preview = result_str[:200].replace("\n", " ")
        logger.info(f"[db][result] {tool_name} -> {preview}")
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_end",
                f"[DB] Tool End: {tool_name}",
                f"Result:\n{result_str[:2000] or '(empty)'}",
            )

        # Cache describe_table results
        if tool_name == "describe_table" and result_str and "Error" not in result_str:
            args_str = self._pending_args.get(tool_name, "")
            try:
                args = json.loads(args_str) if args_str else {}
                db = args.get("database", "")
                table = args.get("table", "")
                if db and table:
                    cache = _load_schema_cache()
                    cache[f"{db}.{table}"] = _parse_describe_result(result_str)
                    _save_schema_cache(cache)
                    logger.info(f"[db] cached schema for {db}.{table}")
            except Exception as e:
                logger.debug(f"[db] could not cache schema: {e}")


async def _describe_db_checks_tables() -> str:
    """Extract table names from db_checks, call describe_table via MCP for each.

    Returns a schema section with fresh column definitions for tables
    explicitly mentioned in db_checks. Bypasses cache — always live.
    """
    if not _db_checks or not _db_mcp_servers:
        return ""

    cache = _load_schema_cache()
    table_to_db, _ = _build_schema_index(cache)
    table_names = _extract_db_check_tables(_db_checks, cache)
    if not table_names or not table_to_db:
        return ""

    server = _db_mcp_servers[0]
    lines = ["## Live Schema (from db_checks — fresh describe_table, not cache)"]
    described = 0

    for table in table_names:
        database = table_to_db.get(table)
        if not database:
            continue
        try:
            result = await server.call_tool("describe_table", {"database": database, "table": table})
            raw = _serialize_tool_result(getattr(result, "content", result))
            if raw and "Error" not in raw:
                parsed = _parse_describe_result(raw)
                lines.append(f"\n### {database}.{table}")
                lines.append(parsed)
                described += 1
                logger.info(f"[db] live describe_table: {database}.{table}")
            else:
                logger.warning(f"[db] describe_table failed for {table}: {raw[:100]}")
        except Exception as e:
            logger.warning(f"[db] describe_table error for {table}: {e}")

    if described == 0:
        return ""

    result_str = "\n".join(lines)
    logger.info(f"[db] described {described}/{len(table_names)} tables from db_checks")
    return result_str


@function_tool
async def verify_in_db(data_json: str) -> str:
    """Verify business data in the database using key values extracted from the UI.

    Call this after completing UI steps that create or modify data, or at the end
    of the scenario as a final verification pass.

    Args:
        data_json: JSON object with key business values to verify, e.g.
            '{"order_id": "1234", "total": "268.45", "user_email": "test@example.com"}'

    Returns:
        JSON array of DataVerification results (check_name, query, expected,
        actual, status, severity). Include these in data_verifications when
        calling submit_report.
    """
    from universal_debug_agent.agents.db_agent import create_db_agent

    if not _db_mcp_servers:
        return json.dumps([{
            "check_name": "DB verification",
            "query": "",
            "expected": "",
            "actual": "DB tool not configured — no database MCP server available",
            "status": "blocked",
            "severity": "high",
        }])

    # No db_checks → skip DB verification entirely
    if not _db_checks:
        logger.info("[db] no db_checks configured, skipping DB verification")
        return json.dumps([])

    network_log = await _fetch_network_log()
    live_schema = await _describe_db_checks_tables()

    db_agent = create_db_agent(
        mcp_servers=_db_mcp_servers,
        model=_model,
        db_checks=_db_checks,
        live_schema=live_schema,
        network_log=network_log,
        code_root_dir=_code_root_dir,
    )
    logger.info(f"[db] starting DB agent with data={data_json[:200]}")
    if network_log:
        logger.info(f"[db] injecting network log ({len(network_log)} chars)")
    if live_schema:
        logger.info(f"[db] injecting live schema for db_checks tables ({len(live_schema)} chars)")
    logger.info(f"[db] injecting {len(_db_checks)} verification checks from scenario")

    # Record everything passed to DB agent in the trace for debugging
    if _trace_recorder is not None:
        _trace_recorder.record(
            "db_handoff",
            "DB Agent Handoff",
            f"## UI Data\n{data_json}\n\n"
            f"## Network Log\n{network_log or '(none)'}\n\n"
            f"## Live Schema\n{live_schema or '(none)'}",
        )

    try:
        result = await Runner.run(
            db_agent,
            data_json,
            max_turns=DB_MAX_TURNS,
            hooks=_DBHooks(_trace_recorder),
            run_config=RunConfig(),
        )
        if _usage_tracker is not None:
            _usage_tracker.record_run_result(result, phase="db_verify")
        output = result.final_output
        # Normalize to list[dict] regardless of output type
        if isinstance(output, DBVerificationOutput):
            verifications = [v.model_dump() for v in output.verifications]
        elif isinstance(output, str):
            try:
                parsed = json.loads(output)
                verifications = parsed if isinstance(parsed, list) else None
            except Exception:
                verifications = None
            if verifications is None:
                return output
        else:
            return json.dumps(output)
        await _save_db_verify_memory(verifications)
        return json.dumps(verifications, default=str)
    except Exception as e:
        logger.error(f"[db] DB agent error: {e}")
        return json.dumps([{
            "check_name": "DB verification",
            "query": "",
            "expected": "",
            "actual": f"DB agent error: {e}",
            "status": "blocked",
            "severity": "high",
        }])
