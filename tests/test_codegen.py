"""Tests for the v2 test code generation pipeline."""

from __future__ import annotations

import json

import pytest

from universal_debug_agent.generators.action_log import ActionLog, ActionRecord
from universal_debug_agent.generators.selector_resolver import SnapshotRefMap
from universal_debug_agent.generators.codegen import _build_action_summary
from universal_debug_agent.generators.selector_resolver import locator_from_role_and_name


# ── ActionLog ──────────────────────────────────────────────────


class TestActionLog:
    def test_record_and_len(self):
        log = ActionLog()
        assert len(log) == 0

        log.record("navigate", url="https://example.com")
        log.record("click", ref="e10", element_role="button", element_name="Submit")
        assert len(log) == 2

    def test_to_dict_list(self):
        log = ActionLog()
        log.record("navigate", url="https://example.com")
        dicts = log.to_dict_list()
        assert len(dicts) == 1
        assert dicts[0]["action_type"] == "navigate"
        assert dicts[0]["url"] == "https://example.com"
        # All unused fields should be empty strings
        assert dicts[0]["ref"] == ""

    def test_record_all_action_types(self):
        log = ActionLog()
        log.record("navigate", url="https://x.com")
        log.record("click", ref="e1", element_role="button", element_name="Go")
        log.record("fill", ref="e2", element_role="textbox", element_name="Email", value="a@b.com")
        log.record("type", ref="e3", element_role="textbox", element_name="Search", value="hello")
        log.record("select", ref="e4", element_role="combobox", element_name="Country", value="AU")
        log.record("press_key", value="Enter")
        log.record("wait", value="text=Loading")
        log.record("dialog", value="accept")
        log.record(
            "db_verify",
            check_name="order exists",
            query="SELECT * FROM orders WHERE id=1",
            expected="1 row",
            actual="1 row",
            status="pass",
        )
        assert len(log) == 9


# ── SnapshotRefMap ─────────────────────────────────────────────

# Format B: name after colon (older/alternative Playwright MCP format)
SAMPLE_SNAPSHOT_B = """\
- document [ref=e1]:
  - navigation [ref=e2]:
    - link [ref=e3]: Home
    - link [ref=e4]: Products
  - main [ref=e10]:
    - heading [ref=e11]: Product Detail
    - button [ref=e14]: Add to cart
    - generic [ref=e50] [cursor=pointer]: Place Order
  - region [ref=e20]:
    - form [ref=e21]:
      - textbox [ref=e22]: Email
      - textbox [ref=e23]: Password
      - button [ref=e24]: Sign In
"""

# Format A: name in quotes before ref (actual Playwright MCP format)
SAMPLE_SNAPSHOT_A = """\
- generic [active] [ref=e1]:
  - list [ref=e3]:
    - listitem [ref=e4]: My Printers
    - link "About Us" [ref=e6] [cursor=pointer]:
      - /url: /about-us
  - button "Submit" [ref=e32] [cursor=pointer]:
    - img [ref=e33]
  - heading "Product Detail" [level=1] [ref=e85]
  - link "Home" [ref=e3] [cursor=pointer]:
    - /url: /
  - button "Add to cart" [ref=e14] [cursor=pointer]
  - searchbox [ref=e36]
  - textbox "Email" [ref=e22]
  - textbox "Password" [ref=e23]
  - button "Sign In" [ref=e24] [cursor=pointer]
  - generic "Place Order" [ref=e50] [cursor=pointer]
"""

# Use format A as default since that's what real Playwright MCP produces
SAMPLE_SNAPSHOT = SAMPLE_SNAPSHOT_A


