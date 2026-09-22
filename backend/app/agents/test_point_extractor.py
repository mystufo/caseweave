"""Test-point extractor: stage 1 of two-stage generation.

用例生成拆成两步的第一步——先让模型只做「测试分析」，把文档穷举成一份功能点清单，
不写用例。第二步 generator 再按功能点分批写用例（见 generator.generate_test_cases）。

为什么要拆：单次调用时模型会在输出到自己"舒适长度"（约 11~13 KB）后停笔，用例数
和文档规模无关，6 千字和 6 万字的 PRD 都只出 16~18 条。先列清单再分批，数量就由文档
结构决定，而不是由模型的输出习惯决定。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents._prompt_dump import dump_prompt, dump_response
from app.agents.llm_factory import build_chat_model
from app.config import get_settings

logger = logging.getLogger("caseweave.test_points")

SYSTEM_PROMPT = """你是一位资深的测试工程师，正在为一份产品需求做**测试分析**。本步骤不写测试用例，只做一件事：把需求穷举成一份"功能点清单"，供后续分批编写用例。

## 输入说明
- 用户可能给出"产品需求文档（PRD）"和/或"测试脑图"。脑图代表测试人员对 PRD 二次梳理后的最终测试意图，两者冲突时**以脑图为准**，脑图缺失的细节回到 PRD 补齐。
- "澄清确认结果"是需求方对歧义的最终答复，其中每一条结论都必须落到某个功能点里。

## 什么是一个功能点
一个功能点 = 文档中一个可独立测试的功能单元、业务规则、交互入口或状态分支。粒度介于"模块"和"用例"之间：一个功能点通常会对应 2~6 条用例（正向 + 反向 + 边界）。
- 例如「分享弹窗展示」「复制分享链接」「X 平台分享」「H5 端下载限制」「落地页水印规则」「分享视频参数校验」各是一个功能点。
- 不要把一个操作步骤（"点击下一步"）当功能点；也不要把明显不同的功能合并成一个功能点。

## 穷举要求（最重要）
- **逐段通读全文**，文档里出现的每个功能、每条规则、每个平台/端差异（PC / H5 / App / 小程序）、每个状态/权限分支、每个异常与兜底处理，都要有对应的功能点。文档很长时尤其不要略过后半部分。
- 埋点/数据上报、文案/多语言、兼容性、性能限制等章节如果在文档中明确写出，同样各算功能点。
- 脑图存在时，脑图的每个叶子节点至少归属一个功能点。
- 只覆盖文档范围内的内容，不要凭空补"登录""注册"这类文档没提到的通用功能。

## 输出格式（只输出 JSON 数组，不要任何说明文字）
[
  {
    "sub": "SHARE-POPUP",
    "feature": "分享弹窗展示",
    "scope": "覆盖范围：弹窗内的元素与布局、各端差异、打开/关闭入口；关键判定点：标题/按钮/预览图展示正确，H5 端不展示下载按钮。",
    "priority": "P1"
  }
]

字段说明：
- `sub`：英文大写短代码（只含大写字母、数字、短横线，2~20 字符），用作该功能点用例编号的中段 `{CASE_PREFIX}-{sub}-NNN`，清单内必须唯一。
- `feature`：功能点名称，简洁。
- `scope`：2~4 句，写清这个功能点涉及哪些界面元素/规则/分支/端，以及关键判定点。后续写用例时会逐条对照它，遗漏的判定点就不会有用例。
- `priority`：该功能点整体重要性，P1（核心主流程）/ P2（常用功能、典型异常）/ P3（极端边界、UI 细节）。

