"""Tests for LLM usage tracking."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agents.items import ModelResponse
from agents.usage import Usage

from universal_debug_agent.observability.llm_usage import (
    JsonlUsageStore,
    LLMUsageTracker,
    default_usage_dir,
)


def test_default_usage_dir():
    assert default_usage_dir("My App/Prod") == "./usage/my_app_prod"


def test_jsonl_usage_store_and_tracker():
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonlUsageStore(tmp)
        tracker = LLMUsageTracker(
            project_name="Test Project",
            scenario="checkout flow",
            provider="openai",
            model="gpt-5.4-nano",
            store=store,
            run_id="run-1",
        )

        response = ModelResponse(
            output=[],
            usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
            response_id="resp-1",
            request_id="req-1",
        )
        result = SimpleNamespace(
            raw_responses=[response],
            context_wrapper=SimpleNamespace(
                usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15)
            ),
        )

        tracker.record_run_result(result, phase="react")
        summary = tracker.write_summary()

        calls = [json.loads(line) for line in Path(store.calls_path).read_text().splitlines()]
        summaries = [
            json.loads(line) for line in Path(store.summaries_path).read_text().splitlines()
        ]

        assert len(calls) == 1
        assert calls[0]["request_id"] == "req-1"
        assert calls[0]["total_tokens"] == 15

        assert len(summaries) == 1
        assert summaries[0]["call_count"] == 1
        assert summaries[0]["total_tokens"] == 15
        assert summary.run_id == "run-1"
