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
