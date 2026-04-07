"""Tests for form data capture merge in db_tool._merge_form_captures."""

from __future__ import annotations

from universal_debug_agent.tools import db_tool
from universal_debug_agent.tools.db_tool import _merge_form_captures


def setup_function():
    """Reset module state before each test."""
    db_tool.clear_captured_form_data()
    db_tool._allowed_domains = []


def teardown_function():
    db_tool.clear_captured_form_data()
    db_tool._allowed_domains = []


def test_form_data_in_network_log():
    """Form capture appears in output when XHR mutations are empty."""
    db_tool.record_form_capture({
        "action": "https://example.test/newsletter",
        "method": "POST",
        "fields": {"email": "test@test.com", "subscribed": "1"},
    })

    result = _merge_form_captures("", [])
    assert "[POST] https://example.test/newsletter => [form submit]" in result
    assert "Request body: email=test@test.com&subscribed=1" in result


def test_form_data_merged_with_xhr():
    """Both XHR mutations and form captures appear in output."""
    db_tool.record_form_capture({
        "action": "https://example.test/newsletter",
        "method": "POST",
        "fields": {"email": "test@test.com"},
    })

    xhr_line = "[POST] https://example.test/api/cart => [200] OK"
    result = _merge_form_captures(xhr_line, [xhr_line])

    assert "https://example.test/api/cart" in result
    assert "https://example.test/newsletter" in result


def test_form_data_dedup_with_xhr():
    """Form capture is skipped if same URL already in XHR mutations."""
    db_tool.record_form_capture({
        "action": "https://example.test/api/submit",
        "method": "POST",
        "fields": {"data": "value"},
    })

    xhr_line = "[POST] https://example.test/api/submit => [200] OK"
    result = _merge_form_captures(xhr_line, [xhr_line])

    # Should only appear once (from XHR), not duplicated by form capture
    assert result.count("https://example.test/api/submit") == 1


def test_domain_filter_on_form_data():
    """Form capture is excluded when action URL doesn't match allowed domains."""
    db_tool._allowed_domains = ["example.test"]
    db_tool.record_form_capture({
        "action": "https://evil.com/steal",
        "method": "POST",
        "fields": {"secret": "data"},
    })

    result = _merge_form_captures("", [])
    assert result == ""


def test_multiple_form_captures():
    """Multiple form submissions all appear in output."""
    db_tool.record_form_capture({
        "action": "https://example.test/login",
        "method": "POST",
        "fields": {"user": "admin"},
    })
    db_tool.record_form_capture({
        "action": "https://example.test/newsletter",
        "method": "POST",
        "fields": {"subscribed": "1"},
    })

    result = _merge_form_captures("", [])
    assert "https://example.test/login" in result
    assert "https://example.test/newsletter" in result


def test_clear_form_data():
    """clear_captured_form_data empties the list."""
    db_tool.record_form_capture({"action": "x", "method": "POST", "fields": {}})
    assert len(db_tool._captured_form_data) == 1
    db_tool.clear_captured_form_data()
    assert len(db_tool._captured_form_data) == 0
