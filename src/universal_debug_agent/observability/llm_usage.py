"""LLM usage tracking with pluggable storage backends."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from agents.result import RunResult
from agents.usage import serialize_usage


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LLMCallRecord(BaseModel):
    run_id: str
    phase: str
    call_index: int
    timestamp: str = Field(default_factory=_utc_now)
    project_name: str
    scenario: str
    provider: str
    model: str
    request_id: str | None = None
    response_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    usage: dict = Field(default_factory=dict)


class LLMRunSummary(BaseModel):
    run_id: str
    timestamp: str = Field(default_factory=_utc_now)
    project_name: str
    scenario: str
    provider: str
    model: str
    phases: list[str] = Field(default_factory=list)
    call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


class UsageStore(Protocol):
    def write_call(self, record: LLMCallRecord) -> None: ...

    def write_summary(self, summary: LLMRunSummary) -> None: ...


class JsonlUsageStore:
    """Local JSONL store for LLM usage telemetry."""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.calls_path = self.root_dir / "llm_calls.jsonl"
        self.summaries_path = self.root_dir / "llm_runs.jsonl"

    def write_call(self, record: LLMCallRecord) -> None:
        self._append(self.calls_path, record.model_dump())

    def write_summary(self, summary: LLMRunSummary) -> None:
        self._append(self.summaries_path, summary.model_dump())

    def _append(self, path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


class MySQLUsageStore:
    """Extension point for persisting usage to MySQL."""

    def write_call(self, record: LLMCallRecord) -> None:
        raise NotImplementedError("MySQLUsageStore is not implemented yet.")

    def write_summary(self, summary: LLMRunSummary) -> None:
        raise NotImplementedError("MySQLUsageStore is not implemented yet.")


class PostgresUsageStore:
    """Extension point for persisting usage to PostgreSQL."""

    def write_call(self, record: LLMCallRecord) -> None:
        raise NotImplementedError("PostgresUsageStore is not implemented yet.")

    def write_summary(self, summary: LLMRunSummary) -> None:
        raise NotImplementedError("PostgresUsageStore is not implemented yet.")


class LLMUsageTracker:
    """Collect per-call usage and persist summaries through a pluggable store."""

    def __init__(
        self,
        project_name: str,
        scenario: str,
        provider: str,
        model: str,
        store: UsageStore,
        run_id: str | None = None,
    ):
        self.project_name = project_name
        self.scenario = scenario
        self.provider = provider
        self.model = model
        self.store = store
        self.run_id = run_id or str(uuid.uuid4())
        self._call_count = 0
        self._phases: list[str] = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._cached_tokens = 0
        self._reasoning_tokens = 0

    def record_run_result(self, result: RunResult, phase: str) -> None:
        self._phases.append(phase)
        for idx, response in enumerate(result.raw_responses, start=1):
            usage = response.usage
            input_details = usage.input_tokens_details
            output_details = usage.output_tokens_details

            record = LLMCallRecord(
                run_id=self.run_id,
                phase=phase,
                call_index=self._call_count + idx,
                project_name=self.project_name,
                scenario=self.scenario,
                provider=self.provider,
                model=self.model,
                request_id=response.request_id,
                response_id=response.response_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_tokens=input_details.cached_tokens or 0,
                reasoning_tokens=output_details.reasoning_tokens or 0,
                usage=serialize_usage(usage),
            )
            self.store.write_call(record)

        aggregated = result.context_wrapper.usage
        self._call_count += aggregated.requests
        self._input_tokens += aggregated.input_tokens
        self._output_tokens += aggregated.output_tokens
        self._total_tokens += aggregated.total_tokens
        self._cached_tokens += aggregated.input_tokens_details.cached_tokens or 0
        self._reasoning_tokens += aggregated.output_tokens_details.reasoning_tokens or 0

    def write_summary(self) -> LLMRunSummary:
        summary = LLMRunSummary(
            run_id=self.run_id,
            project_name=self.project_name,
            scenario=self.scenario,
            provider=self.provider,
            model=self.model,
            phases=self._phases,
            call_count=self._call_count,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            total_tokens=self._total_tokens,
            cached_tokens=self._cached_tokens,
            reasoning_tokens=self._reasoning_tokens,
        )
        self.store.write_summary(summary)
        return summary


def default_usage_dir(project_name: str) -> str:
    safe_name = project_name.lower().replace(" ", "_").replace("/", "_")
    return f"./usage/{safe_name}"
