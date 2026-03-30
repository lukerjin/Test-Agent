"""Tests for CLI error formatting."""

import httpx

from openai import APIStatusError, RateLimitError

from universal_debug_agent.main import _extract_retry_delay, _format_api_error


def test_extract_retry_delay_from_gemini_quota_response():
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.com"),
        json=[{
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "31s",
                    }
                ]
            }
        }],
    )
    error = RateLimitError("quota exceeded", response=response, body=response.json())

    assert _extract_retry_delay(error) == "31s"


def test_format_rate_limit_error_includes_actionable_guidance():
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.com"),
        json=[{
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "31s",
                    }
                ]
            }
        }],
    )
    error = RateLimitError("quota exceeded", response=response, body=response.json())

    message = _format_api_error(error, "gemini")

    assert "gemini API 返回 429" in message
    assert "Google AI Studio / GCP" in message
    assert "31s" in message


def test_format_api_status_error_includes_status_code():
    response = httpx.Response(
        500,
        request=httpx.Request("POST", "https://example.com"),
        json={"error": "server exploded"},
    )
    error = APIStatusError("server exploded", response=response, body=response.json())

    message = _format_api_error(error, "gemini")

    assert "HTTP 500" in message
