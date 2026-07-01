"""OpenAI-compatible embedding HTTP client.

Phase 3 用于把知识条目向量化后写入 pgvector。所有调用走标准的
`POST {base_url}/embeddings` 端点（兼容 OpenAI / 火山方舟 / 智谱等），
所以不引入额外 SDK。

支持两种 mode（settings.embedding_mode）：
- standard：OpenAI 风格 —— input 是字符串数组，一次返回多条向量
- multimodal：火山方舟多模态 embedding —— 路径 /embeddings/multimodal，
  input 是 [{"type":"text","text":...}] 对象数组，一次只接受一条返回单条向量。
  批量调用方需自行 fan-out。

设计要点：
- 配置缺失（无 api_key）时 `embed_texts` 返回 None，调用方需做 None 检查并优雅降级
  （知识入库照常写 content，仅跳过 embedding 字段；搜索退化为按 confidence/时间排序）
- 失败只 log+返回 None，不抛出，避免后台抽取任务把请求线吞了
- 自动按 settings.embedding_dim 校验维度，维度对不上视为配置错误
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.openai.com/v1"


def _resolve_base_url() -> str:
    s = get_settings()
    base = (s.embedding_base_url or "").rstrip("/")
    if not base:
        return _DEFAULT_BASE
    # 容错：允许用户填到 /v1 或不填，统一去掉尾部 /embeddings(/multimodal) 防重复
    for suffix in ("/embeddings/multimodal", "/embeddings"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


async def _embed_standard(texts: list[str], s) -> Optional[list[list[float]]]:
    url = f"{_resolve_base_url()}/embeddings"
    headers = {
        "Authorization": f"Bearer {s.embedding_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": s.embedding_model, "input": texts}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("embedding call failed: %s", e)
        return None

    items = data.get("data") or []
    if len(items) != len(texts):
        logger.warning("embedding count mismatch: got %d for %d inputs", len(items), len(texts))
        return None

    out: list[list[float]] = []
    for it in items:
        vec = it.get("embedding")
        if not isinstance(vec, list) or len(vec) != s.embedding_dim:
            logger.warning(
                "embedding dim mismatch: expected %d, got %s — check EMBEDDING_DIM/EMBEDDING_MODEL",
                s.embedding_dim, len(vec) if isinstance(vec, list) else "?",
            )
            return None
        out.append([float(x) for x in vec])
    return out


async def _embed_multimodal_one(text: str, s, client: httpx.AsyncClient) -> Optional[list[float]]:
    """多模态接入点：一次一条，input 必须是 [{type,text}] 对象数组，data 返回单 dict。"""
    url = f"{_resolve_base_url()}/embeddings/multimodal"
    headers = {
        "Authorization": f"Bearer {s.embedding_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": s.embedding_model, "input": [{"type": "text", "text": text}]}
    try:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("multimodal embedding call failed: %s", e)
        return None

    block = data.get("data")
    # 火山方舟多模态实测：data 是 dict，含 embedding 字段
    vec = (block or {}).get("embedding") if isinstance(block, dict) else None
    if not isinstance(vec, list) or len(vec) != s.embedding_dim:
        logger.warning(
            "multimodal embedding dim mismatch: expected %d, got %s",
            s.embedding_dim, len(vec) if isinstance(vec, list) else "?",
        )
        return None
    return [float(x) for x in vec]


async def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Return one vector per input text, or None if embeddings are disabled / failed.

    None 是一种合法返回 —— 表示 embedding 功能不可用，调用方自己决定怎么降级。
    """
    if not texts:
        return []

    s = get_settings()
    if not s.embedding_api_key:
        logger.info("embedding skipped: no embedding_api_key configured")
        return None

    if (s.embedding_provider or "openai").lower() != "openai":
        logger.warning("embedding provider %r not supported, skipping", s.embedding_provider)
        return None

    mode = (s.embedding_mode or "standard").lower()
    if mode == "multimodal":
        # 多模态接入点不支持批量 —— 逐条调，复用同一个 httpx client 省 TLS 握手
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                out: list[list[float]] = []
                for t in texts:
                    vec = await _embed_multimodal_one(t, s, client)
                    if vec is None:
                        return None  # 任意一条挂掉整批作废，保持 store 路径简单
                    out.append(vec)
                return out
        except Exception as e:
            logger.warning("multimodal embedding batch failed: %s", e)
            return None

    return await _embed_standard(texts, s)


async def embed_one(text: str) -> Optional[list[float]]:
    out = await embed_texts([text])
    return out[0] if out else None
