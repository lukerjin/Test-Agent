"""Tests for model factory."""

import json
import os
from unittest.mock import patch

import pytest
import httpx

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from universal_debug_agent.models.factory import _CompatTransport, create_model
from universal_debug_agent.schemas.profile import ModelConfig


def test_openai_returns_string():
    config = ModelConfig(provider="openai", model_name="gpt-4o")
    result = create_model(config)
    assert result == "gpt-4o"


def test_openai_default_model():
    config = ModelConfig(provider="openai")
    result = create_model(config)
    assert result == "gpt-4o"


def test_gemini_returns_model_instance():
    config = ModelConfig(provider="gemini", model_name="gemini-2.0-flash")
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
        result = create_model(config)
    assert isinstance(result, OpenAIChatCompletionsModel)


def test_gemini_default_model():
    config = ModelConfig(provider="gemini")
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
        result = create_model(config)
    assert isinstance(result, OpenAIChatCompletionsModel)


def test_custom_base_url():
    config = ModelConfig(
        provider="custom",
        model_name="my-model",
        base_url="https://my-api.example.com/v1",
        api_key_env="MY_KEY",
    )
    with patch.dict(os.environ, {"MY_KEY": "test-key"}):
        result = create_model(config)
    assert isinstance(result, OpenAIChatCompletionsModel)


def test_missing_api_key_raises():
    config = ModelConfig(provider="gemini")
    with patch.dict(os.environ, {}, clear=True):
        # Remove any GEMINI_API_KEY that might exist
        os.environ.pop("GEMINI_API_KEY", None)
        with pytest.raises(ValueError, match="No API key found"):
            create_model(config)


def test_explicit_api_key_env():
    config = ModelConfig(
        provider="gemini",
        model_name="gemini-2.0-flash",
        api_key_env="MY_CUSTOM_KEY",
    )
    with patch.dict(os.environ, {"MY_CUSTOM_KEY": "my-secret"}):
        result = create_model(config)
    assert isinstance(result, OpenAIChatCompletionsModel)


def test_deepseek_provider():
    config = ModelConfig(provider="deepseek")
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
        result = create_model(config)
    assert isinstance(result, OpenAIChatCompletionsModel)


def test_profile_model_defaults():
    """ModelConfig has sensible defaults when not specified in YAML."""
    config = ModelConfig()
    assert config.provider == "openai"
    assert config.model_name is None
    assert config.api_key_env is None
    assert config.base_url is None


@pytest.mark.asyncio
async def test_compat_transport_rewrites_content_length():
    captured_request = None

    class DummyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal captured_request
            captured_request = request
            return httpx.Response(200, json={"ok": True}, request=request)

    original_body = {
        "model": "gemini-2.0-flash",
        "parallel_tool_calls": True,
        "tools": [{"type": "function", "function": {"name": "noop", "strict": True}}],
    }
    original_content = json.dumps(original_body).encode()
    request = httpx.Request(
        "POST",
        "https://example.com/chat/completions",
        headers={"Content-Length": str(len(original_content))},
        content=original_content,
    )

    transport = _CompatTransport(DummyTransport())
    await transport.handle_async_request(request)

    assert captured_request is not None

    rewritten_body = json.loads(captured_request.content)
    assert "parallel_tool_calls" not in rewritten_body
    assert rewritten_body["tools"][0]["function"].get("strict") is None
    assert captured_request.headers["Content-Length"] == str(len(captured_request.content))