按文档出现顺序排列。"""

_SUB_RE = re.compile(r"[^A-Z0-9-]+")


def _sanitize_sub(raw: Any, fallback: str, seen: set[str], case_prefix: str | None = None) -> str:
    """把模型给的 sub 规整成 [A-Z0-9-]{2,20} 且清单内唯一；不合格就用 fallback。

    - 模型爱把模块前缀的尾词再写一遍（prefix MODEL-SHARE + sub SHARE-POPUP → MODEL-SHARE-SHARE-POPUP），
      这里把与 case_prefix 尾段重复的开头段剥掉。
    - 超长时在短横线处截断，不把单词砍半（SUPPORT → SUPPOR）。
    """
    s = _SUB_RE.sub("-", str(raw or "").strip().upper()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if case_prefix:
        prefix_segs = [x for x in case_prefix.upper().split("-") if x]
        segs = [x for x in s.split("-") if x]
        # 逐段剥：sub 开头连续若干段若等于 prefix 的尾部若干段，就去掉
        for k in range(min(len(prefix_segs), len(segs) - 1), 0, -1):
            if segs[:k] == prefix_segs[-k:]:
                segs = segs[k:]
                break
        s = "-".join(segs)
    if len(s) > 20:
        cut = s[:21].rfind("-")
        s = s[:cut] if cut >= 2 else s[:20]
        s = s.strip("-")
    if len(s) < 2:
        s = fallback
    base, n = s, 2
    while s in seen:
        s = f"{base}{n}"
        n += 1
    seen.add(s)
    return s


def normalize_test_points(raw_points: Any, case_prefix: str | None = None) -> list[dict[str, Any]]:
    """校验 + 规整模型返回的功能点清单；返回空表表示本次结果不可用。"""
    if not isinstance(raw_points, list):
        return []
    seen: set[str] = set()
    points: list[dict[str, Any]] = []
    for i, p in enumerate(raw_points, start=1):
        if not isinstance(p, dict):
            continue
        feature = str(p.get("feature") or "").strip()
        if not feature:
            continue
        priority = str(p.get("priority") or "P2").strip().upper()
        if priority not in {"P1", "P2", "P3"}:
            priority = "P2"
        points.append({
            "sub": _sanitize_sub(p.get("sub"), f"F{i}", seen, case_prefix),
            "feature": feature,
            "scope": str(p.get("scope") or "").strip(),
            "priority": priority,
        })
    return points


def _strip_fence(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


async def extract_test_points(
    context: str,
    *,
    module_name: str,
    case_prefix: str | None = None,
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Stage 1：从 generator 拼好的上下文（脑图/PRD/澄清/知识）里穷举功能点。

    任何失败（网络、解析、空结果）都返回 []，由调用方回退到单次生成——本步骤绝不能
    让原本能出用例的流程变成 0 条。
    """
    llm = build_chat_model(
        max_tokens=get_settings().generator_max_tokens,
        temperature=0.1,
    )
    active_system = system_prompt or SYSTEM_PROMPT
    user_content = (
        f"功能模块：{module_name}\n\n{context}"
        "请通读以上全部内容，输出该模块需要测试的功能点清单（JSON 数组）。"
    )
    dump_path = dump_prompt(
        agent="test_points",
        system=active_system,
        user=user_content,
        extra={"module": module_name, "context_chars": len(context)},
    )
    start = time.perf_counter()
    try:
        response = await llm.ainvoke([
            SystemMessage(content=active_system),
            HumanMessage(content=user_content),
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Test-point extraction LLM call failed: %s", exc)
        return []
    raw = response.content if isinstance(response.content, str) else str(response.content)
    finish_reason = (getattr(response, "response_metadata", {}) or {}).get("finish_reason")
    dump_response(dump_path, raw, finish_reason=finish_reason)
    logger.info(
        "Test-point extraction responded | chars=%d finish=%s (%.0fms)",
        len(raw), finish_reason, (time.perf_counter() - start) * 1000,
    )
    try:
        parsed = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        logger.warning("Test-point JSON parse failed (%s); head=%s", exc, raw[:200])
        return []
    points = normalize_test_points(parsed, case_prefix)
    logger.info(
        "Test points extracted | module=%s count=%d subs=%s",
        module_name, len(points), ",".join(p["sub"] for p in points[:30]),
    )
    return points
