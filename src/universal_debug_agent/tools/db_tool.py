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
_evidence_collector: Any = None  # EvidenceCollector instance
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
    global _db_mcp_servers, _model, _trace_recorder, _cache_path, _playwright_server, _allowed_domains, _evidence_collector, _code_root_dir, _usage_tracker, _db_checks, _scenario_name
    _db_mcp_servers = db_mcp_servers
    _model = model
    _trace_recorder = trace_recorder
    _cache_path = cache_path
    _playwright_server = playwright_server
    _allowed_domains = allowed_domains or []
    _evidence_collector = evidence_collector
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


def _parse_memory_frontmatter(path: Path) -> dict | None:
    """Read frontmatter from a .md memory file. Returns parsed dict or None."""
    try:
        text = path.read_text()
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    try:
        import yaml
        fm = yaml.safe_load(text[3:end])
        if isinstance(fm, dict):
            fm["_path"] = path
            return fm
    except Exception:
        pass
    return None


def _scan_memory_manifest() -> list[dict]:
    """Scan memory files, read only frontmatter, return manifest sorted by updated desc.

    Returns list of dicts with keys: _path, type, description, tables, updated, checks.
    Cap at 200 files, newest first.
    """
    mem_dir = _db_verify_memory_dir()
    if mem_dir is None or not mem_dir.exists():
        return []

    memories: list[dict] = []
    for path in mem_dir.glob("*.md"):
        fm = _parse_memory_frontmatter(path)
        if fm:
            memories.append(fm)

    memories.sort(key=lambda m: m.get("updated", ""), reverse=True)
    return memories[:200]


def _build_manifest_text(manifest: list[dict]) -> str:
    """Build a lightweight text representation of the manifest for LLM consumption."""
    lines = []
    for i, m in enumerate(manifest):
        desc = m.get("description", "no description")
        scenario = m.get("scenario", "unknown")
        raw_tables = m.get("tables", [])
        tables = ", ".join(raw_tables) if isinstance(raw_tables, list) else str(raw_tables)
        updated = m.get("updated", "?")
        checks = m.get("checks", 0)
        lines.append(
            f"[{i}] scenario: {scenario} | {desc} | tables: {tables} | updated: {updated} | checks: {checks}"
        )
    return "\n".join(lines)


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


def _prefer_same_scenario_memories(manifest: list[dict], scenario_name: str | None) -> list[dict]:
    """Prefer memories from the same scenario to avoid cross-scenario drift."""
    if not manifest or not scenario_name:
        return manifest

    target = scenario_name.lower()
    exact = [m for m in manifest if str(m.get("scenario", "")).lower() == target]
    return exact or manifest


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


async def _llm_select_memories(manifest: list[dict], context: str, top_n: int = 5) -> list[int]:
    """Use LLM to pick most relevant memory indices from a manifest.

    Returns list of selected indices.
    """
    if not manifest or _model is None:
        return []

    client, model_name = _get_model_client()
    if not client or not model_name:
        return []

    manifest_text = _build_manifest_text(manifest)

    prompt = (
        "You are selecting relevant DB verification memories for a test scenario.\n\n"
        f"## Current context\n{context[:2000]}\n\n"
        f"## Available memories\n{manifest_text}\n\n"
        f"Pick up to {top_n} memories most relevant to the current context. "
        "Return ONLY the index numbers, comma-separated. Example: 0,2,4\n"
        "If none are relevant, return: none"
    )

    try:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=50,
            temperature=0,
        )
        answer = response.choices[0].message.content.strip()
        logger.info(f"[db] memory LLM selection (top {top_n}): {answer}")

        if answer.lower() == "none":
            return []

        indices = []
        for part in answer.replace(" ", "").split(","):
            try:
                idx = int(part)
                if 0 <= idx < len(manifest):
                    indices.append(idx)
            except ValueError:
                continue
        return indices[:top_n]
    except Exception as e:
        logger.warning(f"[db] memory LLM selection failed: {e}")
        return []


