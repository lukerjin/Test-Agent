"""Model factory — creates the right LLM model based on profile config.

Supports:
- openai: Native OpenAI models (gpt-4o, gpt-4o-mini, etc.)
- gemini: Google Gemini via OpenAI-compatible endpoint
- Any OpenAI-compatible API via custom base_url
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from universal_debug_agent.schemas.profile import ModelConfig


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


def create_model(config: ModelConfig) -> str | OpenAIChatCompletionsModel:
    """Create a model instance from config.

    Returns:
        - A plain string model name for native OpenAI (the SDK handles it)
        - An OpenAIChatCompletionsModel for other providers
    """
    provider = config.provider
    model_name = config.model_name or _PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o")

    # Native OpenAI — just return the model name string
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

    # Create an AsyncOpenAI client pointing at the provider's endpoint
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
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
