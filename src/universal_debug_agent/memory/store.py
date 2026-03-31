"""Memory store — JSONL-based investigation memory.

Each record is one past investigation. The store supports:
- load(): read all records
- save(): append a new record
- build_prompt_context(): format relevant records for prompt injection

File format (one JSON object per line):
{"issue": "...", "root_cause": "...", "classification": "...", "lesson": "...", "tags": [...], "timestamp": "..."}
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Lowercase and replace all non-alphanumeric characters with spaces."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text.lower()).strip()


class MemoryRecord(BaseModel):
    """A single past investigation record."""

    issue: str
    root_cause: str = ""
    classification: str = "unknown"
    key_findings: list[str] = Field(default_factory=list)
    lesson: str = ""
    tags: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class MemoryStore:
    """JSONL file-based memory store with tag-based retrieval."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._records: list[MemoryRecord] = []
        self._tag_index: dict[str, list[int]] = defaultdict(list)  # tag → [record indices]
        self._loaded = False

    def load(self) -> list[MemoryRecord]:
        """Load all memory records from the JSONL file."""
        if not self.path.exists():
            self._records = []
            self._tag_index = defaultdict(list)
            self._loaded = True
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

        self._records = records
        self._build_index()
        self._loaded = True
        return records

    def save(self, record: MemoryRecord) -> None:
        """Append a single record to the JSONL file and update the index."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(record.model_dump_json() + "\n")

        if not self._loaded:
            self.load()
        else:
            idx = len(self._records)
            self._records.append(record)
            for tag in record.tags:
                self._tag_index[tag].append(idx)

        logger.info(f"Memory saved: {record.issue[:60]}")

    def build_prompt_context(self, max_entries: int = 3, scenario: str = "") -> str:
        """Format relevant memory records for injection into the agent prompt.

        If scenario is provided, retrieves records whose tags overlap with the
        scenario's tags. Falls back to most-recent records when no matches found.
        Returns empty string if no records exist.
        """
        if not self._loaded:
            self.load()

        if not self._records:
            return ""

        if scenario:
            selected = self._retrieve_by_scenario(scenario, max_entries)
        else:
            selected = self._records[-max_entries:]

        if not selected:
            return ""

        parts: list[str] = [
            "## ⚠️ Past Run Lessons — MUST follow, override scenario steps if needed",
            f"{len(selected)} relevant past run(s) on this scenario failed. "
            "The lessons below describe what went wrong and the correct approach. "
            "**These lessons take priority over the scenario step descriptions. "
            "If a lesson says 'do not do X', skip X even if the scenario says to do it.**\n",
        ]

        for i, rec in enumerate(selected, 1):
            entry = f"### Lesson #{i} ({rec.timestamp[:10]}) — {rec.classification.upper()}\n"
            entry += f"- **Scenario**: {rec.issue}\n"
            if rec.lesson:
                entry += f"- **What to do differently**: {rec.lesson}\n"
            else:
                if rec.root_cause:
                    entry += f"- **Root Cause**: {rec.root_cause}\n"
                if rec.key_findings:
                    entry += f"- **Findings**: {'; '.join(rec.key_findings)}\n"
            parts.append(entry)

        parts.append(
            "**Do not repeat any failing action from past runs. Follow the lessons above exactly.**\n"
        )

        return "\n".join(parts)

    def _retrieve_by_scenario(self, scenario: str, max_entries: int) -> list[MemoryRecord]:
        """Return up to max_entries records most relevant to the scenario.

        Scores each record by how many of its tags appear in the scenario text,
        then returns the highest-scoring recent records.
        """
        scenario_normalized = _normalize(scenario)

        scored: list[tuple[int, int]] = []  # (score, original_index)
        for idx, rec in enumerate(self._records):
            if not rec.tags or not rec.lesson:
                continue
            score = sum(1 for tag in rec.tags if _normalize(tag) in scenario_normalized)
            if score > 0:
                scored.append((score, idx))

        if not scored:
            return []

        # Sort by score desc, then by recency (higher index = more recent)
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        top_indices = [idx for _, idx in scored[:max_entries]]
        # Return in chronological order
        top_indices.sort()
        return [self._records[i] for i in top_indices]

    def _build_index(self) -> None:
        """Build in-memory tag → record index from loaded records."""
        self._tag_index = defaultdict(list)
        for idx, rec in enumerate(self._records):
            for tag in rec.tags:
                self._tag_index[tag].append(idx)


def resolve_memory_path(path_template: str, project_name: str) -> str:
    """Resolve {project_name} placeholder in memory path."""
    safe_name = project_name.lower().replace(" ", "_").replace("/", "_")
    return path_template.replace("{project_name}", safe_name)
