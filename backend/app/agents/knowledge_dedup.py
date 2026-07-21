"""KnowledgeDedup: 判定新抽取的知识草稿与知识库已有条目的语义关系。

背景：抽取草稿时会先用 pgvector 粗筛出「主题相近」的候选旧条目（store.find_similar_entries），
但向量距离只能判「像不像」，判不了「矛不矛盾」——
  "用户名 4–20 字符" vs "6–16 字符"  → 距离很近，但**冲突**（不能同时为真）
  "用户名 4–20 字符" vs "长度需 4 到 20 位" → 距离也很近，但只是**相似**（同义可共存）
两者向量距离几乎一样，纯阈值分不出来。所以定性交给 LLM 做一次语义推理。

判定四类，核心是「先看是否同一对象的同一属性/维度，再看取值是否矛盾」：
  duplicate  同一对象同一属性，取值也完全相同，只是措辞不同
  similar    同一对象同一属性/维度、信息不矛盾，可同时成立（补充/细化）
  conflict   同一对象同一属性，取值/结论互相矛盾，不能同时成立
  unrelated  不是同一对象，或虽同一对象但讲的是不同属性/维度（奖励 vs 文案 vs 交互）

设计原则同 knowledge_extractor：fail-open——任何失败都返回空 dict，
调用方退化为「全部按 similar 展示」，绝不因判定失败而丢数据。
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

logger = logging.getLogger("testcraft.knowledge_dedup")

VALID_RELATIONS = {"duplicate", "similar", "conflict", "unrelated"}

SYSTEM_PROMPT = """你是一名严谨的知识库审校员。系统从产品文档里抽取了若干条「新知识草稿」，
每条草稿都附带了知识库中主题相近的若干条「已有条目」（通过语义检索粗筛得到）。
你的任务：判定**每一条草稿**与**它的每一条已有条目**之间的关系，并给出简短判定理由。

判定核心分两步：**先看是否在描述同一对象的同一属性/维度**，再看取值是否矛盾。
「同一对象」不等于「同一主题」——同一个任务/功能/对象往往有多个互不相干的维度
（奖励规则、文案、交互跳转、入口位置……），描述不同维度的两句话应判 unrelated。

四种关系：
- duplicate（完全重复）：**同一对象的同一属性**，取值也完全相同，只是措辞不同。例：
    草稿"用户名限 4 到 20 个字符" ↔ 已有"用户名长度为 4–20 字符" → duplicate
- similar（相似）：**同一对象的同一属性/规则/取值维度**，信息不矛盾，只是粒度或措辞不同、
  一方是另一方的补充或细化，可以同时成立。例：
    草稿"用户名 4–20 字符" ↔ 已有"用户名不能为空且有长度限制" → similar（都在讲用户名长度约束）
- conflict（冲突）：**同一对象的同一属性，但取值/结论互相矛盾**，不可能同时成立。例：
    草稿"用户名 4–20 字符" ↔ 已有"用户名 6–16 字符" → conflict
    草稿"解锁创作者画像任务奖励 100 积分" ↔ 已有"解锁创作者画像任务奖励 135 积分" → conflict
- unrelated（无关）：不是同一对象，**或虽是同一对象但讲的是不同属性/维度/环节**。例：
    草稿"用户名 4–20 字符" ↔ 已有"密码必须含大写字母" → unrelated（不同对象）
    草稿"解锁创作者画像任务奖励 100 积分" ↔ 已有"解锁创作者画像完成后 toast 文案为…" → unrelated（同任务，一个讲奖励一个讲文案）
    草稿"解锁创作者画像任务奖励 100 积分" ↔ 已有"点击【去解锁】打开用户画像调研弹窗" → unrelated（同任务，一个讲奖励一个讲点击交互）
    草稿"解锁创作者画像任务奖励 100 积分" ↔ 已有"新用户画像调研任务奖励 100 积分" → unrelated（不同任务，各有各的奖励）

判定要点：
1. 只比较两条内容陈述的事实本身，不要脑补文档外的背景。
2. **先判是否同一对象的同一属性/维度**：同名对象但属性/维度不同（奖励 vs 文案 vs 交互）→ unrelated；
   连对象都不同 → unrelated。只有确认「同一对象 + 同一属性」后，才继续判 duplicate/similar/conflict。
