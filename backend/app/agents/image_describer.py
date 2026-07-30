"""Image → text description agent (for embedded document images).

把一张图片（UI 原型 / 流程图 / 架构图 / 表单截图）用视觉模型识别成一段结构化中文
描述，供 Clarifier/Generator 生成测试用例时参考。

失败一律返回 ""（空串），由调用方 fail-open：识别不了的图就当没有，绝不阻断导入。
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_factory import build_vision_model
from app.config import get_settings

logger = logging.getLogger("caseweave.image_describer")

_SYSTEM_PROMPT = (
    "你是测试分析专家。用户会给你一张来自产品需求文档的配图，请把图中对"
    "「设计测试用例」有价值的信息用简洁中文描述出来。重点关注：\n"
    "1. 界面元素：页面/弹窗标题、输入框、下拉、按钮、开关、字段及其标签、必填标记、默认值、占位提示；\n"
    "2. 交互与流程：步骤顺序、分支/条件、跳转关系（流程图/时序图要写清节点与走向）；\n"
    "3. 状态流转：状态机各状态与触发条件；\n"
    "4. 规则与约束：图中标注的校验规则、边界值、提示文案、异常路径。\n"
    "忽略纯装饰性内容（配色、图标美化、无信息的插画）。若图片信息太少或无关，"
    "只回一句「（该图无可用于测试的有效信息）」。不要臆造图中没有的内容，不要输出前言。"
)


async def describe_image(image_bytes: bytes, mime: str, *, heading: str | None = None) -> str:
    """识别单张图片，返回中文描述；任何失败返回 ""。"""
    if not image_bytes:
        return ""
    settings = get_settings()

    try:
        b64 = base64.b64encode(image_bytes).decode("ascii")
    except Exception as exc:  # 理论上不会发生
        logger.warning("image base64 encode failed: %s", exc)
        return ""

    hint = f"这张图出现在文档章节「{heading}」附近。\n" if heading else ""
    data_url = f"data:{mime};base64,{b64}"
    human = HumanMessage(content=[
        {"type": "text", "text": hint + "请描述这张图中与测试用例相关的信息。"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ])

    try:
        llm = build_vision_model(max_tokens=settings.vision_max_tokens, temperature=0)
    except Exception as exc:
        logger.warning("build vision model failed: %s", exc)
        return ""

    start = time.perf_counter()
    try:
        resp = await llm.ainvoke([SystemMessage(content=_SYSTEM_PROMPT), human])
    except Exception as exc:
        logger.warning("vision LLM call failed (heading=%s): %s", heading, exc)
        return ""

    raw: Any = resp.content
    text = raw if isinstance(raw, str) else _flatten_content(raw)
    text = (text or "").strip()
    logger.info(
        "image described | heading=%s bytes=%d chars=%d (%.0fms)",
        heading, len(image_bytes), len(text), (time.perf_counter() - start) * 1000,
    )
    return text


def _flatten_content(content: Any) -> str:
    """多模态模型的响应有时是 list[part]（含 {"type":"text","text":...}）；抠出文本拼接。"""
    if isinstance(content, list):
        parts: list[str] = []
        for it in content:
            if isinstance(it, dict):
                t = it.get("text") or it.get("value")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(it, str):
                parts.append(it)
        return "\n".join(parts)
    return str(content)
