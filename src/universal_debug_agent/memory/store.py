"""Memory store — JSONL-based investigation memory.

Each record is one past investigation. The store supports:
- load(): read all records
- save(): append a new record
- build_prompt_context(): format recent records for prompt injection

File format (one JSON object per line):
{"issue": "...", "root_cause": "...", "classification": "...", "key_findings": [...], "timestamp": "..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MemoryRecord(BaseModel):
    """A single past investigation record."""

    issue: str
    root_cause: str = ""
    classification: str = "unknown"
    key_findings: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class MemoryStore:
    """JSONL file-based memory store for a project."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> list[MemoryRecord]:
        """Load all memory records from the JSONL file."""
        if not self.path.exists():
            return []

        records: list[MemoryRecord] = []
        for line_num, line in enumerate(self.path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(MemoryRecord.model_validate_json(line))
            except Exception as e:
                logger.warning(f"Skipping invalid memory record at line {line_num}: {e}")
        return records

    def save(self, record: MemoryRecord) -> None:
        """Append a single record to the JSONL file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(record.model_dump_json() + "\n")
        logger.info(f"Memory saved: {record.issue[:60]}")

    def build_prompt_context(self, max_entries: int = 20) -> str:
        """Format recent memory records for injection into the agent prompt.

        Returns empty string if no records exist.
        """
        records = self.load()
        if not records:
            return ""

        # Take the most recent N records
        recent = records[-max_entries:]

        parts: list[str] = [
            "## Past Investigation Memory",
            f"You have access to {len(recent)} past investigation(s) for this project.",
            "Use these to avoid repeating dead ends and to form hypotheses faster.\n",
        ]

        for i, rec in enumerate(recent, 1):
            entry = f"### Memory #{i} ({rec.timestamp[:10]})\n"
            entry += f"- **Issue**: {rec.issue}\n"
            if rec.root_cause:
                entry += f"- **Root Cause**: {rec.root_cause}\n"
            entry += f"- **Classification**: {rec.classification}\n"
            if rec.key_findings:
                entry += f"- **Key Findings**: {'; '.join(rec.key_findings)}\n"
            parts.append(entry)

        parts.append(
            "**Important**: These are references only. Each investigation is unique — "
            "verify everything independently. Do not assume the same root cause applies.\n"
        )

        return "\n".join(parts)


def resolve_memory_path(path_template: str, project_name: str) -> str:
    """Resolve {project_name} placeholder in memory path."""
    safe_name = project_name.lower().replace(" ", "_").replace("/", "_")
    return path_template.replace("{project_name}", safe_name)
