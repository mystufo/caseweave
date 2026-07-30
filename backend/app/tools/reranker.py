"""检索精排（rerank）：给召回候选逐条打相关性分，让排序比单向量召回更准。

单向量召回（bi-encoder）把 query 和知识各自压成一个向量，丢失了双向语义交互；
精排阶段把 query 与每条候选放到一起判定「这条知识与查询有多相关」，把召回阶段
排不准的地方修好——这正是修「长文档 query vs 单句知识」不对称问题的关键一环。

三种 provider（settings.rerank_provider），统一 fail-open：开关关 / 未配置 / 服务
未就绪 / 出错 → 返回 None，调用方（store.search_relevant）降级回纯向量检索，绝不阻塞。
- llm（默认）：复用主 LLM（build_chat_model，即 llm_* 凭证）**一次调用**给整池候选打分，
  不需要新服务/新鉴权。准确性略逊 cross-encoder，但零额外部署，契合「模型都走远程」的现状。
- local：随 docker-compose 起的本地 TEI cross-encoder 容器（BAAI/bge-reranker-v2-m3），
  走 http://reranker:80/rerank，无需 key。
- openai：远程兼容 /rerank 接口（TEI 格式），需 rerank_api_key。

统一契约：rerank(query, docs) -> Optional[list[float]]，返回**与 docs 顺序对齐**的
分数列表，分数越大越相关（llm 与 TEI raw_scores=false 都归一到 [0,1]）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_BASE = "http://reranker:80"          # docker-compose 里的 TEI reranker 服务
_DEFAULT_REMOTE_BASE = "https://api.openai.com"     # 远程兼容接口占位（一般会显式配 base_url）


async def rerank(query: str, docs: list[str]) -> Optional[list[float]]:
    """给 query 对每条 doc 打相关性分，返回**与 docs 顺序对齐**的分数列表（越大越相关）。

    None 是合法返回，表示 rerank 不可用（开关关 / 未配置 / 服务没起 / 出错），
    调用方应降级为不精排。docs 为空返回 []。
    """
    s = get_settings()
    if not s.rerank_enabled:
        return None
    if not docs:
        return []
    query = (query or "").strip()
    if not query:
        return None

    provider = (s.rerank_provider or "llm").lower()
    if provider == "llm":
        return await _rerank_llm(query, docs, s)
    if provider in ("local", "openai"):
        return await _rerank_tei(query, docs, s, provider)
    logger.warning("rerank provider %r not supported, skipping", s.rerank_provider)
    return None


# ── LLM 打分（默认，复用主模型）──────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = (
    "你是知识检索的相关性打分器。给定一个查询（通常是一段需求文档正文）和若干候选知识条目，"
    "为每条候选打一个 0~1 的相关性分数：1 表示该条目与查询高度相关（讲的是同一个功能/规则/对象），"
    "0 表示完全无关。只依据语义相关性，不要因为条目本身写得好就给高分。\n"
    "严格只输出一个 JSON 对象：{\"scores\": [每条候选的分数, ...]}，"
    "数组长度必须与候选数量完全一致、顺序一一对应，不要输出任何解释或多余文字。"
)


async def _rerank_llm(query: str, docs: list[str], s) -> Optional[list[float]]:
    """复用主 LLM 一次调用给所有候选打分。解析失败/数量对不上 → None（降级）。"""
    # 延迟 import，避免 tools 层在无 LLM 依赖的场景（如脚本）被牵连
    from langchain_core.messages import HumanMessage, SystemMessage
    from app.agents.llm_factory import build_chat_model

    numbered = "\n".join(f"[{i}] {d}" for i, d in enumerate(docs))
    user_content = f"查询：\n{query}\n\n候选知识条目（共 {len(docs)} 条）：\n{numbered}"
    # 思考模型的 max_tokens 含 reasoning_tokens，给小会先烧完思考然后正文为空 → 用 knowledge_max_tokens(≥8192)
    llm = build_chat_model(max_tokens=s.knowledge_max_tokens, temperature=0)

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=_LLM_SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("rerank[llm] call failed: %s", e)
        return None

    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    scores = _parse_llm_scores(raw, len(docs))
    if scores is None:
        logger.warning("rerank[llm] parse failed or count mismatch, skipping | raw=%.120s", raw)
        return None
    return scores


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse_llm_scores(raw: str, n: int) -> Optional[list[float]]:
    """从 LLM 输出里抠出 scores 数组，长度须等于 n，值 clamp 到 [0,1]。"""
    try:
        data = json.loads(_strip_code_fence(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    arr = data.get("scores") if isinstance(data, dict) else None
    if not isinstance(arr, list) or len(arr) != n:
        return None
    out: list[float] = []
    for v in arr:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        out.append(max(0.0, min(1.0, f)))
    return out


# ── TEI cross-encoder（local / 远程兼容）─────────────────────────────────────
# TEI /rerank 契约：请求 {"query", "texts", "raw_scores": false} → 响应 [{"index","score"}]
# （raw_scores=false 时 score∈[0,1]；顺序不保证，靠 index 对回输入位置）。

def _resolve_base_url(s, provider: str) -> str:
    base = (s.rerank_base_url or "").rstrip("/")
    if not base:
        return _DEFAULT_LOCAL_BASE if provider == "local" else _DEFAULT_REMOTE_BASE
    if base.endswith("/rerank"):
        base = base[: -len("/rerank")]
    return base


async def _rerank_tei(query: str, docs: list[str], s, provider: str) -> Optional[list[float]]:
    if provider == "openai" and not s.rerank_api_key:
        logger.info("rerank[tei] skipped: no rerank_api_key configured")
        return None

    url = f"{_resolve_base_url(s, provider)}/rerank"
    headers = {"Content-Type": "application/json"}
    if s.rerank_api_key:
        headers["Authorization"] = f"Bearer {s.rerank_api_key}"
    payload = {"query": query, "texts": docs, "raw_scores": False}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning("rerank[tei] call failed: %s", e)
        return None

    if not isinstance(data, list):
        logger.warning("rerank[tei] unexpected response type: %s", type(data).__name__)
        return None
    scores: list[Optional[float]] = [None] * len(docs)
    try:
        for item in data:
            idx = item["index"]
            if 0 <= idx < len(docs):
                scores[idx] = float(item["score"])
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("rerank[tei] response parse failed: %s", e)
        return None
    if any(sc is None for sc in scores):
        logger.warning(
            "rerank[tei] returned %d scores for %d docs (incomplete), skipping",
            sum(sc is not None for sc in scores), len(docs),
        )
        return None
    return [float(sc) for sc in scores]  # type: ignore[arg-type]
