"""Tests for Playwright-specific tool constraints in InvestigationHooks."""

from __future__ import annotations

import json

import pytest
from agents.exceptions import UserError

from universal_debug_agent.orchestrator.hooks import (
    InvestigationHooks,
    _summarize_tool_result,
)


class _DummyDetector:
    def record(self, tool_name: str, tool_args: str) -> None:
        self.last = (tool_name, tool_args)

    def update_last_result(self, result_hash: str) -> None:
        self.result_hash = result_hash

    def is_stuck(self) -> bool:
        return False


class _DummyEvidenceCollector:
    def collect(self, tool_name: str, tool_args: str, result: str) -> None:
        self.last = (tool_name, tool_args, result)

    def build_summary(self) -> str:
        return "summary"


class _DummyToolCall:
    def __init__(self, arguments: str):
        self.arguments = arguments


class _DummyToolContext:
    def __init__(self, arguments: str):
        self.tool_arguments = arguments
        self.tool_call = _DummyToolCall(arguments)


class _DummyTool:
    def __init__(self, name: str):
        self.name = name


def _hooks() -> InvestigationHooks:
    return InvestigationHooks(
        stuck_detector=_DummyDetector(),
        evidence_collector=_DummyEvidenceCollector(),
    )


@pytest.mark.asyncio
async def test_screenshot_defaults_type_to_png():
    hooks = _hooks()
    context = _DummyToolContext(json.dumps({"fullPage": True}))

    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_take_screenshot"))

    parsed = json.loads(context.tool_arguments)
    assert parsed["type"] == "png"
    assert json.loads(context.tool_call.arguments)["type"] == "png"


@pytest.mark.asyncio
async def test_ambiguous_browser_click_is_blocked():
    hooks = _hooks()
    context = _DummyToolContext(json.dumps({"locator": "locator('form').getByRole('button')"}))

    with pytest.raises(UserError, match="Ambiguous browser_click target blocked"):
        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))


@pytest.mark.asyncio
async def test_named_browser_click_is_allowed():
    hooks = _hooks()
    context = _DummyToolContext(json.dumps({"selector": "button:has-text('Checkout Now')"}))

    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))


@pytest.mark.asyncio
async def test_css_selector_in_ref_is_blocked():
    hooks = _hooks()
    context = _DummyToolContext(json.dumps({"ref": "button:has-text(\"Add to cart\")", "element": "Add to cart button"}))

    with pytest.raises(UserError, match="snapshot ref"):
        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))


@pytest.mark.asyncio
async def test_semantic_ref_is_blocked():
    hooks = _hooks()
    context = _DummyToolContext(json.dumps({"ref": "button_add_to_cart"}))

    with pytest.raises(UserError, match="snapshot ref"):
        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))


@pytest.mark.asyncio
async def test_valid_snapshot_ref_is_allowed():
    hooks = _hooks()
    context = _DummyToolContext(json.dumps({"ref": "e144", "element": "Add to cart button"}))

    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))


def test_summarize_tool_result_extracts_page_url():
    result = """### Page
- Page URL: https://example.test/cart
- Console: 2 errors, 1 warnings
"""
    assert _summarize_tool_result("browser_click", result) == (
        "page=https://example.test/cart; console=2 errors, 1 warnings"
    )


def test_summarize_tool_result_extracts_screenshot_name():
    result = "- [Screenshot of viewport](checkout.png)"
    assert _summarize_tool_result("browser_take_screenshot", result) == "screenshot=checkout.png"


# --- Form capture tests ---

class _MockMCPResult:
    """Simulates an MCP call_tool result with structured content."""
    def __init__(self, text: str):
        self.content = [type("Item", (), {"text": text})()]


class _MockPlaywrightServer:
    """Mock Playwright MCP server that returns configurable results."""
    def __init__(self, evaluate_result: str | None = None, error: Exception | None = None):
        self._evaluate_result = evaluate_result
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name: str, args: dict):
        self.calls.append((tool_name, args))
        if self._error is not None:
            raise self._error
        if self._evaluate_result is not None:
            return _MockMCPResult(self._evaluate_result)
        return _MockMCPResult("null")


def _hooks_with_playwright(server) -> InvestigationHooks:
    return InvestigationHooks(
        stuck_detector=_DummyDetector(),
        evidence_collector=_DummyEvidenceCollector(),
        playwright_server=server,
    )


def _wrap_evaluate_result(data_json: str) -> str:
    """Wrap JSON in the format browser_evaluate actually returns."""
    return (
        f"### Result\n{data_json}\n"
        "### Ran Playwright code\n```js\nawait page.evaluate(...);\n```"
    )


@pytest.mark.asyncio
async def test_form_capture_before_click():
    """Form data is captured when browser_click targets an element inside a form."""
    from universal_debug_agent.tools import db_tool

    form_data = json.dumps({
        "action": "https://example.test/newsletter",
        "method": "POST",
        "fields": {"email": "test@test.com", "subscribed": "1"},
    }, indent=2)
    server = _MockPlaywrightServer(evaluate_result=_wrap_evaluate_result(form_data))
    hooks = _hooks_with_playwright(server)
    context = _DummyToolContext(json.dumps({"ref": "e141", "element": "Submit"}))

    db_tool.clear_captured_form_data()
    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))

    assert len(db_tool._captured_form_data) == 1
    assert db_tool._captured_form_data[0]["action"] == "https://example.test/newsletter"
    assert db_tool._captured_form_data[0]["fields"]["email"] == "test@test.com"
    # Verify browser_evaluate was called with the correct ref
    assert any(name == "browser_evaluate" for name, _ in server.calls)
    db_tool.clear_captured_form_data()


@pytest.mark.asyncio
async def test_no_capture_for_non_form_click():
    """No form data captured when element is not inside a form."""
    from universal_debug_agent.tools import db_tool

    # browser_evaluate returns "null" wrapped in ### Result format when element is not in a form
    server = _MockPlaywrightServer(evaluate_result="### Result\nnull\n### Ran Playwright code\n```js\n```")
    hooks = _hooks_with_playwright(server)
    context = _DummyToolContext(json.dumps({"ref": "e100", "element": "Some link"}))

    db_tool.clear_captured_form_data()
    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))

    assert len(db_tool._captured_form_data) == 0
    db_tool.clear_captured_form_data()


@pytest.mark.asyncio
async def test_form_capture_failure_nonfatal():
    """browser_evaluate failure does not block the click."""
    from universal_debug_agent.tools import db_tool

    server = _MockPlaywrightServer(error=RuntimeError("MCP connection lost"))
    hooks = _hooks_with_playwright(server)
    context = _DummyToolContext(json.dumps({"ref": "e141", "element": "Submit"}))

    db_tool.clear_captured_form_data()
    # Should not raise — failure is silently caught
    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))

    assert len(db_tool._captured_form_data) == 0
    db_tool.clear_captured_form_data()


@pytest.mark.asyncio
async def test_no_capture_without_playwright():
    """No error when hooks have no playwright server."""
    from universal_debug_agent.tools import db_tool

    hooks = _hooks()  # no playwright_server
    context = _DummyToolContext(json.dumps({"ref": "e141", "element": "Submit"}))

    db_tool.clear_captured_form_data()
    await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))

    assert len(db_tool._captured_form_data) == 0
    db_tool.clear_captured_form_data()
