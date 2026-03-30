"""Human-readable execution trace recording for agent runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.items import ItemHelpers, ModelResponse


@dataclass
class TraceEvent:
    kind: str
    title: str
    content: str


class ExecutionTraceRecorder:
    """Persist a structured execution trace as JSONL plus a readable markdown summary."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.run_dir / "trace.jsonl"
        self.md_path = self.run_dir / "trace.md"
        self._events: list[TraceEvent] = []

    def record(self, kind: str, title: str, content: str) -> None:
        event = TraceEvent(kind=kind, title=title, content=content)
        self._events.append(event)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")
        self._render_markdown()

    def record_llm_response(self, response: ModelResponse) -> None:
        text_parts: list[str] = []
        tool_parts: list[str] = []
        for item in response.output:
            item_type = getattr(item, "type", None)
            text = ItemHelpers.extract_text(item)
            if text:
                text_parts.append(text[:1000])
            elif item_type:
                tool_parts.append(str(item_type))

        content_parts: list[str] = []
        if text_parts:
            content_parts.append("Text:\n" + "\n\n".join(text_parts))
        if tool_parts:
            content_parts.append("Output items:\n" + ", ".join(tool_parts))
        if response.request_id:
            content_parts.append(f"request_id: {response.request_id}")
        self.record("llm_response", "LLM Response", "\n\n".join(content_parts) or "(empty)")

    def _render_markdown(self) -> None:
        lines = ["# Execution Trace", ""]
        for idx, event in enumerate(self._events, start=1):
            lines.append(f"## {idx}. {event.title}")
            lines.append(f"- Kind: {event.kind}")
            lines.append("")
            lines.append(event.content)
            lines.append("")
        self.md_path.write_text("\n".join(lines), encoding="utf-8")