def _load_memory_content(paths: list[Path], max_total_chars: int = 6000) -> tuple[list[str], str]:
    """Load selected memory files. Returns (table_names, full_content_for_prompt).

    table_names: used to prioritize schema hint filtering.
    full_content: injected into DB agent prompt as verification knowledge.
    """
    tables: set[str] = set()
    content_parts: list[str] = []
    total_chars = 0

    for path in paths:
        fm = _parse_memory_frontmatter(path)
        if fm and isinstance(fm.get("tables"), list):
            tables.update(t.lower() for t in fm["tables"] if isinstance(t, str))

        # Load full file content for prompt injection
        try:
            text = path.read_text()
            # Strip frontmatter, keep body
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3:].strip()
            if text and total_chars + len(text) <= max_total_chars:
                content_parts.append(f"### From: {path.stem}\n\n{text}")
                total_chars += len(text)
        except Exception:
            pass

    content = ""
    if content_parts:
        content = "## DB Verification Memory (from previous successful runs)\n\n" + "\n\n---\n\n".join(content_parts)

    logger.info(f"[db] loaded {len(tables)} tables, {len(content_parts)} memory docs ({total_chars} chars)")
    return sorted(tables), content


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
            {"requestBody": True, "requestHeaders": False, "static": False},
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
            mutations.append(line_stripped)
            last_was_mutation = True
        # Keep "Request body:" lines that follow a kept mutation request
        elif line_stripped.startswith("Request body:") and last_was_mutation:
            mutations.append("  " + line_stripped)
        else:
            last_was_mutation = False

    if not mutations:
        return ""

    result_str = "\n".join(mutations)
    if len(result_str) > 3000:
        result_str = result_str[:3000] + "\n... (truncated)"

    return result_str


def _build_workflow_summary() -> str:
    """Build a compact summary of what the UI agent did, for the DB agent's context."""
    if _evidence_collector is None or not _evidence_collector.items:
        return ""

    lines: list[str] = []
    for item in _evidence_collector.items:
        tool = item["tool"]
        args = item.get("args", "")
        result = item.get("result_preview", "")

        if tool == "browser_navigate":
            # Extract URL
            try:
                url = json.loads(args).get("url", args)
            except Exception:
                url = args
            lines.append(f"Navigate: {url}")

        elif tool == "browser_click":
            # Extract element description
            try:
                parsed = json.loads(args)
                element = parsed.get("element", parsed.get("ref", args))
            except Exception:
                element = args[:80]
            lines.append(f"Click: {element}")

        elif tool in ("browser_type", "browser_fill_form"):
            # Extract what was filled (redact passwords)
            try:
                parsed = json.loads(args)
                if "fields" in parsed:
                    fields = [f.get("name", "?") for f in parsed["fields"]]
                    lines.append(f"Fill form: {', '.join(fields)}")
                else:
                    ref = parsed.get("ref", "?")
                    text = parsed.get("text", "")
                    if "password" in args.lower():
                        text = "***"
                    lines.append(f"Type: ref={ref} text={text[:50]}")
            except Exception:
                lines.append(f"Type: {args[:60]}")

        elif tool == "browser_select_option":
            lines.append(f"Select: {args[:80]}")

        elif tool == "browser_snapshot":
            # Extract page URL from result
            url_match = re.search(r"Page URL:\s*(\S+)", result)
            if url_match:
                lines.append(f"Page: {url_match.group(1)}")

        elif tool == "browser_take_screenshot":
            lines.append("Screenshot taken")

        elif tool == "get_test_account":
            lines.append(f"Get test account: {args}")

    if not lines:
        return ""

    # Cap at ~30 lines to keep it compact
    if len(lines) > 30:
        lines = lines[:15] + [f"... ({len(lines) - 30} steps omitted) ..."] + lines[-15:]

    return "\n".join(lines)


