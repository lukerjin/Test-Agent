"""Model input filters for controlling tool-output growth across turns."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.extensions.tool_output_trimmer import ToolOutputTrimmer
from agents.run_config import CallModelData, ModelInputData

logger = logging.getLogger(__name__)


def _serialize_output(value: Any) -> str:
    """Extract text from a tool output value.

    The Responses API stores MCP tool outputs as structured content:
      [{"type": "input_text", "text": "..."}]
    Using str() on this escapes newlines in the Python repr, breaking
    regex-based filters. We extract the text field directly instead.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, dict) and "text" in value:
        return value["text"]
    return str(value)


# ── Playwright snapshot filtering ────────────────────────────────────────────

_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "checkbox", "radio",
    "combobox", "searchbox", "heading", "alert", "dialog",
    "listitem", "row", "cell",
    "tab", "tabpanel", "menuitem", "option", "switch",
    "img", "navigation", "status", "banner", "form",
    "spinbutton", "slider", "progressbar",
})

# Match ARIA roles at YAML line boundaries: "- rolename " or "- rolename[" or "- rolename\n"
# Avoids substring false positives (e.g. "row" matching "arrow").
_ROLE_PATTERN = re.compile(
    r"- (?:" + "|".join(re.escape(r) for r in sorted(_INTERACTIVE_ROLES)) + r")(?:\s|$|\[|\")"
)

_KEEP_MARKERS = ("[active]", "[checked]", "[cursor=pointer]", "/url:", "text:")


def _extract_interactive_snapshot(text: str, max_lines: int | None = 200) -> str:
    """Return the Page section + an interactive-elements-only ARIA tree.

    Returns an empty string when no ``### Snapshot`` block is found so the
    caller can fall back to plain character-count truncation.

    ``max_lines=None`` keeps all filtered lines (used for the current turn).
    """
    # 1. Keep the ### Page section verbatim (URL / title / console counts).
    page_lines: list[str] = []
    in_page = False
    for line in text.splitlines():
        if line.startswith("### Page"):
            in_page = True
        elif line.startswith("###") and in_page:
            in_page = False
        if in_page:
            page_lines.append(line)

    # 2. Locate the YAML snapshot block.
    snap_match = re.search(r"### Snapshot\n```yaml\n(.*?)```", text, re.DOTALL)
    if not snap_match:
        return ""

    yaml_lines = snap_match.group(1).splitlines()
    kept_indices: set[int] = set()

    for idx, line in enumerate(yaml_lines):
        stripped = line.strip()

        # Drop [unchanged] back-references — zero information density.
        if "[unchanged]" in stripped:
            continue

        should_keep = False

        # Keep any line that names an interactive ARIA role (word-boundary match).
        if _ROLE_PATTERN.search(stripped):
            should_keep = True

        # Keep lines with action/state markers.
        elif any(marker in stripped for marker in _KEEP_MARKERS):
            should_keep = True

        # Keep inline-text nodes: "- generic [ref=eN]: visible text"
        # These carry section labels, prices, error messages, etc.
        elif re.search(r"\[ref=e\d+\]:\s+\S", stripped):
            should_keep = True

        if should_keep:
            kept_indices.add(idx)
            # Walk up indentation to preserve parent context (form structure, labels).
            indent = len(line) - len(line.lstrip())
            for back_idx in range(idx - 1, -1, -1):
                parent_line = yaml_lines[back_idx]
                parent_indent = len(parent_line) - len(parent_line.lstrip())
                if parent_indent < indent:
                    kept_indices.add(back_idx)
                    indent = parent_indent
                    if parent_indent == 0:
                        break

    if not kept_indices:
        return ""

    kept = [yaml_lines[i] for i in sorted(kept_indices)]
    displayed = kept if max_lines is None else kept[:max_lines]
    parts = [
        *page_lines,
        f"### Snapshot (interactive only — {len(yaml_lines)} → {len(displayed)} lines)",
        "```yaml",
        *displayed,
        "```",
    ]
    return "\n".join(parts)


