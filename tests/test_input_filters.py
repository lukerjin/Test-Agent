"""Tests for model input filtering."""

from universal_debug_agent.orchestrator.input_filters import MCPToolOutputFilter
from agents.run_config import CallModelData, ModelInputData


def test_mcp_filter_trims_large_old_browser_outputs():
    filter_ = MCPToolOutputFilter(recent_turns=1)
    huge_output = "x" * 5000
    data = CallModelData(
        model_data=ModelInputData(
            input=[
                {"role": "user", "content": "start checkout"},
                {"type": "function_call", "call_id": "c1", "name": "browser_navigate"},
                {"type": "function_call_output", "call_id": "c1", "output": huge_output},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "continue"},
            ],
            instructions="test",
        ),
        agent=None,
        context=None,
    )

    result = filter_(data)
    output = result.input[2]["output"]

    assert output.startswith("[Trimmed: browser_navigate output")
    assert len(output) < len(huge_output)


def test_mcp_filter_keeps_recent_turns_untrimmed():
    filter_ = MCPToolOutputFilter(recent_turns=1)
    huge_output = "x" * 5000
    data = CallModelData(
        model_data=ModelInputData(
            input=[
                {"role": "user", "content": "only turn"},
                {"type": "function_call", "call_id": "c1", "name": "browser_navigate"},
                {"type": "function_call_output", "call_id": "c1", "output": huge_output},
            ],
            instructions="test",
        ),
        agent=None,
        context=None,
    )

    result = filter_(data)

    assert result.input[2]["output"] == huge_output
