"""Model input filters for controlling tool-output growth across turns."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.extensions.tool_output_trimmer import ToolOutputTrimmer
from agents.run_config import CallModelData, ModelInputData


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
})

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
    kept: list[str] = []

    for line in yaml_lines:
        stripped = line.strip()
        lower = stripped.lower()

        # Drop [unchanged] back-references — zero information density.
        if "[unchanged]" in stripped:
            continue

        # Keep any line that names an interactive ARIA role.
        if any(role in lower for role in _INTERACTIVE_ROLES):
            kept.append(line)
            continue

        # Keep lines with action/state markers.
        if any(marker in stripped for marker in _KEEP_MARKERS):
            kept.append(line)
            continue

        # Keep inline-text nodes: "- generic [ref=eN]: visible text"
        # These carry section labels, prices, error messages, etc.
        if re.search(r"\[ref=e\d+\]:\s+\S", stripped):
            kept.append(line)
            continue

    if not kept:
        return ""

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
        user_msg_count = 0
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if isinstance(item, dict) and item.get("role") == "user":
                user_msg_count += 1
                if user_msg_count >= self.recent_turns:
                    return i
        return len(items)

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
            # Resolve the file path — auto-generated snapshots live in .playwright-mcp/
            # subdirectory, named snapshots are in the root of snapshot_dir.
            # Both are inlined; _extract_interactive_snapshot filters them down to
            # interactive elements only (~2-5K), so raw size (20-55K) is not a problem.
            candidates = [name, f"{name}.md"] if "/" not in name else [name]
            for candidate in candidates:
                path = self.snapshot_dir / candidate
                try:
                    content = path.read_text(encoding="utf-8")
                    return f"### Snapshot\n```yaml\n{content}\n```"
                except OSError:
                    continue
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
        output_str = self._resolve_snapshot_refs(output_str)
        filtered = _extract_interactive_snapshot(output_str, max_lines=None)
        if not filtered:
            return item
        trimmed_item = dict(item)
        trimmed_item["output"] = filtered
        return trimmed_item

    @staticmethod
    def _strip_old_snapshot(output_str: str) -> str:
        """Replace snapshot ARIA tree in old turns with just the Page section.

        The model only needs to know *what action was taken* from old turns,
        not what the page looked like. Keeping the full ARIA tree wastes tokens
        without adding useful context.
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

        # Remove the Snapshot block entirely, keep everything else.
        stripped = re.sub(
            r"### Snapshot\n```yaml\n.*?```",
            "[snapshot omitted]",
            output_str,
            flags=re.DOTALL,
        )
        # Also remove file-ref snapshots: - [Snapshot](filename)
        stripped = re.sub(r"-?\s*\[Snapshot\]\([^)]+\)", "[snapshot omitted]", stripped)
        return stripped

    def _trim_output_item(self, item: dict[str, Any], tool_names: tuple[str, ...]) -> dict[str, Any]:
        """Trim old-turn outputs: drop snapshot ARIA tree, char-truncation fallback."""
        output_str = _serialize_output(item.get("output", ""))
        if not output_str:
            return item

        lowered_names = " ".join(tool_names).lower()
        is_browser = any(m in lowered_names for m in ("browser_", "playwright", "navigate", "snapshot"))

        # For old browser turns, drop the ARIA tree entirely — only Page metadata kept.
        if self.snapshot_filter and is_browser:
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
