"""ModuleClassifier: 给一段产品文档自动归类到项目下已有的模块。

为什么需要：用户经常忘了选模块，或新人不知道项目里有哪些模块。落到 NULL 项目级也能跑，
但会让"按模块召回知识 / 关联关系 / 同模块用例归档"统统失效。

设计：
- 输入：raw_text（截断后的）+ 候选 modules（id, name, description）
- 输出：(best_module_id | None, confidence 0-1, reasoning)
- 失败/无候选/解析异常 → 返回 (None, 0.0, "...")，调用方按"未识别"走老路径

阈值由调用方决定：
- ≥ 0.7：直接落 module_id（高置信，省掉用户一次点击）
- 0.3 ~ 0.7：返回前端"建议归类到 XX，是否采用？"
- < 0.3：当作没识别
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_factory import build_chat_model
# 英文名（= 用例编号前缀）复用 clarifier 的归一逻辑。clarifier 不 import 本模块，无循环依赖。
from app.agents.clarifier import _sanitize_case_prefix

logger = logging.getLogger("caseweave.module_classifier")


SYSTEM_PROMPT = """你是一名产品经理助手，正在帮测试团队归档新上传的产品需求文档。
你的任务：根据文档内容，从给定的"候选模块清单"中选出**最匹配的一个**模块；
若都不匹配（或清单为空），则**提议一个新的模块**供测试团队新建。

判断要点：
- 看文档讲的是哪一块业务逻辑/UI/数据流；模块描述只是参考，名字 + 关键词更重要
- 文档可能横跨多个模块，请挑"主功能落在的那个"，不要纠结边角
- 文档完全不属于任何候选模块（或候选清单为空）→ module_id=null，并在 proposed_module 给出建议
- confidence 取值范围 0.0–1.0：
    1.0 = 文档名/章节直接点名该模块
    0.7 = 内容主体属于该模块（推荐自动落库门槛）
    0.5 = 关联较多但还涉及其他模块
    0.3 = 仅有零星关联，建议询问用户
    0.0 = 完全不匹配
- proposed_module：**仅当 module_id=null 时**给出，用于建议新建模块；命中已有模块时填 null。
    name 取简洁的业务功能名（≤8 个汉字，如"订单管理""用户登录"），不要带"模块""系统"等冗余后缀；
    code 取该模块的英文名，同时用作用例编号前缀：大写英文，单词以短横线连接（如 ORDER-MGMT、USER-LOGIN），
        只含 A–Z 0–9 和短横线，不要 TC- 前缀；
    description 一句话概括该模块职责（≤40 字）。

输出格式（必须是单个 JSON 对象，不要解释、不要 ```fenced```）：
{"module_id": <int 或 null>, "confidence": <float>, "reasoning": "<不超过 60 字>",
 "proposed_module": {"name": "<新模块中文名>", "code": "<英文名/编号前缀>", "description": "<一句话>"} 或 null}
