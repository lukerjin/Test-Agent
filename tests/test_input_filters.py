"""Tests for model input filtering."""

from universal_debug_agent.orchestrator.input_filters import (
    MCPToolOutputFilter,
    _serialize_output,
    _extract_interactive_snapshot,
)
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


def test_serialize_output_extracts_text_from_structured_content():
    """Responses API MCP outputs are stored as [{"type": "input_text", "text": "..."}].
    _serialize_output must extract the text so regex filters work on real newlines.
    """
    structured = [{"type": "input_text", "text": "### Page\n- URL: http://example.com\n### Snapshot\n```yaml\n- button [ref=e1]: Submit\n```"}]
    result = _serialize_output(structured)
    assert "### Snapshot" in result
    assert "\n" in result  # real newlines, not escaped \\n
    assert result == structured[0]["text"]


def test_extract_interactive_snapshot_on_structured_output():
    """Verify the snapshot filter works end-to-end on real Playwright output format."""
    playwright_text = (
        "### Ran Playwright code\n```js\nawait page.goto('http://example.com');\n```\n"
        "### Page\n- Page URL: http://example.com\n- Page Title: Test\n"
        "### Snapshot\n```yaml\n"
        "- generic [active] [ref=e1]:\n"
        "  - button \"Submit\" [ref=e2] [cursor=pointer]\n"
        "  - generic [ref=e3]: some static text\n"
        "  - generic [ref=e4]: more static text\n"
        "```"
    )
    # Test via structured content (real-world format)
    structured_output = [{"type": "input_text", "text": playwright_text}]
    serialized = _serialize_output(structured_output)
    filtered = _extract_interactive_snapshot(serialized)
    assert filtered, "snapshot filter should extract interactive elements"
    assert "button" in filtered
    assert "Submit" in filtered


def test_mcp_filter_applies_snapshot_filter_to_structured_playwright_output():
    """Verify MCPToolOutputFilter works with the real Responses API output format.

    The critical bug was: output is [{"type": "input_text", "text": "..."}] (a list),
    and str() on that produces a Python repr with escaped \\n, breaking the regex.
    After the fix, text is extracted properly and the snapshot filter runs.
    """
    # Pure containers with no visible text — these should be filtered out
    filler = "".join("  - generic [ref=e%d]\n" % i for i in range(100))
    playwright_text = (
        "### Ran Playwright code\n```js\nawait page.goto('http://example.com');\n```\n"
        "### Page\n- Page URL: http://example.com\n- Page Title: Test\n- Console: 0 errors\n"
        "### Snapshot\n```yaml\n"
        + filler
        + "  - button \"Checkout\" [ref=e200] [cursor=pointer]\n"
        "```"
    )
    structured_output = [{"type": "input_text", "text": playwright_text}]

    filter_ = MCPToolOutputFilter(recent_turns=1)
    data = CallModelData(
        model_data=ModelInputData(
            input=[
                {"role": "user", "content": "run test"},
                {"type": "function_call", "call_id": "c1", "name": "browser_navigate"},
                {"type": "function_call_output", "call_id": "c1", "output": structured_output},
            ],
            instructions="test",
        ),
        agent=None,
        context=None,
    )

    result = filter_(data)
    output = result.input[2]["output"]
    # Should be a filtered string, not the original structured list
    assert isinstance(output, str)
    assert "button" in output
    assert "Checkout" in output
    # 100 filler generic-only lines (no text, no ref pattern) should be dropped
    assert len(output) < len(playwright_text)


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
