"""FeedbackTriage: 把负反馈分诊到 知识 / Skill / Prompt 三个进化出口。

设计：
- triage_intent 是纯函数——把已归一的 intent 映射到出口列表，无副作用、无 LLM
- classify_dislike 给"带原因的 dislike"用 LLM 归一到与 diff_analyzer 相同的 intent 枚举，
  这样 dislike 也能进分诊链路（问题 A）；失败 fail-open 返回 "其它"
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.diff_analyzer import VALID_INTENTS
from app.agents.llm_factory import build_chat_model
from app.config import get_settings

logger = logging.getLogger("caseweave.feedback_triage")

# intent → 出口列表。空列表表示"不消费"（噪声或信号不明）。
# knowledge：产品规则类，归产品知识（模块级同时也喂 skill）
# prompt/skill：通用测试设计缺陷，喂通用提示词与模块经验
_TRIAGE_MAP: dict[str, list[str]] = {
    "修正业务规则": ["knowledge", "skill"],
    "补充边界用例": ["prompt", "skill"],
    "调整步骤": ["prompt", "skill"],
    "改写表达": [],
    "其它": [],
}


def triage_intent(intent: str | None) -> list[str]:
    """把归一后的 intent 映射到消费出口列表（纯函数）。未知 intent → 不消费。"""
    if not intent:
        return []
    return list(_TRIAGE_MAP.get(intent, []))


def targets_to_str(targets: list[str]) -> str | None:
    """出口列表 → 逗号分隔存储串（空列表存 None）。"""
    return ",".join(targets) if targets else None


CLASSIFY_SYSTEM_PROMPT = """你是一名资深测试架构师。测试人员对一条生成的测试用例点了"踩"，并（可能）
给了一句原因。请判断这条负反馈的**意图类别**，只从下列固定枚举中选一个：

- 补充边界用例：缺少边界值、空值、特殊字符、并发、异常输入等场景
- 修正业务规则：用例里的产品规则/业务行为写错了
- 调整步骤：操作步骤顺序/拆分/表述有问题
- 改写表达：仅措辞、格式问题，业务含义没错
- 其它：以上都不是，或原因不足以判断

只输出一个 JSON 对象：{"intent": "<上述枚举之一>"}，不要解释、不要代码 fence。"""


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


async def classify_dislike(
    *,
    reason: str,
    case: dict[str, Any] | None = None,
    module_name: str | None = None,
) -> str:
    """把带原因的 dislike 归一到 intent 枚举。无原因/失败 → "其它"（fail-open）。"""
    if not (reason or "").strip():
        return "其它"

    llm = build_chat_model(max_tokens=get_settings().knowledge_max_tokens, temperature=0)
    parts = [f"产品模块：{module_name or '未指定'}", f"踩的原因：{reason.strip()}"]
    if case:
        for f in ("name", "steps", "expected_result"):
            v = case.get(f)
            if v:
                parts.append(f"用例{f}：{v}")
    user_content = "\n".join(parts)

    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=CLASSIFY_SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("classify_dislike LLM failed: %s", e)
        return "其它"

    raw_content: Any = resp.content
    raw = raw_content if isinstance(raw_content, str) else str(raw_content)
    try:
        data = json.loads(_strip_code_fence(raw))
        intent = str(data.get("intent") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        intent = ""
    if intent not in VALID_INTENTS:
        intent = "其它"
    logger.info(
        "classify_dislike done | intent=%s (%.0fms)", intent, (time.perf_counter() - start) * 1000,
    )
    return intent
