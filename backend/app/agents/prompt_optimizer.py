"""PromptOptimizer: 分析某项目 generator 的负反馈，产出「建议的 prompt 改动」草稿。

设计原则（Phase 4.2 二阶段）：
- 输入 = 当前生效的 generator system prompt 全文 + 最近 N 条负反馈样本
  （intent / summary / 被改字段，来自 diff_analyzer 已落库的 diff_analysis）
- 输出 = 一份**增量修订后的完整 prompt** + 一句为什么这么改（rationale）
- 定位是「只读建议 + 人工审核」：本模块只负责产出建议内容，绝不落库、绝不激活
- 关键护栏：产出模板必须仍满足 generator 的输出契约（编号前缀 / JSON 数组 / 优先级），
  否则判定为破坏契约 → 返回 None 丢弃，避免把 prompt 改坏拖垮生成
- 样本不足 / LLM 失败 / JSON 解析失败 / 契约校验不过：一律返回 None（吞异常 + log）
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

logger = logging.getLogger("caseweave.prompt_optimizer")

# 负反馈样本少于该值就不给建议（信号太弱，改了也是拍脑袋）。可被 settings 覆盖。
DEFAULT_MIN_SAMPLES = 3

# generator 输出契约的关键锚点：产出模板必须命中每一组（组内任一别名即可），
# 缺任一组视为破坏了 generator 与下游解析器的约定 → 丢弃该建议。
_CONTRACT_TOKENS: list[tuple[str, ...]] = [
    ("{CASE_PREFIX}",),                 # 用例编号前缀占位符
    ("JSON数组", "JSON 数组", "JSON数组"),  # 输出为 JSON 数组
    ("case_number",),                  # 编号字段
    ("priority", "P1"),                # 优先级契约
    ("expected_result",),              # 预期结果字段
]

SYSTEM_PROMPT = """你是一名资深测试架构师兼提示词工程师。给你一份**正在使用的**测试用例
生成系统提示词，以及测试人员近期对生成结果的**负反馈**（点踩 / 人工修改的意图汇总）。
你的任务：在**尽量小改动**的前提下，修订这份提示词，使其下次能规避这些负反馈。

## 硬约束（务必遵守，否则你的产出会被丢弃）
1. 你只做**增量修订**：补充/收紧规则、增加约束或示例。**禁止推翻结构、禁止大段重写**。
2. 必须**完整保留原提示词的输出契约**，原样保留这些关键约定：
   - 用例编号占位符 `{CASE_PREFIX}` 及其编号规则
   - 「输出为 JSON 数组」以及 case_number / priority / expected_result 等字段约定
   - 优先级 P1/P2/P3 规则
   若你删改了上述任何一项，产出将无效。
3. 改动要**由负反馈直接支撑**——没有对应负反馈信号的地方不要乱改。
4. 如果这些负反馈**无法**转化为对提示词的有效改进（如全是"改写表达"类措辞修改），
   输出固定字符串 `__NO_SIGNAL__`（不要输出 JSON）。

## 输出格式（仅 JSON 对象，不要解释、不要代码 fence）
{
  "suggested_template": "修订后的完整提示词全文（不是 diff，是可直接使用的整篇）",
  "rationale": "一段话说明改了哪里、对应哪些负反馈（不超过 120 字）"
}
"""


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _format_feedback_samples(samples: list[dict[str, Any]]) -> str:
    if not samples:
        return "（无）"
    lines: list[str] = []
    for s in samples:
        intent = s.get("intent") or "其它"
        summary = (s.get("summary") or "").strip() or "（无总结）"
        fields = s.get("changed_fields")
        suffix = f"（改动字段：{', '.join(fields)}）" if isinstance(fields, list) and fields else ""
        lines.append(f"- [{intent}] {summary}{suffix}")
    return "\n".join(lines)


def _violates_contract(template: str) -> list[str]:
    """返回产出模板缺失的契约组（人类可读名）；空列表 = 契约完整。"""
    missing: list[str] = []
    for group in _CONTRACT_TOKENS:
        if not any(tok in template for tok in group):
            missing.append(group[0])
    return missing


async def suggest_generator_prompt(
    *,
    current_template: str,
    feedback_samples: list[dict[str, Any]],
    min_samples: int | None = None,
) -> dict[str, Any]:
    """基于负反馈给 generator 提示词一份增量修订建议。

    始终返回带 outcome 码的 dict，便于上层给出区分性的文案：
      - {"outcome":"ok", "suggested_template":..., "rationale":...}  产出可用建议
      - {"outcome":"insufficient_samples"}   负反馈样本不足
      - {"outcome":"no_signal"}              LLM 判定这批反馈无需/无法改 prompt
      - {"outcome":"llm_failed"}             LLM 调用异常
      - {"outcome":"parse_failed"}           返回非预期 JSON
      - {"outcome":"contract_violation", "missing":[...]}  产出破坏了输出契约被丢弃
      - {"outcome":"identical"}              产出与当前版本无实质差异
    """
    threshold = DEFAULT_MIN_SAMPLES if min_samples is None else max(1, min_samples)
    if len(feedback_samples) < threshold:
        logger.info(
            "prompt_optimizer skip | samples=%d (need >=%d)",
            len(feedback_samples), threshold,
        )
        return {"outcome": "insufficient_samples"}
    if not (current_template or "").strip():
        logger.info("prompt_optimizer skip | empty current template")
        return {"outcome": "insufficient_samples"}

    llm = build_chat_model(max_tokens=get_settings().generator_max_tokens, temperature=0.2)
    user_content = (
        f"## 当前正在使用的生成提示词（全文）\n{current_template}\n\n"
        f"## 近期负反馈（{len(feedback_samples)} 条）\n"
        f"{_format_feedback_samples(feedback_samples)}\n\n"
        "请按要求输出修订后的完整提示词与 rationale。"
    )

    logger.info("prompt_optimizer LLM call | samples=%d", len(feedback_samples))
    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("prompt_optimizer LLM call failed: %s", e)
        return {"outcome": "llm_failed"}

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_content: Any = resp.content
    raw = (raw_content if isinstance(raw_content, str) else str(raw_content)).strip()

    if not raw or "__NO_SIGNAL__" in raw:
        logger.info("prompt_optimizer no signal (%.0fms)", elapsed_ms)
        return {"outcome": "no_signal"}

    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        logger.warning("prompt_optimizer JSON parse failed: %s; head=%s", e, raw[:200])
        return {"outcome": "parse_failed"}
    if not isinstance(data, dict):
        logger.warning("prompt_optimizer returned non-object: %s", type(data).__name__)
        return {"outcome": "parse_failed"}

    suggested = str(data.get("suggested_template") or "").strip()
    rationale = str(data.get("rationale") or "").strip()[:200]
    if not suggested:
        logger.info("prompt_optimizer empty suggested_template")
        return {"outcome": "parse_failed"}

    # 与原文无实质差异 → 视作没建议（去掉首尾空白后比较）
    if suggested.strip() == current_template.strip():
        logger.info("prompt_optimizer suggestion identical to current, skip")
        return {"outcome": "identical"}

    # 契约护栏：缺任一关键约定即丢弃
    missing = _violates_contract(suggested)
    if missing:
        logger.warning("prompt_optimizer discarded, missing contract tokens: %s", missing)
        return {"outcome": "contract_violation", "missing": missing}

    logger.info(
        "prompt_optimizer done | suggested_chars=%d (%.0fms)", len(suggested), elapsed_ms,
    )
    return {"outcome": "ok", "suggested_template": suggested, "rationale": rationale}