def _filter_schema_cache(data_json: str, workflow_summary: str, network_log: str, remembered_tables: list[str] | None = None, db_checks: list[str] | None = None) -> str:
    """Filter schema cache to tables matching keywords from the context.

    Extracts keywords from UI data, workflow summary, and network log,
    then returns only matching table schemas. This gives the DB agent
    enough schema context to write SQL without injecting all 510 tables.
    """
    cache = _load_schema_cache()
    if not cache:
        return ""

    # Build keyword set from runtime evidence only. db_checks are handled
    # separately as explicit table names so natural-language phrasing does not
    # fan out into unrelated schema matches.
    context = f"{data_json} {workflow_summary} {network_log}".lower()

    # Extract meaningful words (skip very short/common ones)
    words = set(re.findall(r"[a-z_]{3,}", context))
    # Add compound terms that might match table names
    keywords = set()
    for w in words:
        keywords.add(w)
        # Split on underscore to catch partial matches: "newsletter_type" → "newsletter", "type"
        for part in w.split("_"):
            if len(part) >= 3:
                keywords.add(part)

    # Always include common business tables
    keywords.update({"order", "orders", "customer", "customers", "payment", "product"})

    explicit_tables = set(_extract_db_check_tables(db_checks, cache))

    # Match tables whose name contains any keyword, always keeping explicit
    # tables named in db_checks even when the rest of the context is sparse.
    matched: dict[str, str] = {}
    for db_table, schema in cache.items():
        table_name = db_table.split(".")[-1].lower()
        if table_name in explicit_tables or any(kw in table_name for kw in keywords):
            matched[db_table] = schema

    if not matched:
        return ""

    # Cap at ~20 tables to keep token budget reasonable
    if len(matched) > 20:
        remembered = set(remembered_tables or [])
        scored = sorted(
            matched.items(),
            key=lambda kv: (
                0 if kv[0].split(".")[-1].lower() in explicit_tables else 1,
                0 if kv[0].split(".")[-1].lower() in remembered else 1,
                len(kv[0].split(".")[-1]),
            ),
        )
        matched = dict(scored[:20])

    lines = ["## Relevant DB Schema (from cache — skip describe_table for these)"]
    for db_table, columns in sorted(matched.items()):
        lines.append(f"\n### {db_table}")
        lines.append(columns)

    result = "\n".join(lines)
    logger.info(
        "[db] schema hint: %s tables matched from %s cached (explicit db_checks tables: %s)",
        len(matched),
        len(cache),
        sorted(explicit_tables),
    )
    return result


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

    network_log = await _fetch_network_log()
    workflow_summary = _build_workflow_summary()

    # Memory: scan manifests → LLM picks top 5 → load tables + content
    # picked_paths reused at save time as merge target (no second LLM call)
    remembered_tables: list[str] = []
    memory_content: str = ""
    picked_paths: list[Path] = []
    manifest = _scan_memory_manifest()
    if manifest:
        manifest = _prefer_same_scenario_memories(manifest, _scenario_name)
        context = f"UI Data: {data_json}\nWorkflow: {workflow_summary[:500]}\nNetwork: {network_log[:500]}"
        if _db_checks:
            context += f"\nDB Checks: {', '.join(_db_checks)}"
        selected = await _llm_select_memories(manifest, context, top_n=5)
        if selected:
            picked_paths = [manifest[i]["_path"] for i in selected]
            remembered_tables, memory_content = _load_memory_content(picked_paths)

    schema_hint = _filter_schema_cache(data_json, workflow_summary, network_log, remembered_tables, _db_checks)

    # For tables explicitly mentioned in db_checks, get fresh schema via describe_table (bypasses cache)
    live_schema = await _describe_db_checks_tables()

    db_agent = create_db_agent(
        mcp_servers=_db_mcp_servers,
        model=_model,
        network_log=network_log,
        workflow_summary=workflow_summary,
        code_root_dir=_code_root_dir,
        schema_hint=schema_hint,
        live_schema=live_schema,
        db_checks=_db_checks,
        verify_memory=memory_content,
    )
    logger.info(f"[db] starting DB agent with data={data_json[:200]}")
    if network_log:
        logger.info(f"[db] injecting network log ({len(network_log)} chars)")
    if workflow_summary:
        logger.info(f"[db] injecting workflow summary ({len(workflow_summary)} chars)")
    if schema_hint:
        logger.info(f"[db] injecting schema hint ({len(schema_hint)} chars)")
    if live_schema:
        logger.info(f"[db] injecting live schema for db_checks tables ({len(live_schema)} chars)")
    if _db_checks:
        logger.info(f"[db] injecting {len(_db_checks)} verification hints from scenario")

    # Record everything passed to DB agent in the trace for debugging
    if _trace_recorder is not None:
        _trace_recorder.record(
            "db_handoff",
            "DB Agent Handoff",
            f"## UI Data\n{data_json}\n\n"
            f"## Workflow Summary\n{workflow_summary or '(none)'}\n\n"
            f"## Network Log\n{network_log or '(none)'}\n\n"
            f"## Live Schema\n{live_schema or '(none)'}\n\n"
            f"## Schema Hint\n{schema_hint or '(none)'}\n\n"
            f"## Memory\n{memory_content or '(none)'}",
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