"""


@dataclass
class ModuleSuggestion:
    module_id: Optional[int]
    confidence: float
    reasoning: str
    proposed_name: Optional[str] = None          # 都不匹配时 LLM 建议新建的模块名（中文）
    proposed_code: Optional[str] = None           # 建议新模块的英文名 = 用例编号前缀（大写）
    proposed_description: Optional[str] = None    # 建议新模块的简短描述

    @property
    def is_high_confidence(self) -> bool:
        return self.module_id is not None and self.confidence >= 0.7

    @property
    def is_suggestable(self) -> bool:
        # 0.3 ~ 0.7：值得展示给用户但不自动落库
        return self.module_id is not None and 0.3 <= self.confidence < 0.7

    @property
    def has_proposal(self) -> bool:
        # 没命中任何已有模块，但 LLM 提议新建一个
        return self.module_id is None and bool((self.proposed_name or "").strip())


def _strip_code_fence(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse(raw: str, valid_ids: set[int]) -> ModuleSuggestion:
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as e:
        logger.warning("module classifier JSON parse failed: %s; head=%s", e, raw[:200])
        return ModuleSuggestion(None, 0.0, "解析失败")

    if not isinstance(data, dict):
        return ModuleSuggestion(None, 0.0, "格式错误")

    raw_id = data.get("module_id")
    module_id: Optional[int] = None
    if raw_id is not None:
        try:
            mid = int(raw_id)
            if mid in valid_ids:
                module_id = mid
            # LLM 偶尔会编造 id；不在候选集 → 当作没识别
        except (TypeError, ValueError):
            module_id = None

    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    if module_id is None:
        conf = 0.0

    reasoning = str(data.get("reasoning") or "").strip()[:200]

    # 仅当没命中已有模块时，采纳 LLM 提议的新模块
    proposed_name: Optional[str] = None
    proposed_code: Optional[str] = None
    proposed_description: Optional[str] = None
    if module_id is None:
        proposed = data.get("proposed_module")
        if isinstance(proposed, dict):
            pname = str(proposed.get("name") or "").strip()
            if pname:
                proposed_name = pname[:50]
                proposed_description = str(proposed.get("description") or "").strip()[:200] or None
                # code 归一为合法的大写编号前缀；LLM 没给或给了非法值时 _sanitize 返回 "CASE"，
                # 视作没建议（前端可让用户自己补），不硬塞 CASE。
                raw_code = str(proposed.get("code") or "").strip()
                if raw_code:
                    sanitized = _sanitize_case_prefix(raw_code)
                    proposed_code = sanitized if sanitized != "CASE" else None

    return ModuleSuggestion(
        module_id, conf, reasoning,
        proposed_name=proposed_name,
        proposed_code=proposed_code,
        proposed_description=proposed_description,
    )


async def classify_module(
    *,
    doc_content: str,
    candidates: list[dict[str, Any]],
) -> ModuleSuggestion:
    """Best-effort module classification.

    candidates: [{"id": int, "name": str, "description": str | None}, ...]
    候选可以为空——此时进入"纯提议"模式，让 LLM 直接建议一个新模块。
    返回 ModuleSuggestion；任何异常都吞掉返回低置信度结果（不阻塞主流程）。
    """
    if not (doc_content or "").strip():
        return ModuleSuggestion(None, 0.0, "文档为空")

    valid_ids = {int(c["id"]) for c in candidates if c.get("id") is not None}
    catalog_lines = []
    for c in candidates:
        cid = c.get("id")
        cname = (c.get("name") or "").strip()
        cdesc = (c.get("description") or "").strip()
        if not cname:
            continue
        line = f"- id={cid}, name={cname}"
        if cdesc:
            line += f"，描述：{cdesc[:80]}"
        catalog_lines.append(line)

    catalog = "\n".join(catalog_lines) if catalog_lines else "（项目当前还没有任何模块，请直接提议一个新模块）"
    user_content = (
        "候选模块清单：\n"
        + catalog
        + "\n\n产品文档内容（已截断）：\n"
        + doc_content
    )

    # 分类比抽取轻得多，留 2048 token 足够，避免占满 reasoning 预算
    llm = build_chat_model(max_tokens=2048, temperature=0)
    logger.info(
        "module classifier LLM call | candidates=%d doc_chars=%d",
        len(candidates), len(doc_content),
    )
    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    except Exception as e:
        logger.warning("module classifier LLM call failed: %s", e)
        return ModuleSuggestion(None, 0.0, "LLM 调用失败")

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_content: Any = resp.content
    raw = raw_content if isinstance(raw_content, str) else str(raw_content)
    suggestion = _parse(raw, valid_ids)
    logger.info(
        "module classifier done | module_id=%s confidence=%.2f (%.0fms)",
        suggestion.module_id, suggestion.confidence, elapsed_ms,
    )
    return suggestion
