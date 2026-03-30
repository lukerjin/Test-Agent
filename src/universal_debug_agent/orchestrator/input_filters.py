"""Model input filters for controlling tool-output growth across turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.extensions.tool_output_trimmer import ToolOutputTrimmer
from agents.run_config import CallModelData, ModelInputData


def _serialize_output(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


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

    def __post_init__(self) -> None:
        self._base_trimmer = ToolOutputTrimmer(
            recent_turns=self.recent_turns,
            max_output_chars=self.default_max_chars,
            preview_chars=self.default_preview_chars,
        )

    def __call__(self, data: CallModelData[Any]) -> ModelInputData:
        trimmed = self._base_trimmer(data)
        boundary = self._find_recent_boundary(trimmed.input)
        if boundary == len(trimmed.input):
            return trimmed

        call_id_to_names = self._build_call_id_to_names(trimmed.input)
        new_items: list[Any] = []

        for idx, item in enumerate(trimmed.input):
            if idx < boundary and isinstance(item, dict) and item.get("type") == "function_call_output":
                call_id = str(item.get("call_id") or item.get("id") or "")
                tool_names = call_id_to_names.get(call_id, ())
                item = self._trim_output_item(item, tool_names)
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

    def _trim_output_item(self, item: dict[str, Any], tool_names: tuple[str, ...]) -> dict[str, Any]:
        output_str = _serialize_output(item.get("output", ""))
        if not output_str:
            return item

        lowered_names = " ".join(tool_names).lower()
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
