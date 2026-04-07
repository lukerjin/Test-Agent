"""RunHooks implementation — monitors tool calls and detects stuck state."""

from __future__ import annotations

import json
import hashlib
import logging
import re
from typing import Any

from agents import Agent, RunContextWrapper, RunHooks, Tool
from agents.exceptions import UserError
from universal_debug_agent.observability.trace_recorder import ExecutionTraceRecorder
from universal_debug_agent.tools import db_tool

logger = logging.getLogger(__name__)


def _compact_jsonish(value: str, limit: int = 180) -> str:
    compact = " ".join(value.strip().split())
    return compact[: limit - 3] + "..." if len(compact) > limit else compact


def _summarize_llm_response(response) -> str:
    parts: list[str] = []
    for item in getattr(response, "output", []):
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            name = getattr(item, "name", "?")
            args = getattr(item, "arguments", "") or ""
            parts.append(f"{name}({_compact_jsonish(args, 120)})")
        else:
            text = getattr(item, "text", None)
            if not text:
                try:
                    from agents.items import ItemHelpers

                    text = ItemHelpers.extract_text(item)
                except Exception:
                    text = None
            if text:
                parts.append(f"text:{_compact_jsonish(text, 120)}")
    if not parts:
        return "(no visible output items)"
    summary = ", ".join(parts[:3])
    if len(parts) > 3:
        summary += f", ... (+{len(parts) - 3} more)"
    return summary


def _summarize_tool_result(tool_name: str, result_str: str) -> str:
    if not result_str:
        return "(empty)"

    # Strip base64 image data early — never log it
    result_str_clean = re.sub(
        r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "<image>", result_str
    )

    page_url = re.search(r"- Page URL:\s*(.+)", result_str_clean)
    if page_url:
        summary = f"page={page_url.group(1).strip()}"
        console_info = re.search(r"- Console:\s*(.+)", result_str_clean)
        if console_info:
            summary += f"; console={console_info.group(1).strip()}"
        return summary

    # browser_take_screenshot result starts with page info without "- Page URL:"
    page_match = re.search(r"page=(\S+)", result_str_clean)
    if page_match:
        summary = f"page={page_match.group(1)}"
        console_info = re.search(r"- Console:\s*(.+)", result_str_clean)
        if console_info:
            summary += f"; console={console_info.group(1).strip()}"
        return summary

    screenshot = re.search(r"\[Screenshot of viewport\]\((.+?)\)", result_str_clean)
    if screenshot:
        return f"screenshot={screenshot.group(1)}"

    rows = re.search(r"(\d+\s+rows?\s+returned)", result_str_clean, re.IGNORECASE)
    if rows:
        return rows.group(1)

    aliases = re.search(r'"aliases"\s*:\s*{', result_str_clean)
    if aliases:
        return "database aliases loaded"

    first_line = result_str_clean.strip().splitlines()[0]
    return _compact_jsonish(first_line, 180)


class SwitchToAnalysisMode(Exception):
    """Raised by hooks when the agent is detected as stuck."""

    def __init__(self, evidence_summary: str, reason: str):
        self.evidence_summary = evidence_summary
        self.reason = reason
        super().__init__(reason)


