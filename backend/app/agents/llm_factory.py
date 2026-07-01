"""LLM factory: returns a chat model based on LLM_PROVIDER.

Supported providers:
- anthropic: official Anthropic API (or compatible base_url)
- openai:    OpenAI-compatible APIs (火山方舟 / DeepSeek / 阿里百炼 / OpenRouter / etc.)
"""
from typing import Any
from langchain_core.language_models import BaseChatModel
from app.config import get_settings

settings = get_settings()


def build_chat_model(*, max_tokens: int = 4096, temperature: float = 0.2) -> BaseChatModel:
    """Build a LangChain chat model based on the configured provider."""
    provider = (settings.llm_provider or "anthropic").lower()

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatAnthropic(**kwargs)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        kwargs = {
            "model": settings.llm_model,
            "api_key": settings.llm_api_key,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(
        f"Unsupported LLM_PROVIDER: {provider!r}. Use 'anthropic' or 'openai'."
    )
