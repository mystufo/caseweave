"""DiffAnalyzer: 分析测试人员对生成用例的 edit，沉淀为知识 + 修改意图。

设计原则：
- 区分"措辞修改 vs 规则修改"：只有规则修改才产 KnowledgeDraft，措辞修改产空数组
- intent 取固定枚举（便于后续聚合统计）
- 任何失败返回 None，不抛——这是后台异步任务，吞异常 + log warning 即可
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_factory import build_chat_model
from app.config import get_settings
from app.knowledge.store import KnowledgeDraft

logger = logging.getLogger("testcraft.diff_analyzer")

VALID_INTENTS = {"补充边界用例", "修正业务规则", "改写表达", "调整步骤", "其它"}
VALID_RULE_TYPES = {"product_rule", "constraint", "ui_behavior"}
WATCHED_FIELDS = ("name", "preconditions", "steps", "expected_result", "remarks", "priority")

SYSTEM_PROMPT = """你是一名资深测试架构师。测试人员刚刚对一条 LLM 生成的测试用例做了人工修改，
你的任务是判断"这次修改背后的真实意图"，并从中提取**可沉淀**的产品规则。

## 修改意图（intent，必须从下列枚举中选一个）
- 补充边界用例：增加边界值、空值、特殊字符等场景
- 修正业务规则：纠正用例里写错的产品规则（如密码长度、价格策略）
- 改写表达：仅措辞、表述、格式调整，**业务含义没变**
- 调整步骤：操作步骤的顺序/拆分/合并
- 其它：以上都不是

## 知识抽取规则（extracted_rules）
- 仅当修改清晰指向一条**自包含的产品规则**时才输出（产品规则、约束、UI 行为）
- intent="改写表达" 或 "调整步骤" 几乎一定输出空数组——这两种修改不携带新规则
- 每条 content 必须自包含可独立阅读，不要写成"按上文" / "根据修改"
- knowledge_type 限：product_rule / constraint / ui_behavior
- 最多输出 3 条，宁缺勿滥；不确定就不要输出
- confidence 在 0.5–0.9：用例里直接出现明确数值/规则用 0.8+，需要推断的用 0.5–0.6

## 输出格式（仅 JSON 对象，不要解释）
{
  "summary": "一句话描述这次修改改了什么（不超过 40 字）",
  "intent": "补充边界用例 | 修正业务规则 | 改写表达 | 调整步骤 | 其它",
  "extracted_rules": [
    {"knowledge_type": "constraint", "content": "...", "confidence": 0.8}
  ]
}

如果没有可沉淀的规则，extracted_rules 输出 []。"""


@dataclass
class DiffAnalysis:
    summary: str
    intent: str
    changed_fields: list[str]
    extracted_rules: list[KnowledgeDraft] = field(default_factory=list)
    raw: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "intent": self.intent,
            "changed_fields": list(self.changed_fields),
            "extracted_rules": [
                {"knowledge_type": r.knowledge_type, "content": r.content, "confidence": r.confidence}
                for r in self.extracted_rules
            ],
        }


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def diff_changed_fields(
    original: dict[str, Any] | None,
    modified: dict[str, Any] | None,
) -> list[str]:
    """对比 5 个文本字段 + priority；返回真有变化（且非空）的字段名列表。"""
    if not modified:
        return []
    o = original or {}
    changed: list[str] = []
    for f in WATCHED_FIELDS:
        if f not in modified:
            continue
        new_v = modified.get(f)
        old_v = o.get(f)
        if (new_v or "") != (old_v or "") and (new_v or "").strip():
            changed.append(f)
    return changed


def has_real_diff(
    original: dict[str, Any] | None,
    modified: dict[str, Any] | None,
) -> bool:
    return bool(diff_changed_fields(original, modified))


def _format_case(label: str, case: dict[str, Any] | None) -> str:
    if not case:
        return f"### {label}\n（空）\n"
    lines = [f"### {label}"]
    for f in WATCHED_FIELDS:
        v = case.get(f)
        if v in (None, ""):
            continue
        lines.append(f"- **{f}**: {v}")
    return "\n".join(lines) + "\n"


async def analyze_edit(
    *,
    original: dict[str, Any] | None,
    modified: dict[str, Any] | None,
    module_name: str | None = None,
) -> DiffAnalysis | None:
    """LLM 分析一次 edit。失败/无变化返回 None。"""
    changed = diff_changed_fields(original, modified)
    if not changed:
        return None

    # max_tokens 复用 knowledge_max_tokens；diff 分析输出体量不大，但思考模型仍需充足预算
    llm = build_chat_model(max_tokens=get_settings().knowledge_max_tokens, temperature=0)
    user_content = (
        f"产品模块：{module_name or '未指定'}\n"
        f"被修改字段：{', '.join(changed)}\n\n"
        f"{_format_case('修改前', original)}\n{_format_case('修改后', modified)}"
    )

    logger.info(
        "diff_analyzer LLM call | module=%s changed=%s",
        module_name or "未指定", changed,
    )
    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:
        logger.warning("diff_analyzer LLM call failed: %s", e)
        return None

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_content: Any = resp.content
    raw = raw_content if isinstance(raw_content, str) else str(raw_content)

    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        logger.warning("diff_analyzer JSON parse failed: %s; head=%s", e, raw[:200])
        return None
    if not isinstance(data, dict):
        logger.warning("diff_analyzer returned non-object, got %s", type(data).__name__)
        return None

    summary = str(data.get("summary") or "").strip()[:200]
    intent = str(data.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        intent = "其它"

    rules: list[KnowledgeDraft] = []
    raw_rules = data.get("extracted_rules") or []
    if isinstance(raw_rules, list):
        seen: set[str] = set()
        for item in raw_rules:
            if not isinstance(item, dict):
                continue
            t = str(item.get("knowledge_type") or "").strip()
            c = str(item.get("content") or "").strip()
            if t not in VALID_RULE_TYPES or not c:
                continue
            key = f"{t}|{c[:80]}"
            if key in seen:
                continue
            seen.add(key)
            try:
                conf = float(item.get("confidence", 0.7))
            except (TypeError, ValueError):
                conf = 0.7
            conf = max(0.3, min(0.95, conf))
            rules.append(KnowledgeDraft(
                knowledge_type=t, content=c, source="user_feedback", confidence=conf,
            ))
            if len(rules) >= 3:
                break

    logger.info(
        "diff_analyzer done | intent=%s rules=%d raw_chars=%d (%.0fms)",
        intent, len(rules), len(raw), elapsed_ms,
    )
    return DiffAnalysis(
        summary=summary,
        intent=intent,
        changed_fields=changed,
        extracted_rules=rules,
        raw=raw,
    )