class InvestigationHooks(RunHooks):
    """Hooks that track tool calls and trigger mode switch when stuck."""

    # Tools that change page state — refs from their snapshot are stale after page reload
    _PAGE_CHANGING_TOOLS = {"browser_click", "browser_navigate"}

    def __init__(
        self,
        stuck_detector: Any,
        evidence_collector: Any,
        trace_recorder: ExecutionTraceRecorder | None = None,
        playwright_server: Any = None,
    ):
        self.stuck_detector = stuck_detector
        self.evidence_collector = evidence_collector
        self.trace_recorder = trace_recorder
        self._playwright_server = playwright_server
        # Buffer function_call args from LLM responses for use in on_tool_start.
        # context.tool_arguments is unreliable for MCP tools; the LLM response is authoritative.
        self._pending_tool_args: dict[str, list[str]] = {}
        # Auto-snapshot captured after click/navigate — consumed by input_filters
        self.pending_auto_snapshot: str | None = None

    async def on_llm_end(
        self, context: RunContextWrapper, agent: Agent, response
    ) -> None:
        if self.trace_recorder is not None:
            self.trace_recorder.record_llm_response(response)
        logger.info(f"[LLM]    {_summarize_llm_response(response)}")
        for item in response.output:
            if getattr(item, "type", None) == "function_call":
                name = getattr(item, "name", None)
                args = getattr(item, "arguments", None)
                if name:
                    self._pending_tool_args.setdefault(name, []).append(args or "")

    async def on_tool_start(
        self, context: RunContextWrapper, agent: Agent, tool: Tool
    ) -> None:
        tool_name = getattr(tool, "name", str(tool))

        self._apply_playwright_defaults(context, tool_name)
        self._validate_playwright_click_args(context, tool_name)
        await self._capture_form_data_before_click(context, tool_name)

        # Prefer buffered args from LLM response (reliable for MCP tools)
        buffered = self._pending_tool_args.get(tool_name)
        if buffered:
            tool_args = buffered.pop(0)
            if not buffered:
                del self._pending_tool_args[tool_name]
        else:
            tool_args = self._tool_args(context, tool)

        self.stuck_detector.record(tool_name, tool_args)
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_start",
                f"Tool Start: {tool_name}",
                f"Args: {tool_args[:1000] or '(none)'}",
            )
        logger.info(f"[action] {tool_name}({_compact_jsonish(tool_args, 160)})")

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        result_str = str(result) if result else ""
        result_hash = hashlib.md5(result_str.encode()).hexdigest()

        tool_name = getattr(tool, "name", str(tool))
        tool_args = self._tool_args(context, tool)

        self.stuck_detector.update_last_result(result_hash)
        self.evidence_collector.collect(tool_name, tool_args, result_str)
        if self.trace_recorder is not None:
            self.trace_recorder.record(
                "tool_end",
                f"Tool End: {tool_name}",
                f"Args: {tool_args[:1000] or '(none)'}\n\nResult:\n{result_str[:2000] or '(empty)'}",
            )

        logger.info(f"[result]  {tool_name} -> {_summarize_tool_result(tool_name, result_str)}")

        # Auto-snapshot after page-changing actions to ensure fresh refs
        if tool_name in self._PAGE_CHANGING_TOOLS and self._playwright_server is not None:
            await self._auto_snapshot_after_action(tool_name)

        if self.stuck_detector.is_stuck():
            reason = self.stuck_detector.stuck_reason()
            logger.warning(f"[stuck]  {reason}")
            if self.trace_recorder is not None:
                self.trace_recorder.record(
                    "mode_switch",
                    "Switch To Analysis",
                    reason,
                )
            raise SwitchToAnalysisMode(
                evidence_summary=self.evidence_collector.build_summary(),
                reason=reason,
            )

    async def _auto_snapshot_after_action(self, tool_name: str) -> None:
        """Auto-capture a fresh snapshot after click/navigate.

        Page-changing actions (click, navigate) may trigger full page reloads
        (e.g. traditional form POST). The snapshot in the click result is captured
        BEFORE the reload, so its refs are stale. We capture a fresh snapshot here
        and store it so the input_filter can inject it into the LLM's next input,
        giving the model correct refs without requiring it to manually call
        browser_snapshot.
        """
        try:
            result = await self._playwright_server.call_tool(
                "browser_snapshot",
                {"depth": 10},
            )
            # Extract text content from MCP result
            content = getattr(result, "content", result)
            if isinstance(content, list):
                for item in content:
                    text = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else None)
                    if text and "```yaml" in text:
                        self.pending_auto_snapshot = text
                        logger.info(f"[auto-snapshot] captured fresh snapshot after {tool_name}")
                        return
            logger.warning(f"[auto-snapshot] no yaml content in snapshot result after {tool_name}")
        except Exception as e:
            logger.warning(f"[auto-snapshot] failed after {tool_name}: {e}")

    # JS function to extract form data before submit.
    # Returns null if element is not inside a <form> or form is GET.
    _FORM_CAPTURE_JS = """(element) => {
  const form = element.closest('form');
  if (!form) return null;
  const method = (form.method || 'GET').toUpperCase();
  if (method !== 'POST' && method !== 'PUT' && method !== 'PATCH' && method !== 'DELETE') return null;
  const action = form.action || window.location.href;
  const data = {};
  const formData = new FormData(form);
  for (const [key, value] of formData.entries()) {
    if (value instanceof File) {
      data[key] = '[File: ' + value.name + ', ' + value.size + ' bytes]';
    } else if (data.hasOwnProperty(key)) {
      if (Array.isArray(data[key])) data[key].push(value);
      else data[key] = [data[key], value];
    } else {
      data[key] = value;
    }
  }
  return { action: action, method: method, fields: data };
}"""

    async def _capture_form_data_before_click(
        self, context: RunContextWrapper, tool_name: str
    ) -> None:
        """Capture form field values before browser_click submits a traditional form.

        Traditional <form> POSTs cause page navigation and are invisible to
        browser_network_requests. We use browser_evaluate to extract FormData
        before the click, then pass it to db_tool for network log merging.
        """
        if tool_name != "browser_click" or self._playwright_server is None:
            return

        args = self._parse_tool_args(context)
        if args is None:
            return
        ref = args.get("ref")
        if not ref:
            return

        try:
            result = await self._playwright_server.call_tool(
                "browser_evaluate",
                {"function": self._FORM_CAPTURE_JS, "ref": ref},
            )
            content = getattr(result, "content", result)
            text = None
            if isinstance(content, list):
                for item in content:
                    text = getattr(item, "text", None) or (
                        item.get("text") if isinstance(item, dict) else None
                    )
                    if text:
                        break
            elif isinstance(content, str):
                text = content

            if not text or text.strip() == "null" or text.strip() == "undefined":
                return

            # browser_evaluate returns: "### Result\n{json}\n### Ran Playwright code\n```js...```"
            # Extract the JSON between "### Result" and the next "###" or code block
            if "### Result" in text:
                start = text.index("### Result") + len("### Result")
                # Find the end: next "###" heading or end of string
                end = text.find("###", start)
                if end == -1:
                    end = len(text)
                text = text[start:end].strip()

            data = json.loads(text)
            if not isinstance(data, dict) or "action" not in data:
                return

            db_tool.record_form_capture(data)
            logger.info(
                f"[form-capture] {data.get('method', 'POST')} {data.get('action', '?')} "
                f"({len(data.get('fields', {}))} fields)"
            )
            if self.trace_recorder is not None:
                self.trace_recorder.record(
                    "form_capture",
                    f"Form Capture: {data.get('method')} {data.get('action')}",
                    f"Fields: {json.dumps(data.get('fields', {}), ensure_ascii=False)[:500]}",
                )
        except Exception as e:
            logger.debug(f"[form-capture] failed for ref={ref}: {e}")

    def _tool_args(self, context: RunContextWrapper, tool: Tool) -> str:
        if hasattr(context, "tool_arguments"):
            return getattr(context, "tool_arguments") or "(none)"
        return str(getattr(tool, "args", "")) or "(none)"

    def _parse_tool_args(self, context: RunContextWrapper) -> dict[str, Any] | None:
        if not hasattr(context, "tool_arguments"):
            return None
        try:
            parsed = json.loads(getattr(context, "tool_arguments"))
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _write_tool_args(self, context: RunContextWrapper, args: dict[str, Any]) -> None:
        if not hasattr(context, "tool_arguments"):
            return
        payload = json.dumps(args, ensure_ascii=False)
        context.tool_arguments = payload
        if getattr(context, "tool_call", None) is not None:
            context.tool_call.arguments = payload

    def _apply_playwright_defaults(self, context: RunContextWrapper, tool_name: str) -> None:
        if tool_name == "browser_take_screenshot":
            args = self._parse_tool_args(context)
            if args is not None and not args.get("type"):
                args["type"] = "png"
                self._write_tool_args(context, args)

        elif tool_name == "browser_snapshot":
            # Strip `filename` so Playwright MCP returns inline ARIA content instead
            # of a file reference. Without inline content the LLM cannot see element
            # refs and gets stuck calling snapshot in a loop.
            # Enforce minimum depth=10: checkout forms nest textboxes and buttons at
            # depth 9-10 in the ARIA tree. depth<10 shows the form container and
            # labels but collapses the actual input fields, causing the model to
            # click buttons without filling required fields.
            args = self._parse_tool_args(context)
            if args is not None:
                changed = False
                if "filename" in args:
                    args.pop("filename")
                    changed = True
                if (args.get("depth") or 0) < 10:
                    args["depth"] = 10
                    changed = True
                if changed:
                    self._write_tool_args(context, args)

    def _validate_playwright_click_args(self, context: RunContextWrapper, tool_name: str) -> None:
        if tool_name != "browser_click":
            return
        args = self._parse_tool_args(context)
        if args is None:
            return

        # Validate that `ref` is a real snapshot ref (e.g. "e144"), not a CSS selector.
        # @playwright/mcp browser_click requires an exact ref from the current page snapshot.
        ref = args.get("ref")
        if isinstance(ref, str) and not re.match(r"^e\d+$", ref.strip()):
            raise UserError(
                f"browser_click 'ref' must be a snapshot ref like 'e144', not a selector string. "
                f"Got: {ref!r}. Call browser_snapshot first to get the current page refs, "
                f"then pass the exact ref value (e.g. {{\"ref\": \"e144\"}})."
            )

        candidates = [
            args.get("selector"),
            args.get("locator"),
            args.get("element"),
            args.get("target"),
        ]
        candidate_text = " ".join(str(v).strip() for v in candidates if isinstance(v, str)).lower()
        if not candidate_text:
            return

        ambiguous_patterns = {
            "button",
            "form button",
            "form >> button",
            "locator('form').getbyrole('button')",
            'locator("form").getbyrole("button")',
        }
        if candidate_text in ambiguous_patterns:
            raise UserError(
                "Ambiguous browser_click target blocked. Use a named button, stable selector, "
                "or refreshed page snapshot before clicking."
            )
