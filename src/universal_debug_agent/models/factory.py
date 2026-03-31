"""Model factory — creates the right LLM model based on profile config.

Supports:
- openai: Native OpenAI models (gpt-4o, gpt-4o-mini, etc.)
- gemini: Google Gemini via OpenAI-compatible endpoint
- Any OpenAI-compatible API via custom base_url
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from openai import AsyncOpenAI

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from universal_debug_agent.schemas.profile import ModelConfig

logger = logging.getLogger(__name__)

# Known provider base URLs
_PROVIDER_BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "deepseek": "https://api.deepseek.com",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

# Default models per provider
_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "gemini": "gemini-2.0-flash",
    "deepseek": "deepseek-chat",
    "groq": "llama-3.3-70b-versatile",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "openrouter": "openai/gpt-4o",
}

# Providers that need OpenAI-incompatible fields stripped
_STRIP_STRICT_PROVIDERS = {"gemini", "deepseek", "groq", "together"}

# Fields to remove from the top-level request body
_UNSUPPORTED_TOP_LEVEL = {
    "parallel_tool_calls", "store", "reasoning_effort",
    "verbosity", "top_logprobs", "prompt_cache_retention",
}


class _CompatTransport(httpx.AsyncBaseTransport):
    """httpx transport wrapper that strips OpenAI-specific fields from request JSON."""

    def __init__(self, transport: httpx.AsyncBaseTransport):
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.content:
            try:
                body = json.loads(request.content)
                modified = False

                # Strip unsupported top-level params
                for key in _UNSUPPORTED_TOP_LEVEL:
                    if key in body:
                        del body[key]
                        modified = True

                # Strip 'strict' from tool definitions
                if "tools" in body and isinstance(body["tools"], list):
                    for tool in body["tools"]:
                        if isinstance(tool, dict) and "function" in tool:
                            if "strict" in tool["function"]:
                                del tool["function"]["strict"]
                                modified = True

                # Strip 'strict' from response_format.json_schema
                rf = body.get("response_format")
                if isinstance(rf, dict) and "json_schema" in rf:
                    if "strict" in rf["json_schema"]:
                        del rf["json_schema"]["strict"]
                        modified = True

                if modified:
                    new_content = json.dumps(body).encode()
                    # Drop the stale Content-Length so httpx recalculates it for the rewritten body.
                    headers = request.headers.copy()
                    headers.pop("Content-Length", None)
                    request = httpx.Request(
                        method=request.method,
                        url=request.url,
                        headers=headers,
                        content=new_content,
                    )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass  # Not JSON, pass through

        return await self._transport.handle_async_request(request)


def create_model(config: ModelConfig) -> str | OpenAIChatCompletionsModel:
    """Create a model instance from config."""
    provider = config.provider
    model_name = config.model_name or _PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o")

    # Native OpenAI — return model name string; SDK client uses default max_retries=2
    # which already handles transient 429s internally without restarting the run.
    if provider == "openai" and not config.base_url:
        return model_name

    # Resolve API key
    api_key = _resolve_api_key(config)

    # Resolve base URL
    base_url = config.base_url or _PROVIDER_BASE_URLS.get(provider)
    if not base_url:
        raise ValueError(
            f"Unknown provider '{provider}' and no base_url specified. "
            f"Known providers: {', '.join(_PROVIDER_BASE_URLS.keys())}"
        )

    needs_compat = provider in _STRIP_STRICT_PROVIDERS or config.base_url

    if needs_compat:
        # Use custom transport to strip unsupported fields at HTTP level
        transport = _CompatTransport(httpx.AsyncHTTPTransport())
        http_client = httpx.AsyncClient(transport=transport)
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
            max_retries=5,
        )
    else:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=5,
        )

    return OpenAIChatCompletionsModel(
        model=model_name,
        openai_client=client,
    )


def _resolve_api_key(config: ModelConfig) -> str:
    """Resolve the API key from config or environment."""
    if config.api_key_env:
        key = os.environ.get(config.api_key_env, "")
        if not key:
            raise ValueError(
                f"Environment variable '{config.api_key_env}' is not set. "
                f"Set it with: export {config.api_key_env}=your-api-key"
            )
        return key

    # Fallback: try common env var names per provider
    fallback_env_vars: dict[str, str] = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
        "together": "TOGETHER_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }

    env_var = fallback_env_vars.get(config.provider, "")
    if env_var:
        key = os.environ.get(env_var, "")
        if key:
            return key

    raise ValueError(
        f"No API key found for provider '{config.provider}'. "
        f"Set api_key_env in your profile, or export {fallback_env_vars.get(config.provider, 'YOUR_API_KEY')}"
    )
