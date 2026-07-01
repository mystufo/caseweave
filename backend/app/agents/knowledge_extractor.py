"""KnowledgeExtractor: 从产品文档中抽取可沉淀的知识条目（产品规则/约束/术语等）。

设计原则：
- 抽到什么算什么 —— 这是"积累"层，目的是让未来同模块的用例生成能命中这些规则
- 输出限定在固定的几个 type，方便后续 prompt 注入时挑选
- 失败 / 空结果 / 解析异常都返回空列表（不抛出），不能阻塞上传主流程
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_factory import build_chat_model
from app.config import get_settings
from app.knowledge.store import KnowledgeDraft

logger = logging.getLogger("testcraft.knowledge_extractor")

VALID_TYPES = {"product_rule", "constraint", "term", "module_relation", "ui_behavior"}

SYSTEM_PROMPT = """你是一名资深产品分析师，正在阅读一份产品需求文档，目标是提取**可沉淀、可复用**的产品知识条目，
存入项目知识库以辅助未来的测试用例生成。

请只抽取以下五类有长期价值的知识：
- product_rule：明确的业务规则（如 "VIP 用户购物可享 8 折"）
- constraint：边界与约束（如 "用户名长度 4–20 字符"、"单次下单最多 99 件"）
- term：领域术语定义（如 "白名单：经审核后可绕过风控的账号集合"）
- module_relation：与其他模块的关联（如 "下单成功后会触发库存模块扣减"）
- ui_behavior：明确写出的 UI/交互行为（如 "未登录点击购买跳转到登录页"）

抽取要点：
1. 每条 content 必须自包含可单独阅读，不要写成「见上文」「同前」
2. 不要总结/缩写文档大意；只保留**可以被未来用例引用的具体规则**
3. 同一规则不要重复发出，保持去重
4. 不确定 / 模糊不清 / 仅猜测的，不要写
5. 控制总数：一份文档最多输出 12 条，宁缺勿滥
6. confidence 取值范围 0.4–0.9：文档明确直述用 0.8+，需要推断的用 0.5–0.6

输出格式（必须是 JSON 数组，仅此而已，不要解释）：
[
  {"knowledge_type": "product_rule", "content": "...", "confidence": 0.8},
  {"knowledge_type": "constraint",  "content": "...", "confidence": 0.7}
]

如果文档中没有可沉淀的具体规则，输出空数组 []。
"""


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        # 去掉 ```json ... ``` 或 ``` ... ```
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse(raw: str) -> list[KnowledgeDraft]:
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        logger.warning("knowledge extractor JSON parse failed: %s; head=%s", e, raw[:200])
        return []

    if not isinstance(data, list):
        logger.warning("knowledge extractor returned non-array, got %s", type(data).__name__)
        return []

    out: list[KnowledgeDraft] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        t = str(item.get("knowledge_type") or "").strip()
        c = str(item.get("content") or "").strip()
        if t not in VALID_TYPES or not c:
            continue
        # 去重 key 取 type+content（首 80 字够区分）
        key = f"{t}|{c[:80]}"
        if key in seen:
            continue
        seen.add(key)
        try:
            conf = float(item.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        conf = max(0.1, min(0.95, conf))
        out.append(KnowledgeDraft(knowledge_type=t, content=c, source="document", confidence=conf))
    return out[:12]


async def extract_knowledge(
    doc_content: str,
    module_name: str | None = None,
) -> list[KnowledgeDraft]:
    """Best-effort knowledge extraction. Returns [] on any failure."""
    if not (doc_content or "").strip():
        return []

    # max_tokens 由 .env 的 KNOWLEDGE_MAX_TOKENS 控制（默认 8192）。
    # 部分模型（如 kimi-k2 思考版）会先消耗大量 reasoning_tokens，预算太小
    # 会导致正文被截断为空（finish_reason=length），不要随意往下调。
    llm = build_chat_model(max_tokens=get_settings().knowledge_max_tokens, temperature=0)
    user_content = f"产品模块：{module_name or '未指定'}\n\n需求文档内容：\n{doc_content}"

    logger.info(
        "knowledge extractor LLM call | module=%s doc_chars=%d",
        module_name or "未指定", len(doc_content),
    )
    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:
        logger.warning("knowledge extractor LLM call failed: %s", e)
        return []

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_content: Any = resp.content
    raw = raw_content if isinstance(raw_content, str) else str(raw_content)
    drafts = _parse(raw)
    logger.info(
        "knowledge extractor done | extracted=%d raw_chars=%d (%.0fms)",
        len(drafts), len(raw), elapsed_ms,
    )
    return drafts