class TestSnapshotRefMap:
    def test_update_from_snapshot(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)

        assert len(ref_map) > 0
        info = ref_map.get("e14")
        assert info is not None
        assert info.role == "button"
        assert info.name == "Add to cart"

    def test_get_returns_none_for_unknown(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        assert ref_map.get("e999") is None

    def test_textbox_resolution(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        info = ref_map.get("e22")
        assert info is not None
        assert info.role == "textbox"
        assert info.name == "Email"

    def test_generic_with_extra_brackets(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        info = ref_map.get("e50")
        assert info is not None
        assert info.role == "generic"
        assert info.name == "Place Order"

    def test_to_locator_getbyrole(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        loc = ref_map.to_locator("e14")
        assert loc == "page.getByRole('button', { name: 'Add to cart' })"

    def test_to_locator_textbox(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        loc = ref_map.to_locator("e22")
        assert loc == "page.getByRole('textbox', { name: 'Email' })"

    def test_to_locator_generic_fallback_to_getbytext(self):
        """'generic' is not a standard ARIA role, so fall back to getByText."""
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        loc = ref_map.to_locator("e50")
        assert loc == "page.getByText('Place Order', { exact: true })"

    def test_to_locator_unknown_ref(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        loc = ref_map.to_locator("e999")
        assert "WARNING" in loc

    def test_merge_preserves_existing(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        old_count = len(ref_map)

        # Merge a small new snapshot — old refs should survive
        ref_map.merge_from_snapshot("  - button [ref=e100]: New Button")
        assert ref_map.get("e100") is not None
        assert ref_map.get("e14") is not None  # still there
        assert len(ref_map) == old_count + 1

    def test_update_clears_old(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)
        assert ref_map.get("e14") is not None

        ref_map.update_from_snapshot("  - button [ref=e100]: Only Button")
        assert ref_map.get("e14") is None  # cleared
        assert ref_map.get("e100") is not None

    def test_to_locator_escapes_quotes(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot('  - button "It\'s a test" [ref=e1]')
        loc = ref_map.to_locator("e1")
        assert "It\\'s a test" in loc

    def test_format_a_quoted_name_before_ref(self):
        """Playwright MCP real format: role "name" [ref=id] [attrs]"""
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT_A)

        info = ref_map.get("e6")
        assert info is not None
        assert info.role == "link"
        assert info.name == "About Us"

        info = ref_map.get("e32")
        assert info is not None
        assert info.role == "button"
        assert info.name == "Submit"

    def test_format_a_heading_with_level(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT_A)

        info = ref_map.get("e85")
        assert info is not None
        assert info.role == "heading"
        assert info.name == "Product Detail"

    def test_format_a_no_name(self):
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT_A)

        info = ref_map.get("e36")
        assert info is not None
        assert info.role == "searchbox"
        assert info.name == ""

    def test_format_b_name_after_colon(self):
        """Alternative format: role [ref=id]: name"""
        ref_map = SnapshotRefMap()
        ref_map.update_from_snapshot(SAMPLE_SNAPSHOT_B)

        info = ref_map.get("e14")
        assert info is not None
        assert info.role == "button"
        assert info.name == "Add to cart"

        info = ref_map.get("e22")
        assert info is not None
        assert info.role == "textbox"
        assert info.name == "Email"


# ── locator_from_role_and_name ─────────────────────────────────


class TestLocatorFromRoleAndName:
    def test_button_with_name(self):
        assert locator_from_role_and_name("button", "Submit") == \
            "page.getByRole('button', { name: 'Submit' })"

    def test_textbox_with_name(self):
        assert locator_from_role_and_name("textbox", "Email") == \
            "page.getByRole('textbox', { name: 'Email' })"

    def test_generic_role_falls_back_to_getbytext_exact(self):
        loc = locator_from_role_and_name("generic", "Place Order")
        assert loc == "page.getByText('Place Order', { exact: true })"

    def test_no_name_falls_back_to_role_only(self):
        loc = locator_from_role_and_name("button", "")
        assert loc == "page.getByRole('button')"

    def test_no_role_no_name(self):
        loc = locator_from_role_and_name("", "")
        assert "WARNING" in loc

    def test_spinbutton_numeric_name_dropped(self):
        """Spinbutton current value like '1' should not be used as name."""
        loc = locator_from_role_and_name("spinbutton", "1")
        assert loc == "page.getByRole('spinbutton')"

    def test_spinbutton_quoted_numeric_dropped(self):
        loc = locator_from_role_and_name("spinbutton", '"1"')
        assert loc == "page.getByRole('spinbutton')"

    def test_textbox_dedup_required_marker(self):
        """'Email* Email' → getByRole('textbox', { name: 'Email' })"""
        loc = locator_from_role_and_name("textbox", "Email* Email")
        assert loc == "page.getByRole('textbox', { name: 'Email' })"

    def test_textbox_required_marker_different_parts(self):
        """'Password* Password' → getByRole('textbox', { name: 'Password' })"""
        loc = locator_from_role_and_name("textbox", "Password* Password")
        assert loc == "page.getByRole('textbox', { name: 'Password' })"


# ── Action Summary Builder ─────────────────────────────────────


class TestBuildActionSummary:
    def test_navigate_and_click(self):
        log = ActionLog()
        log.record("navigate", url="https://example.com")
        log.record("click", ref="e14", element_role="button", element_name="Add to cart")

        ref_map = SnapshotRefMap()
        # Note: ref_map is intentionally empty — locators are built from
        # the role+name already stored in the ActionRecord, not from ref lookup.
        summary = _build_action_summary(log, ref_map)
        assert "Navigate to: https://example.com" in summary
        assert "Click: button 'Add to cart'" in summary
        assert "getByRole('button'" in summary

    def test_fill_action(self):
        log = ActionLog()
        log.record("fill", ref="e22", element_role="textbox", element_name="Email", value="test@test.com")

        ref_map = SnapshotRefMap()
        summary = _build_action_summary(log, ref_map)
        assert "Fill: textbox 'Email'" in summary
        assert "test@test.com" in summary
        assert "getByRole('textbox'" in summary

    def test_db_verify_action(self):
        log = ActionLog()
        log.record(
            "db_verify",
            check_name="order exists",
            query="SELECT 1 FROM orders WHERE id=1",
            expected="1 row",
            actual="1 row",
            status="pass",
        )

        ref_map = SnapshotRefMap()
        summary = _build_action_summary(log, ref_map)
        assert "DB Check: order exists [pass]" in summary
        assert "SELECT 1 FROM orders" in summary

    def test_press_key_and_wait(self):
        log = ActionLog()
        log.record("press_key", value="Enter")
        log.record("wait", value="text=Success")

        ref_map = SnapshotRefMap()
        summary = _build_action_summary(log, ref_map)
        assert "Press key: Enter" in summary
        assert "Wait: text=Success" in summary


# ── Hooks action recording integration ─────────────────────────

class _DummyDetector:
    def record(self, tool_name: str, tool_args: str) -> None:
        pass

    def update_last_result(self, result_hash: str) -> None:
        pass

    def is_stuck(self) -> bool:
        return False


class _DummyEvidenceCollector:
    def collect(self, tool_name: str, tool_args: str, result: str) -> None:
        pass

    def build_summary(self) -> str:
        return ""


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


def _make_hooks():
    from universal_debug_agent.orchestrator.hooks import InvestigationHooks
    return InvestigationHooks(
        stuck_detector=_DummyDetector(),
        evidence_collector=_DummyEvidenceCollector(),
    )


class TestHooksActionRecording:
    @pytest.mark.asyncio
    async def test_navigate_recorded(self):
        hooks = _make_hooks()
        args = json.dumps({"url": "https://example.com/products"})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_navigate"))

        assert len(hooks.action_log) == 1
        rec = hooks.action_log.records[0]
        assert rec.action_type == "navigate"
        assert rec.url == "https://example.com/products"

    @pytest.mark.asyncio
    async def test_click_with_ref_resolution(self):
        hooks = _make_hooks()

        # First, populate ref_map from a snapshot
        hooks.ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)

        args = json.dumps({"ref": "e14", "element": "Add to cart button"})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))

        assert len(hooks.action_log) == 1
        rec = hooks.action_log.records[0]
        assert rec.action_type == "click"
        assert rec.ref == "e14"
        assert rec.element_role == "button"
        assert rec.element_name == "Add to cart"

    @pytest.mark.asyncio
    async def test_click_without_ref_map_uses_element_desc(self):
        hooks = _make_hooks()
        # Don't populate ref_map — should fall back to element description

        args = json.dumps({"ref": "e99", "element": "Some button"})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_click"))

        rec = hooks.action_log.records[0]
        assert rec.element_name == "Some button"
        assert rec.element_role == ""  # not resolved

    @pytest.mark.asyncio
    async def test_type_recorded_with_value(self):
        hooks = _make_hooks()
        hooks.ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)

        args = json.dumps({"ref": "e22", "text": "user@example.com"})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_type"))

        rec = hooks.action_log.records[0]
        assert rec.action_type == "type"
        assert rec.element_role == "textbox"
        assert rec.element_name == "Email"
        assert rec.value == "user@example.com"

    @pytest.mark.asyncio
    async def test_fill_form_expands_fields(self):
        """browser_fill_form with fields array should produce one record per field."""
        hooks = _make_hooks()
        hooks.ref_map.update_from_snapshot(SAMPLE_SNAPSHOT)

        args = json.dumps({
            "fields": [
                {"name": "Email", "type": "textbox", "ref": "e22", "value": "a@b.com"},
                {"name": "Password", "type": "textbox", "ref": "e23", "value": "secret"},
            ]
        })
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_fill_form"))

        assert len(hooks.action_log) == 2
        rec0 = hooks.action_log.records[0]
        assert rec0.action_type == "fill"
        assert rec0.element_name == "Email"
        assert rec0.value == "a@b.com"

        rec1 = hooks.action_log.records[1]
        assert rec1.action_type == "fill"
        assert rec1.element_name == "Password"
        assert rec1.value == "secret"

    @pytest.mark.asyncio
    async def test_select_option_recorded(self):
        hooks = _make_hooks()
        args = json.dumps({"ref": "e5", "values": ["bank_transfer"]})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_select_option"))

        rec = hooks.action_log.records[0]
        assert rec.action_type == "select"
        assert rec.value == "bank_transfer"

    @pytest.mark.asyncio
    async def test_press_key_recorded(self):
        hooks = _make_hooks()
        args = json.dumps({"key": "ArrowDown"})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_press_key"))

        rec = hooks.action_log.records[0]
        assert rec.action_type == "press_key"
        assert rec.value == "ArrowDown"

    @pytest.mark.asyncio
    async def test_snapshot_ignored_in_action_log(self):
        """browser_snapshot should NOT be recorded as an action."""
        hooks = _make_hooks()
        args = json.dumps({"depth": 10})
        context = _DummyToolContext(args)

        await hooks.on_tool_start(context, agent=None, tool=_DummyTool("browser_snapshot"))

        assert len(hooks.action_log) == 0

    @pytest.mark.asyncio
    async def test_ref_map_updated_from_tool_result(self):
        hooks = _make_hooks()

        # Simulate on_tool_end with a snapshot result containing refs
        snapshot_result = """\
- Page URL: https://example.com
- button [ref=e50]: Buy Now
- textbox [ref=e51]: Quantity
"""
        await hooks.on_tool_end(
            context=_DummyToolContext("{}"),
            agent=None,
            tool=_DummyTool("browser_snapshot"),
            result=snapshot_result,
        )

        assert hooks.ref_map.get("e50") is not None
        assert hooks.ref_map.get("e50").name == "Buy Now"

    @pytest.mark.asyncio
    async def test_db_verify_result_recorded(self):
        hooks = _make_hooks()

        verifications = json.dumps([
            {
                "check_name": "order exists",
                "query": "SELECT * FROM orders WHERE id=42",
                "expected": "1 row",
                "actual": "1 row",
                "status": "pass",
                "severity": "high",
            },
            {
                "check_name": "total correct",
                "query": "SELECT total FROM orders WHERE id=42",
                "expected": "268.45",
                "actual": "268.45",
                "status": "pass",
                "severity": "high",
            },
        ])

        await hooks.on_tool_end(
            context=_DummyToolContext("{}"),
            agent=None,
            tool=_DummyTool("verify_in_db"),
            result=verifications,
        )

        db_records = [r for r in hooks.action_log.records if r.action_type == "db_verify"]
        assert len(db_records) == 2
        assert db_records[0].check_name == "order exists"
        assert db_records[0].query == "SELECT * FROM orders WHERE id=42"
        assert db_records[1].check_name == "total correct"
