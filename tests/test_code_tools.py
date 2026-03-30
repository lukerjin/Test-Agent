"""Tests for compact code search behavior."""

from __future__ import annotations

from universal_debug_agent.tools.code_tools import _format_grep_discovery


def test_grep_code_returns_compact_file_summary():
    output = _format_grep_discovery(
        [
            "/repo/orders.py:1:class Order:",
            "/repo/payments.py:2:order_id = 1",
        ],
        pattern="Order|order_id",
        directory="",
        file_glob="*.py",
        root_dir="/repo",
    )

    assert "# grep_code discovery summary" in output
    assert "matched_files: 2" in output
    assert "- orders.py (1 matches)" in output
    assert "- payments.py (1 matches)" in output


def test_grep_code_caps_large_match_sets():
    output = _format_grep_discovery(
        [
            f"/repo/file_{idx}.py:1:value = 'order_{idx}'"
            for idx in range(12)
        ],
        pattern="order",
        directory="",
        file_glob="*.py",
        root_dir="/repo",
    )

    assert "matched_files: 12" in output
    assert "showing top 8" in output
    assert "Narrow directory or file_glob" in output
