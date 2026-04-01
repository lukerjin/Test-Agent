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


def configure(
    db_mcp_servers: list[MCPServerStdio],
    model: Any,
    trace_recorder: ExecutionTraceRecorder | None = None,
    cache_path: Path | None = None,
    playwright_server: MCPServerStdio | None = None,
    allowed_domains: list[str] | None = None,
    evidence_collector: Any = None,
    code_root_dir: str = "",
) -> None:
    """Configure the DB tool. Call this before running the UI agent."""
    global _db_mcp_servers, _model, _trace_recorder, _cache_path, _playwright_server, _allowed_domains, _evidence_collector, _code_root_dir
    _db_mcp_servers = db_mcp_servers
    _model = model
    _trace_recorder = trace_recorder
    _cache_path = cache_path
    _playwright_server = playwright_server
    _allowed_domains = allowed_domains or []
    _evidence_collector = evidence_collector
    _code_root_dir = code_root_dir


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
    db_agent = create_db_agent(
        mcp_servers=_db_mcp_servers,
        model=_model,
        network_log=network_log,
        workflow_summary=workflow_summary,
        code_root_dir=_code_root_dir,
    )
    logger.info(f"[db] starting DB agent with data={data_json[:200]}")
    if network_log:
        logger.info(f"[db] injecting network log ({len(network_log)} chars)")
    if workflow_summary:
        logger.info(f"[db] injecting workflow summary ({len(workflow_summary)} chars)")

    # Record everything passed to DB agent in the trace for debugging
    if _trace_recorder is not None:
        _trace_recorder.record(
            "db_handoff",
            "DB Agent Handoff",
            f"## UI Data\n{data_json}\n\n"
            f"## Workflow Summary\n{workflow_summary or '(none)'}\n\n"
            f"## Network Log\n{network_log or '(none)'}",
        )

    try:
        result = await Runner.run(
            db_agent,
            data_json,
            max_turns=DB_MAX_TURNS,
            hooks=_DBHooks(_trace_recorder),
            run_config=RunConfig(),
        )
        output = result.final_output
        if isinstance(output, DBVerificationOutput):
            return json.dumps([v.model_dump() for v in output.verifications], default=str)
        if isinstance(output, str):
            return output
        return json.dumps(output)
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
