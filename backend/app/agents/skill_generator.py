"""SkillGenerator: 把一个模块下"用户反馈分析 + 用户反馈衍生的知识"归纳成一份 Markdown Skill。

设计原则：
- 输入 = 该模块最近 N 条 edit 反馈的 diff_analysis（含 intent/summary）+ 来自反馈的知识条目
- 输出 = 给下一次同模块生成用例时直接注入的"测试设计经验"备忘单（Markdown）
- 样本不足或 LLM 失败：返回 None，让上层路由跳过 upsert
"""
from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_factory import build_chat_model
from app.config import get_settings

logger = logging.getLogger("testcraft.skill_generator")

MIN_SAMPLES = 3  # 反馈 + 知识合计少于该值就不归纳（信号太弱）

SYSTEM_PROMPT = """你是一名资深测试架构师，正在为某个产品模块沉淀"测试设计经验"备忘单。

## 输入说明
你将收到：
1. 一份**模块名**
2. 该模块最近的"用例修改记录"——每条包含修改意图（intent）、一句话总结（summary）
3. 该模块下"由历史用例修改沉淀的产品规则" —— 它们已经经过去重去噪，是高密度信号

## 输出要求
- 输出**简洁的 Markdown 备忘单**，目的是被下一次该模块生成用例时作为提示词注入
- 结构建议：
  ```
  ## 设计要点
  - …
  ## 易错场景 / 边界
  - …
  ## 已沉淀的产品规则
  - …
  ```
- 内容要点：
  - 直接面向"如何设计该模块的测试用例"，不要复述每条反馈
  - "易错场景"段提炼出测试人员反复修改的方向（边界值、异常输入、特殊状态）
  - "已沉淀的产品规则"段直接列规则原文，不要重新措辞（避免引入歧义）
  - 全文不超过 600 字；宁少勿水
- 仅输出 Markdown 正文，不要前言/解释/代码 fence

## 增量合并（当提供了"已有备忘单"时）
- 若输入里带有**已有备忘单**，说明这是在旧经验之上的增量更新：
  - **保留**旧备忘单里仍然有效的要点，不要删除或改写与本次新信号无关的内容
  - 把本次新反馈/新规则**合并进对应小节**，语义重复的条目去重、不要堆叠
  - 输出**完整的合并后 Markdown**（不是只输出新增部分）
- 若本次新信号无法带来任何新增/修正（如全是"改写表达"类噪声），输出固定字符串 `__NO_SIGNAL__`，
  以便上层保留旧备忘单不动

## 何时输出空
- 如果输入信号无法归纳出任何有用经验（如全是"改写表达"类的措辞修改），输出固定字符串 `__NO_SIGNAL__`
"""


def _format_feedback_samples(samples: list[dict[str, Any]]) -> str:
    if not samples:
        return "（无）"
    lines: list[str] = []
    for s in samples:
        intent = s.get("intent") or "其它"
        summary = (s.get("summary") or "").strip() or "（无总结）"
        lines.append(f"- [{intent}] {summary}")
    return "\n".join(lines)


def _format_knowledge_entries(entries: list[str]) -> str:
    if not entries:
        return "（无）"
    return "\n".join(f"- {e.strip()}" for e in entries if e and e.strip())


async def generate_skill_for_module(
    *,
    module_name: str,
    feedback_samples: list[dict[str, Any]],
    knowledge_entries: list[str],
    existing_skill: str | None = None,
) -> str | None:
    """归纳一份 Skill Markdown。失败 / 信号不足返回 None。

    existing_skill 非空时进入"增量合并"模式：在旧备忘单基础上并入新信号、保留旧要点，
    此时放宽 MIN_SAMPLES 闸门（哪怕只有 1 条新反馈也值得合并进旧经验）。
    """
    incremental = bool((existing_skill or "").strip())
    total_signal = len(feedback_samples) + len(knowledge_entries)
    # 首次归纳需要足够信号避免噪声成篇；增量合并只要有任意新信号即可
    if not incremental and total_signal < MIN_SAMPLES:
        logger.info(
            "skill_generator skip | module=%s feedback=%d knowledge=%d (need >=%d)",
            module_name, len(feedback_samples), len(knowledge_entries), MIN_SAMPLES,
        )
        return None
    if incremental and total_signal == 0:
        logger.info("skill_generator skip | module=%s incremental but no new signal", module_name)
        return None

    llm = build_chat_model(max_tokens=get_settings().knowledge_max_tokens, temperature=0.2)
    parts = [
        f"# 模块名：{module_name}\n",
        f"## 最近修改记录（{len(feedback_samples)} 条）\n"
        f"{_format_feedback_samples(feedback_samples)}\n",
        f"## 已沉淀的产品规则（{len(knowledge_entries)} 条，来自历史用例修改）\n"
        f"{_format_knowledge_entries(knowledge_entries)}\n",
    ]
    if incremental:
        parts.append(
            f"## 已有备忘单（请在其基础上增量合并，保留仍有效的旧要点）\n"
            f"{existing_skill.strip()}\n"  # type: ignore[union-attr]
        )
    parts.append("请按要求输出 Markdown 经验备忘单。")
    user_content = "\n".join(parts)

    logger.info(
        "skill_generator LLM call | module=%s feedback=%d knowledge=%d incremental=%s",
        module_name, len(feedback_samples), len(knowledge_entries), incremental,
    )
    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:
        logger.warning("skill_generator LLM call failed: %s", e)
        return None

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_content: Any = resp.content
    raw = (raw_content if isinstance(raw_content, str) else str(raw_content)).strip()

    # 裁掉首尾代码 fence（模型偶尔会包一层 ```markdown）
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        if first_nl > 0:
            raw = raw[first_nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    if not raw or raw == "__NO_SIGNAL__":
        logger.info("skill_generator no signal | module=%s (%.0fms)", module_name, elapsed_ms)
        return None

    logger.info(
        "skill_generator done | module=%s chars=%d (%.0fms)",
        module_name, len(raw), elapsed_ms,
    )
    return raw
