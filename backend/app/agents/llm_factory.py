"""LLM factory: returns a chat model based on LLM_PROVIDER.

Supported providers:
- anthropic: official Anthropic API (or compatible base_url)
- openai:    OpenAI-compatible APIs (火山方舟 / DeepSeek / 阿里百炼 / OpenRouter / etc.)
"""
from typing import Any
from langchain_core.language_models import BaseChatModel
from app.config import get_settings
from app.usage import UsageRecorder

settings = get_settings()

# 全站唯一的 token 记账回调。挂在这里而不是各 agent 里，是为了保证「所有 LLM 调用都被
# 算进 daily_usage」这条不变量——新增 agent 只要走本工厂就自动记账，不会漏。
# 归属靠 app/usage.py 的 ContextVar，请求入口（app/limits.py 的依赖）负责设置。
_usage_recorder = UsageRecorder()


def _shared_kwargs() -> dict[str, Any]:
    return {
        "timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
        "callbacks": [_usage_recorder],
    }


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
            **_shared_kwargs(),
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
            # 流式调用默认不回 usage，要显式索要 stream_options.include_usage，否则
            # /chat、/generate 这些流式接口的 token 会漏记、配额形同虚设。
            # 个别兼容网关不认这个参数会直接报错，遇到就把 LLM_STREAM_USAGE 置 false。
            "stream_usage": settings.llm_stream_usage,
            **_shared_kwargs(),
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(
        f"Unsupported LLM_PROVIDER: {provider!r}. Use 'anthropic' or 'openai'."
    )


def build_vision_model(*, max_tokens: int | None = None, temperature: float = 0.0) -> BaseChatModel:
    """Build a multimodal chat model for image → text description.

    与 build_chat_model 同构，但读取 vision_* 配置，且每一项留空时逐项回退到对应的
    llm_*（provider/model/api_key/base_url）。这样：
    - 主模型本身就是视觉模型时，只置 VISION_ENABLED=true 即可（vision_* 全空 → 全部回退）。
    - 要用独立视觉模型时，单独填 VISION_* 覆盖对应项。

    返回的 ChatAnthropic / ChatOpenAI 都原生支持多模态 HumanMessage
    （content 传 list，含 {"type":"image_url", ...}）。
    """
    provider = (settings.vision_provider or settings.llm_provider or "anthropic").lower()
    model = settings.vision_model or settings.llm_model
    api_key = settings.vision_api_key or settings.llm_api_key
    base_url = settings.vision_base_url or settings.llm_base_url
    tokens = max_tokens if max_tokens is not None else settings.vision_max_tokens

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "max_tokens": tokens,
            "temperature": temperature,
            **_shared_kwargs(),
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatAnthropic(**kwargs)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        kwargs = {
            "model": model,
            "api_key": api_key,
            "max_tokens": tokens,
            "temperature": temperature,
            "stream_usage": settings.llm_stream_usage,
            **_shared_kwargs(),
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(
        f"Unsupported VISION_PROVIDER: {provider!r}. Use 'anthropic' or 'openai'."
    )
