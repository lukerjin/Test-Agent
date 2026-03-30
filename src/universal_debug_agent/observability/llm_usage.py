"""LLM usage tracking with pluggable storage backends."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field
from openai import APIConnectionError, APIStatusError

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
    status: str = "success"
    error_type: str | None = None
    error_message: str | None = None
    status_code: int | None = None
    rate_limit_limit_tokens: int | None = None
    rate_limit_used_tokens: int | None = None
    rate_limit_requested_tokens: int | None = None
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
    error_count: int = 0
    last_error_type: str | None = None
    last_error_message: str | None = None


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
        self.runs_root = self.root_dir / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def write_call(self, record: LLMCallRecord) -> None:
        self._append(self.calls_path, record.model_dump())

    def write_summary(self, summary: LLMRunSummary) -> None:
        self._append(self.summaries_path, summary.model_dump())

    def _append(self, path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path


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
        self._error_count = 0
        self._last_error_type: str | None = None
        self._last_error_message: str | None = None

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
            error_count=self._error_count,
            last_error_type=self._last_error_type,
            last_error_message=self._last_error_message,
        )
        self.store.write_summary(summary)
        return summary

    def record_error(self, error: BaseException, phase: str) -> None:
        self._phases.append(phase)
        self._call_count += 1
        self._error_count += 1
        self._last_error_type = type(error).__name__
        self._last_error_message = str(error)

        limit, used, requested = _extract_rate_limit_metrics(str(error))
        request_id = getattr(error, "request_id", None)
        status_code = getattr(error, "status_code", None)

        record = LLMCallRecord(
            run_id=self.run_id,
            phase=phase,
            call_index=self._call_count,
            project_name=self.project_name,
            scenario=self.scenario,
            provider=self.provider,
            model=self.model,
            request_id=request_id,
            status="error",
            error_type=type(error).__name__,
            error_message=str(error),
            status_code=status_code,
            rate_limit_limit_tokens=limit,
            rate_limit_used_tokens=used,
            rate_limit_requested_tokens=requested,
            usage=_serialize_error_usage(error),
        )
        self.store.write_call(record)

    def write_final_output(self, final_output: object) -> Path | None:
        if not isinstance(self.store, JsonlUsageStore):
            return None

        run_dir = self.store.run_dir(self.run_id)
        suffix = "json" if isinstance(final_output, dict) else "txt"
        path = run_dir / f"final_output.{suffix}"
        if isinstance(final_output, dict):
            payload = json.dumps(final_output, ensure_ascii=False, indent=2)
        else:
            payload = str(final_output)
        path.write_text(payload, encoding="utf-8")
        return path

    def write_error_output(self, error: BaseException) -> Path | None:
        if not isinstance(self.store, JsonlUsageStore):
            return None

        run_dir = self.store.run_dir(self.run_id)
        path = run_dir / "error.txt"
        path.write_text(str(error), encoding="utf-8")
        return path


def default_usage_dir(project_name: str) -> str:
    safe_name = project_name.lower().replace(" ", "_").replace("/", "_")
    return f"./usage/{safe_name}"


def _extract_rate_limit_metrics(message: str) -> tuple[int | None, int | None, int | None]:
    limit_match = re.search(r"Limit\s+(\d+)", message)
    used_match = re.search(r"Used\s+(\d+)", message)
    requested_match = re.search(r"Requested\s+(\d+)", message)
    limit = int(limit_match.group(1)) if limit_match else None
    used = int(used_match.group(1)) if used_match else None
    requested = int(requested_match.group(1)) if requested_match else None
    return limit, used, requested


def _serialize_error_usage(error: BaseException) -> dict:
    payload = {
        "error_type": type(error).__name__,
        "message": str(error),
    }
    if isinstance(error, APIStatusError):
        payload["status_code"] = error.status_code
        payload["request_id"] = error.request_id
        payload["body"] = error.body
    elif isinstance(error, APIConnectionError):
        payload["body"] = error.body
    return payload