@dataclass
class MCPToolOutputFilter:
    """Trim bulky MCP tool outputs before they are sent back to the model."""

    recent_turns: int = 1
    default_max_chars: int = 4_000
    default_preview_chars: int = 800
    aggressive_max_chars: int = 1_500
    aggressive_preview_chars: int = 300
    aggressive_tool_markers: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "playwright",
            "browser_",
            "navigate",
            "snapshot",
            "database",
            "sql",
            "query",
        })
    )
    snapshot_filter: bool = True  # set False to revert to char-truncation only
    snapshot_dir: Path | None = None  # playwright output dir for resolving file refs
    hooks: Any = None  # InvestigationHooks ref — for consuming auto-snapshot

    def __post_init__(self) -> None:
        self._base_trimmer = ToolOutputTrimmer(
            recent_turns=self.recent_turns,
            max_output_chars=self.default_max_chars,
            preview_chars=self.default_preview_chars,
        )

    @staticmethod
    def _strip_images(item: dict[str, Any]) -> dict[str, Any]:
        """Remove input_image content blocks from a function_call_output item."""
        output = item.get("output")
        if not isinstance(output, list):
            return item
        filtered = [
            block for block in output
            if not (isinstance(block, dict) and block.get("type") == "input_image")
        ]
        if len(filtered) == len(output):
            return item
        trimmed_item = dict(item)
        trimmed_item["output"] = filtered
        return trimmed_item

    def __call__(self, data: CallModelData[Any]) -> ModelInputData:
        trimmed = self._base_trimmer(data)
        boundary = self._find_recent_boundary(trimmed.input)
        call_id_to_names = self._build_call_id_to_names(trimmed.input)
        new_items: list[Any] = []

        for idx, item in enumerate(trimmed.input):
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                item = self._strip_images(item)
                call_id = str(item.get("call_id") or item.get("id") or "")
                tool_names = call_id_to_names.get(call_id, ())
                if idx < boundary:
                    # Old turns: snapshot filter (preserves semantics) with char-truncation fallback.
                    item = self._trim_output_item(item, tool_names)
                else:
                    # Recent turns: snapshot filter only — no char truncation.
                    item = self._filter_recent_item(item, tool_names)
            new_items.append(item)

        return ModelInputData(input=new_items, instructions=trimmed.instructions)

    def _find_recent_boundary(self, items: list[Any]) -> int:
        """Return the index of the first item in the 'recent' zone.

        The basic rule: the last ``recent_turns`` function_call_output items
        are "recent" (snapshot-filter only); everything before is "old"
        (snapshot stripped + char-truncated).

        **Same-page extension**: if earlier outputs are on the same page URL
        as the most recent snapshot, they stay "recent" too. This prevents
        stripping a snapshot when the agent does same-page actions (fill form,
        type) that don't change the URL — those actions don't produce new
        snapshots, so the earlier snapshot's refs are still valid.
        """
        # Pass 1: find the basic boundary by counting outputs from the end
        basic_boundary = 0
        output_count = 0
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                output_count += 1
                if output_count >= self.recent_turns:
                    basic_boundary = i
                    break

        # Pass 2: find the most recent page URL, scanning from the end across ALL outputs.
        # Some tools (fill_form, type) don't include a Page URL in their output,
        # so we need to look further back to find the URL from the last snapshot/navigate.
        current_url = None
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            output_str = _serialize_output(item.get("output", ""))
            url_match = re.search(r"Page URL:\s*(\S+)", output_str)
            if url_match:
                current_url = url_match.group(1).strip()
                break

        if not current_url:
            return basic_boundary

        # Pass 3: extend boundary backwards to include all outputs on the same URL.
        # Outputs without a URL (fill_form, type) are assumed to be on the same page
        # as the nearest output that does have a URL.
        extended_boundary = basic_boundary
        for i in range(basic_boundary - 1, -1, -1):
            item = items[i]
            if not isinstance(item, dict) or item.get("type") != "function_call_output":
                continue
            output_str = _serialize_output(item.get("output", ""))
            url_match = re.search(r"Page URL:\s*(\S+)", output_str)
            if url_match:
                if url_match.group(1).strip() == current_url:
                    extended_boundary = i
                else:
                    break  # Different page — stop extending
            else:
                # No URL in this output (e.g. fill_form, type) — assume same page
                extended_boundary = i

        return extended_boundary

    def _build_call_id_to_names(self, items: list[Any]) -> dict[str, tuple[str, ...]]:
        mapping: dict[str, tuple[str, ...]] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            name = item.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                mapping[call_id] = (name, name.lower())
        return mapping

    def _resolve_snapshot_refs(self, output_str: str) -> str:
        """Replace [Snapshot](name) file references with inline ARIA tree content.

        playwright MCP saves snapshot files to its cwd and returns a markdown
        link instead of inline content. This method reads those files and injects
        the ARIA tree so _extract_interactive_snapshot can process it normally.
        """
        if self.snapshot_dir is None:
            return output_str

        def _replace(m: re.Match) -> str:
            name = m.group(1)
            # Build candidate paths. Auto-generated snapshots live in .playwright-mcp/
            # subdirectory; named snapshots are in the root of snapshot_dir.
            if "/" in name:
                basename = Path(name).name
                candidates = [
                    name,                                  # exact relative path
                    basename,                              # basename only
                    f".playwright-mcp/{basename}",         # explicit subdir
                ]
            else:
                candidates = [name, f"{name}.md"]

            # Try each candidate in snapshot_dir and its parent (handles cwd off-by-one)
            search_dirs = [self.snapshot_dir]
            if self.snapshot_dir.parent != self.snapshot_dir:
                search_dirs.append(self.snapshot_dir.parent)

            for base_dir in search_dirs:
                for candidate in candidates:
                    path = base_dir / candidate
                    try:
                        content = path.read_text(encoding="utf-8")
                        return f"### Snapshot\n```yaml\n{content}\n```"
                    except OSError:
                        continue

            logger.warning(
                "Failed to resolve snapshot ref %r — tried %s and parent",
                name, self.snapshot_dir,
            )
            return m.group(0)  # leave unchanged if file not found

        # Match optional leading "- " list marker emitted by playwright MCP
        return re.sub(r"-?\s*\[Snapshot\]\(([^)]+)\)", _replace, output_str)

    def _filter_recent_item(self, item: dict[str, Any], tool_names: tuple[str, ...]) -> dict[str, Any]:
        """Apply snapshot filter to the current (recent) turn — no char truncation."""
        if not self.snapshot_filter:
            return item
        output_str = _serialize_output(item.get("output", ""))
        if not output_str:
            return item
        lowered = " ".join(tool_names).lower()
        if not any(m in lowered for m in ("browser_", "playwright", "navigate", "snapshot")):
            return item

        # If hooks captured a fresh auto-snapshot after click/navigate,
        # use it instead of the stale file-ref snapshot in the tool result.
        # This ensures the model sees post-reload refs, not pre-reload ones.
        if (
            self.hooks is not None
            and getattr(self.hooks, "pending_auto_snapshot", None)
            and any(t in lowered for t in ("browser_click", "browser_navigate"))
        ):
            auto_snap = self.hooks.pending_auto_snapshot
            self.hooks.pending_auto_snapshot = None
            # Replace the stale snapshot section with the fresh one
            output_str = re.sub(
                r"### Snapshot\n.*$",
                auto_snap,
                output_str,
                flags=re.DOTALL,
            )
        else:
            output_str = self._resolve_snapshot_refs(output_str)

        filtered = _extract_interactive_snapshot(output_str, max_lines=None)
        if not filtered:
            return item
        trimmed_item = dict(item)
        trimmed_item["output"] = filtered
        return trimmed_item

    @staticmethod
    def _make_page_summary(output_str: str) -> str:
        """Extract a ~30-token mini summary from a resolved snapshot for old turns."""
        url_match = re.search(r"Page URL:\s*(\S+)", output_str)
        url = url_match.group(1).strip() if url_match else "unknown"

        snap_match = re.search(r"### Snapshot\n```yaml\n(.*?)```", output_str, re.DOTALL)
        if not snap_match:
            return f"[page: {url}]"

        yaml_text = snap_match.group(1)
        headings = re.findall(r'- heading\s+"([^"]*)"', yaml_text)
        buttons = re.findall(r'- button\s+"([^"]*)"', yaml_text)

        parts = [f"page: {url}"]
        if headings:
            parts.append(f"headings: {', '.join(headings[:2])}")
        if buttons:
            parts.append(f"buttons: {', '.join(buttons[:3])}")

        summary = " | ".join(parts)
        if len(summary) > 120:
            summary = summary[:117] + "..."
        return f"[{summary}]"

    @classmethod
    def _strip_old_snapshot(cls, output_str: str) -> str:
        """Replace snapshot ARIA tree in old turns with a mini summary.

        The model only needs to know *what action was taken* from old turns,
        not what the page looked like. The mini summary preserves key page
        indicators (URL, headings, buttons) for long-flow recall.
        """
        page_lines: list[str] = []
        in_page = False
        for line in output_str.splitlines():
            if line.startswith("### Page"):
                in_page = True
            elif line.startswith("###") and in_page:
                in_page = False
            if in_page:
                page_lines.append(line)

        if not page_lines:
            return output_str

        summary = cls._make_page_summary(output_str)

        # Remove the Snapshot block, replace with mini summary.
        stripped = re.sub(
            r"### Snapshot\n```yaml\n.*?```",
            summary,
            output_str,
            flags=re.DOTALL,
        )
        # Also remove file-ref snapshots: - [Snapshot](filename)
        stripped = re.sub(r"-?\s*\[Snapshot\]\([^)]+\)", summary, stripped)
        return stripped

    def _trim_output_item(self, item: dict[str, Any], tool_names: tuple[str, ...]) -> dict[str, Any]:
        """Trim old-turn outputs: drop snapshot ARIA tree, char-truncation fallback."""
        output_str = _serialize_output(item.get("output", ""))
        if not output_str:
            return item

        lowered_names = " ".join(tool_names).lower()
        is_browser = any(m in lowered_names for m in ("browser_", "playwright", "navigate", "snapshot"))

        # For old browser turns, resolve file refs then replace ARIA tree with mini summary.
        if self.snapshot_filter and is_browser:
            output_str = self._resolve_snapshot_refs(output_str)
            stripped = self._strip_old_snapshot(output_str)
            if stripped != output_str:
                trimmed_item = dict(item)
                trimmed_item["output"] = stripped
                return trimmed_item

        # Fallback: original char-count truncation for non-snapshot outputs.
        aggressive = any(marker in lowered_names for marker in self.aggressive_tool_markers)
        max_chars = self.aggressive_max_chars if aggressive else self.default_max_chars
        preview_chars = self.aggressive_preview_chars if aggressive else self.default_preview_chars

        if len(output_str) <= max_chars:
            return item

        display_name = tool_names[0] if tool_names else "tool"
        summary = (
            f"[Trimmed: {display_name} output — {len(output_str)} chars → "
            f"{preview_chars} char preview]\n{output_str[:preview_chars]}..."
        )

        trimmed_item = dict(item)
        trimmed_item["output"] = summary
        return trimmed_item