3. 同一属性下：数值/范围/枚举/开关等取值对不上 → conflict；取值一致或一方是另一方的子集/泛化 → duplicate 或 similar。
4. 拿不准是 similar 还是 conflict 时，若无明显矛盾，判 similar（更保守，不误报冲突）；
   拿不准是 similar 还是 unrelated 时，若不是同一属性，判 unrelated（避免把无关维度当成相似展示）。
5. reason 用一句中文说清依据，20 字左右，例如「长度上限不同，4-20 vs 6-16」或「同任务不同维度：奖励 vs 文案」。

输入是一个 JSON 数组，每个元素形如：
  {"draft_index": 0, "draft_type": "constraint", "draft_content": "...",
   "candidates": [{"entry_id": 12, "content": "..."}, ...]}

输出必须是 JSON 数组，仅此而已，不要解释、不要代码块标记，形如：
[
  {"draft_index": 0, "relations": [
     {"entry_id": 12, "relation": "conflict", "reason": "长度上限不同，4-20 vs 6-16"},
     {"entry_id": 34, "relation": "similar",  "reason": "同讲用户名长度，未矛盾"}
  ]}
]
每条草稿都要出现在输出里；某草稿所有候选都无关时 relations 给 []。"""


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse(raw: str) -> dict[int, dict[int, dict[str, str]]]:
    """解析 LLM 输出为 {draft_index: {entry_id: {"relation": str, "reason": str}}}。"""
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        logger.warning("knowledge dedup JSON parse failed: %s; head=%s", e, raw[:200])
        return {}
    if not isinstance(data, list):
        logger.warning("knowledge dedup returned non-array: %s", type(data).__name__)
        return {}

    out: dict[int, dict[int, dict[str, str]]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            di = int(item.get("draft_index"))
        except (TypeError, ValueError):
            continue
        rels = item.get("relations")
        if not isinstance(rels, list):
            continue
        by_entry: dict[int, dict[str, str]] = {}
        for r in rels:
            if not isinstance(r, dict):
                continue
            try:
                eid = int(r.get("entry_id"))
            except (TypeError, ValueError):
                continue
            rel = str(r.get("relation") or "").strip().lower()
            if rel not in VALID_RELATIONS:
                continue
            by_entry[eid] = {
                "relation": rel,
                "reason": str(r.get("reason") or "").strip(),
            }
        out[di] = by_entry
    return out


async def classify_relations(
    pairs: list[dict[str, Any]],
) -> dict[int, dict[int, dict[str, str]]]:
    """判定每条草稿与其候选旧条目的关系。

    入参 pairs：[{
        "draft_index": int,
        "draft_type": str,
        "draft_content": str,
        "candidates": [{"entry_id": int, "content": str}, ...],
    }, ...]  —— 只应传「有候选」的草稿。

    返回 {draft_index: {entry_id: {"relation": duplicate|similar|conflict|unrelated,
                                    "reason": str}}}。

    fail-open：无输入 / LLM 失败 / 解析失败 → 返回 {}，调用方自行退化。
    """
    pairs = [p for p in pairs if p.get("candidates")]
    if not pairs:
        return {}

    settings = get_settings()
    llm = build_chat_model(max_tokens=settings.knowledge_max_tokens, temperature=0)
    user_content = json.dumps(pairs, ensure_ascii=False)

    logger.info(
        "knowledge dedup LLM call | drafts_with_candidates=%d timeout=%.0fs max_retries=%d",
        len(pairs), settings.llm_timeout_seconds, settings.llm_max_retries,
    )
    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.warning(
            "knowledge dedup LLM call failed after %.0fms (%s): %s "
            "（超时可调大 LLM_TIMEOUT_SECONDS，当前 %.0fs）",
            elapsed_ms, type(e).__name__, e, settings.llm_timeout_seconds,
        )
        return {}

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_content: Any = resp.content
    raw = raw_content if isinstance(raw_content, str) else str(raw_content)
    result = _parse(raw)
    logger.info(
        "knowledge dedup done | classified_drafts=%d (%.0fms)",
        len(result), elapsed_ms,
    )
    return result
