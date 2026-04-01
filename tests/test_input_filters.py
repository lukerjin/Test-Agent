"""Tests for model input filtering."""

import tempfile
from pathlib import Path

from universal_debug_agent.orchestrator.input_filters import (
    MCPToolOutputFilter,
    _serialize_output,
    _extract_interactive_snapshot,
    _ROLE_PATTERN,
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


# ── New tests ────────────────────────────────────────────────────────────────


def test_role_matching_no_false_positives():
    """'row' should not match 'arrow', 'cell' should not match 'cancelled'."""
    assert not _ROLE_PATTERN.search("- generic [ref=e1]: arrow")
    assert not _ROLE_PATTERN.search("- generic [ref=e2]: cancelled")
    assert not _ROLE_PATTERN.search("- generic [ref=e3]: blinking")
    # But real roles should match
    assert _ROLE_PATTERN.search("- row [ref=e10]:")
    assert _ROLE_PATTERN.search("- cell [ref=e11]: $268.45")
    assert _ROLE_PATTERN.search("- link \"About\" [ref=e12]")
    assert _ROLE_PATTERN.search('- button "Submit" [ref=e13]')
    assert _ROLE_PATTERN.search("- tab [ref=e14]")
    assert _ROLE_PATTERN.search("- img [ref=e15]")


def test_resolve_snapshot_refs_with_subdir_path():
    """File refs like .playwright-mcp/page-xxx.yml should be resolved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create the snapshot file in the .playwright-mcp subdirectory
        subdir = Path(tmpdir) / ".playwright-mcp"
        subdir.mkdir()
        snap_file = subdir / "page-2026-01-01.yml"
        snap_file.write_text("- button [ref=e1]: Submit\n")

        filter_ = MCPToolOutputFilter(snapshot_dir=Path(tmpdir))

        input_text = "### Page\n- Page URL: http://example.com\n- [Snapshot](.playwright-mcp/page-2026-01-01.yml)"
        resolved = filter_._resolve_snapshot_refs(input_text)

        assert "### Snapshot" in resolved
        assert "button" in resolved
        assert "[Snapshot](" not in resolved


def test_resolve_snapshot_refs_fallback_on_missing_file():
    """When the file doesn't exist, original text is preserved."""
    with tempfile.TemporaryDirectory() as tmpdir:
        filter_ = MCPToolOutputFilter(snapshot_dir=Path(tmpdir))
        input_text = "- [Snapshot](.playwright-mcp/nonexistent.yml)"
        result = filter_._resolve_snapshot_refs(input_text)
        # Original text preserved
        assert "[Snapshot](" in result


def test_parent_context_preserved():
    """When a button is kept, its parent form/label lines should also be kept."""
    snapshot_text = (
        "### Page\n- Page URL: http://example.com\n"
        "### Snapshot\n```yaml\n"
        "- generic [ref=e1]:\n"
        "  - form [ref=e2]:\n"
        "    - generic [ref=e3]: Customer Details\n"
        "      - textbox [ref=e4]\n"
        "      - button \"Continue\" [ref=e5]\n"
        "```"
    )
    result = _extract_interactive_snapshot(snapshot_text, max_lines=None)
    assert "form" in result
    assert "Customer Details" in result
    assert "button" in result
    assert "Continue" in result
    assert "textbox" in result


def test_old_turn_mini_summary():
    """Old turns should get a mini summary instead of [snapshot omitted]."""
    snapshot_text = (
        "### Page\n- Page URL: https://example.com/checkout\n- Page Title: Checkout\n"
        "### Snapshot\n```yaml\n"
        '- heading "Secure Checkout" [ref=e1]\n'
        '- button "Continue" [ref=e2]\n'
        '- button "Place Order" [ref=e3]\n'
        "```"
    )
    result = MCPToolOutputFilter._strip_old_snapshot(snapshot_text)
    assert "[snapshot omitted]" not in result
    assert "Secure Checkout" in result
    assert "Continue" in result
    assert "Place Order" in result
    assert "example.com/checkout" in result


def test_old_turn_mini_summary_with_file_ref():
    """File-ref snapshots in old turns should also get a mini summary."""
    output_text = (
        "### Page\n- Page URL: https://example.com/cart\n"
        "- [Snapshot](.playwright-mcp/page-xxx.yml)"
    )
    result = MCPToolOutputFilter._strip_old_snapshot(output_text)
    assert "[Snapshot](" not in result
    assert "example.com/cart" in result


def test_filter_config_from_profile():
    """FilterConfig fields should be wired into MCPToolOutputFilter."""
    from universal_debug_agent.schemas.profile import BoundariesConfig, FilterConfig

    # Default FilterConfig
    bc = BoundariesConfig()
    assert bc.filter.recent_turns == 1
    assert bc.filter.evidence_preview_chars == 1500

    # Custom FilterConfig
    bc2 = BoundariesConfig(filter=FilterConfig(recent_turns=3, evidence_preview_chars=3000))
    assert bc2.filter.recent_turns == 3
    assert bc2.filter.evidence_preview_chars == 3000
