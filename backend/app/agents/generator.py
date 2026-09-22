"""Generator Agent: produces structured test cases from doc + clarifications.

默认走两阶段生成（先穷举功能点清单，再按功能点分批写用例），见文件下半部分说明。
"""
import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage
from app.agents.llm_factory import build_chat_model
from app.agents._prompt_dump import dump_prompt, dump_response
from app.config import get_settings

logger = logging.getLogger("caseweave.generator")

SYSTEM_PROMPT = """你是一位资深的测试工程师，负责根据产品需求文档和澄清结果编写完整的测试用例。

## 输入说明（重要）
- 用户可能给出"产品需求文档（PRD）"和/或"测试脑图"两份资料。脑图代表测试人员对 PRD 二次梳理后的最终测试意图。
- 当两者同时存在且描述冲突时，**以测试脑图为准**——脑图缺失的细节再回到 PRD 补齐。
- 仅有脑图时：直接以脑图节点为骨架生成用例，不要凭空补 PRD 才有的字段。
- 仅有 PRD 时：保持原有行为。

## 用例粒度（最重要的约束，务必严格遵守）
**一条测试用例 = 一个可独立验证的完整测试目标（一个"验证意图 + 判定点"），而不是一次 UI 操作、一个界面元素或一个步骤。**
- 一个连续的操作流程，只要它指向**同一个最终判定点**，就必须写成**一条**用例——把每一步操作依次写进 `steps` 字段（1. 2. 3. …），预期结果写这条流程走完后的最终验证点。
- **严禁**把同一流程的"第一步""第二步""第三步"、或"点击打开→逐步填写→提交"这类顺序操作拆成多条用例。
- **严禁**把"查看标题""查看描述""查看列表"这种同一界面的多个静态元素各写成一条用例；应合并为"某界面元素展示正确"一条，steps 逐条查看，预期结果逐条列出。
- 只有当操作对应**不同且可独立判定的预期结果**时才拆成不同用例（例如：不同的正向路径、各自独立的反向/边界判定点、彼此互斥的分支结果）。
- 判断口诀：如果两条"用例"必须**按顺序连续执行、后者依赖前者的操作状态、且中间没有各自独立的判定意图**，那它们本就是同一条用例的多个步骤，必须合并。

### 粒度正反例（以"用户画像调研弹窗"为例）
- ❌ 错误：调研第一步选择 / 调研第二步选择 / 调研第三步选择 / 点击提交……各拆成一条用例。
- ✅ 正确：`完成用户画像调研并提交` 一条正向用例——steps 覆盖"打开弹窗→第一步选择→第二步选择→第三步选择→第四步选择→点击提交"，expected_result 用序号对应各步骤的判定点（如 `6. 调研完成、任务标记完成并弹出 toast`）。
- ✅ 仍可独立的反向/边界用例：`某步未选择时下一步按钮置灰`、`其他输入框超 100 字截断` 等——因为它们各自有**独立的判定点**，与主流程判定不同。

## 测试用例覆盖策略
1. **正向用例**：覆盖所有正常业务流程（Happy Path）
2. **反向用例**：边界值、异常输入、权限不足、网络异常等
3. **关联用例**：基于模块关联关系，生成跨功能交互场景
4. **边界用例**：最大值、最小值、空值、特殊字符

## 输出格式（JSON数组）
输出一个JSON数组，每个元素为一条测试用例：
[
  {
    "case_number": "{CASE_PREFIX}-{SUB}-001",
    "name": "用例名称（简洁描述测试意图）",
    "module": "功能模块名称",
    "priority": "P1",
    "preconditions": "前置条件（执行前需满足的条件）",
    "steps": "1. 步骤一\\n2. 步骤二\\n3. 步骤三",
    "expected_result": "1. 步骤一对应的预期结果\\n2. 步骤二对应的预期结果\\n3. 步骤三对应的预期结果",
    "remarks": "备注或注意事项（可为空）",
    "test_result": ""
  }
]

## 执行步骤与预期结果的对应规则（务必遵守）
- `expected_result` 可以有多条，且**必须与 `steps` 里需要校验的步骤序号一一对应**：预期结果每一条以对应的步骤序号开头（如 `2. …` 表示这是第 2 步的预期结果）。
- 只对**需要产生可观察结果/需要断言**的步骤写预期结果；纯准备性、无观察点的操作步骤可不写对应预期结果（序号跳过即可）。
- 序号必须真实指向 `steps` 中存在的步骤，不得错位、不得凭空多出步骤里没有的序号。
- 若整条用例只有一个最终判定点，可只写一条预期结果并标注其对应的步骤序号（如 `3. 提交成功并弹出 toast`）。
- 示例：
  - steps: `1. 打开调研弹窗\\n2. 第一步选择"个人用途"\\n3. 第四步点击提交`
  - expected_result: `1. 弹窗居中展示，标题正确\\n2. "下一步"按钮高亮可点\\n3. 调研完成、任务标记完成并弹出 toast`

## 优先级评定规则（priority 字段，必填）
- **P1（最高）**：核心主流程、影响整体可用性、阻塞业务的功能点；登录/支付/下单/数据安全/权限校验等关键正向用例
- **P2（中）**：常用功能、典型异常分支、主要边界场景；非核心但高频的业务路径
- **P3（最低）**：极端边界、低频异常、UI 细节、辅助提示文案、兜底兼容场景
- 必须从 P1/P2/P3 中三选一，不可省略，不可写其他值

## 用例编号规则（严格遵守）
- 必须使用统一前缀，格式：`{CASE_PREFIX}-{SUB}-{3位序号}`（不要加 `TC-` 这种额外前缀）
- `{CASE_PREFIX}` 由用户给定（其内部可能已经包含短横线，如 `USER-LOGIN`），所有用例的此段必须完全一致，并出现在 case_number 最开头
- `{SUB}` 是你针对该用例所属子功能/场景补充的英文短词（大写、可选，可省略时输出 `{CASE_PREFIX}-001` 形式）；同一子场景下用例的 `{SUB}` 必须保持一致
- 例如前缀 USER-LOGIN：USER-LOGIN-VALID-001 / USER-LOGIN-INVALID-001 / USER-LOGIN-LOCKOUT-001

## 要求
- 每个功能点至少生成1条正向用例 + 2条反向用例
- 再次强调粒度：同一操作流程的连续步骤必须合并进一条用例的 `steps`，不要按步骤拆分用例（见上文"用例粒度"约束）
- 步骤要具体可执行，不能有"等操作"这类模糊描述
- 预期结果要明确，包含具体的提示语、页面跳转、数据变化等；多条预期结果需按步骤序号与 steps 一一对应（见"执行步骤与预期结果的对应规则"）
- 只输出JSON数组，不要有其他说明文字"""


def _build_llm() -> BaseChatModel:
    # max_tokens 由 .env 的 GENERATOR_MAX_TOKENS 控制（默认 16384）。
    # 脑图 + PRD 同时输入时输出体量大；思考模型的 max_tokens 含 thinking，
    # 给小了会让正文被截断成 0 条
    return build_chat_model(
        max_tokens=get_settings().generator_max_tokens,
        temperature=0.2,
    )


def _salvage_truncated_json_array(raw: str) -> list | None:
    """
    模型把 JSON 数组写到一半就被 max_tokens 截断的兜底解析。
    策略：从尾部往回找最后一个完整的 `}`（top-level 大括号），把那之后裁掉，
    再补 `]` 闭合数组——能救回 N-1 条已成形的用例，比直接返回 [] 强。
    """
    s = (raw or "").strip()
    if not s.startswith("["):
        return None
    depth_brace = 0
    in_str = False
    escape = False
    last_close = -1
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
            if depth_brace == 0:
                last_close = i
    if last_close < 0:
        return None
    salvaged = s[: last_close + 1] + "]"
    try:
        import json as _json
        parsed = _json.loads(salvaged)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None




# ══════════════════════════════════════════════════════════════════════════════
# 两阶段生成（默认开启，settings.generator_two_stage）
#
#   阶段 1  test_point_extractor.extract_test_points：只做测试分析，穷举功能点清单
#   阶段 2  按功能点分批（settings.generator_batch_size 个/批）调用 generator，
#           每批只写自己那几个功能点的用例，批间受 settings.generator_batch_concurrency 限流
#   合并    按批次顺序拼接、去重 case_number
#
# 阶段 1 失败 / 空清单 → 退回旧的单次生成，保证不会比以前更差。
# 单批失败 → 记 warning 跳过，其余批次照常入库。
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class GenerationResult:
    cases: list[dict[str, Any]]
    test_points: list[dict[str, Any]] = field(default_factory=list)
    batches: int = 0              # 实际发起的批次数；0 = 走了单次生成
    failed_batches: int = 0
    mode: str = "single"          # single | two_stage


def _build_user_context(
    *,
    skills: str | None,
    relevant_knowledge: str | None,
    module_relations: str | None,
    mindmap_content: str | None,
    doc_content: str,
    clarification_answers: dict[str, str] | None,
) -> str:
    """generator 与 test_point_extractor 共用的上下文段（不含开头的模块/前缀行与结尾指令）。"""
    parts: list[str] = []
    # Skills 优先级最高：来自该模块历史用例修改沉淀的"测试设计经验"（人或 LLM 归纳的 Markdown 备忘单）。
    # 比项目知识库更"贴近测试意图"，所以注入位置在知识库之前。
    if skills:
        parts.append(
            "## 测试设计经验（来自该模块历史用例修改沉淀，应优先参考）\n"
            f"{skills}\n\n"
        )
    if relevant_knowledge:
        parts.append(
            "## 项目知识库（来自历史文档抽取，作为产品上下文参考；与当前文档冲突时以当前文档为准）\n"
            f"{relevant_knowledge}\n\n"
        )
    if module_relations:
        parts.append(f"## 模块关联关系\n{module_relations}\n\n")
    # 脑图放在 PRD 之前，让 LLM 先看到测试人员的最终意图（system prompt 已声明冲突时以脑图为准）
    if mindmap_content:
        parts.append(f"## 测试脑图（与 PRD 冲突时以脑图为准）\n{mindmap_content}\n\n")
    if doc_content:
        parts.append(f"## 需求文档\n{doc_content}\n\n")
    if clarification_answers:
        qa_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in clarification_answers.items())
        parts.append(f"## 澄清确认结果\n{qa_text}\n\n")
    return "".join(parts)


def _user_header(module_name: str, case_prefix: str) -> str:
    return (
        f"功能模块：{module_name}\n"
        f"用例编号前缀（CASE_PREFIX，所有用例必须以 {case_prefix}- 开头，不要再加 TC- 之类的额外前缀）：{case_prefix}\n\n"
    )


def _single_shot_tail(case_prefix: str) -> str:
    return (
        "请根据以上信息生成完整的测试用例列表。"
        f"提醒：所有 case_number 必须以 `{case_prefix}-` 开头，不要带 `TC-` 前缀。"
    )


def _batch_tail(case_prefix: str, batch_points: list[dict[str, Any]], batch_no: int, total_batches: int) -> str:
    lines = [
        "## 本批次任务（重要）",
        f"功能点清单已在上一步整理完毕并拆成 {total_batches} 批，这是第 {batch_no} 批。"
        f"本次**只**为下面 {len(batch_points)} 个功能点编写用例；其它功能点由其它批次负责，不要越界，"
        "也不要遗漏本批的任何一个功能点：",
    ]
    for i, p in enumerate(batch_points, start=1):
        scope = f" —— {p['scope']}" if p.get("scope") else ""
        lines.append(f"{i}. [{p['sub']}] {p['feature']}（整体优先级 {p['priority']}）{scope}")
    lines += [
        "",
        "- 每个功能点至少 1 条正向 + 2 条反向/边界用例；其「覆盖范围」里提到的每个判定点都要有用例覆盖。",
        f"- 用例编号使用对应功能点的 sub：`{case_prefix}-{{sub}}-001` 起、按功能点各自递增（例如 "
        f"`{case_prefix}-{batch_points[0]['sub']}-001`）。不要带 `TC-` 前缀。",
        "- 只输出本批功能点的用例 JSON 数组，不要输出其它说明文字。",
    ]
    return "\n".join(lines)


def _parse_cases(raw: str, *, finish_reason: str | None, label: str) -> list[dict[str, Any]]:
    """JSON 数组解析 + 截断兜底。失败返回 []。"""
    import json

    raw = (raw or "").strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        cases = json.loads(raw)
        if isinstance(cases, list):
            logger.info("Generator[%s] parsed %d cases", label, len(cases))
            return [c for c in cases if isinstance(c, dict)]
        logger.warning("Generator[%s] returned non-list JSON: %s", label, str(cases)[:200])
        return []
    except json.JSONDecodeError as exc:
        # 最常见的失败模式：max_tokens 截断导致 JSON 末尾不完整。
        # 先尝试从尾部回滚到最后一个完整对象，能救回大部分用例
        salvaged = _salvage_truncated_json_array(raw)
        if salvaged:
            logger.warning(
                "Generator[%s] JSON truncated (%s); salvaged %d cases via tail-rollback "
                "(finish=%s, raw_len=%d, raw_head=%s)",
                label, exc, len(salvaged), finish_reason, len(raw), raw[:200],
            )
            return [c for c in salvaged if isinstance(c, dict)]
        logger.warning(
            "Generator[%s] JSON parse failed (%s); finish=%s raw_len=%d head=%s tail=%s",
            label, exc, finish_reason, len(raw), raw[:300], raw[-300:],
        )
        return []


async def _invoke_generator(
    *,
    system_prompt: str,
    user_content: str,
    label: str,
    dump_extra: dict[str, Any],
) -> list[dict[str, Any]]:
    """一次 generator LLM 调用 → 解析后的用例列表。"""
    llm = _build_llm()
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    logger.info("Generator LLM call[%s] | prompt_chars=%d", label, len(user_content))
    dump_path = dump_prompt(
        agent=f"generator_{label}" if label != "single" else "generator",
        system=system_prompt,
        user=user_content,
        extra=dump_extra,
    )
    start = time.perf_counter()
    response = await llm.ainvoke(messages)
    elapsed_ms = (time.perf_counter() - start) * 1000
    raw = response.content if isinstance(response.content, str) else str(response.content)
    raw = raw.strip()
    finish_reason = None
    try:
        meta = getattr(response, "response_metadata", {}) or {}
        finish_reason = (
            meta.get("finish_reason")
            or meta.get("stop_reason")
            or (meta.get("model_output", {}) or {}).get("finish_reason")
        )
    except Exception:
        pass
    logger.info(
        "Generator LLM responded[%s] | response_chars=%d finish=%s (%.0fms)",
        label, len(raw), finish_reason, elapsed_ms,
    )
    dump_response(dump_path, raw, finish_reason=finish_reason)
    return _parse_cases(raw, finish_reason=finish_reason, label=label)


def _split_batches(points: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    """按 batch_size 均分（13 个 / 6 → 5/4/4，而不是 6/6/1，避免末批过小）。"""
    if not points:
        return []
    batch_size = max(1, batch_size)
    n_batches = math.ceil(len(points) / batch_size)
    base, extra = divmod(len(points), n_batches)
    batches: list[list[dict[str, Any]]] = []
    idx = 0
    for b in range(n_batches):
        size = base + (1 if b < extra else 0)
        batches.append(points[idx: idx + size])
        idx += size
    return batches


_TRAILING_NUM_RE = re.compile(r"^(.*?)(\d+)$")


def _dedupe_case_numbers(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """多批合并后 case_number 可能撞车（模型没按 sub 编号）；撞车的顺延尾号直到唯一。"""
    seen: set[str] = set()
    for c in cases:
        num = str(c.get("case_number") or "").strip()
        if not num:
            continue
        if num not in seen:
            seen.add(num)
            continue
        m = _TRAILING_NUM_RE.match(num)
        if not m:
            seen.add(num)
            continue
        head, digits = m.group(1), m.group(2)
        width, n = len(digits), int(digits)
        candidate = num
        while candidate in seen:
            n += 1
            candidate = f"{head}{n:0{width}d}"
        c["case_number"] = candidate
        seen.add(candidate)
    return cases


async def generate_test_cases_detailed(
    doc_content: str,
    module_name: str,
    case_prefix: str,
    clarification_answers: dict[str, str] | None = None,
    skills: str | None = None,
    module_relations: str | None = None,
    relevant_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
    test_point_system_prompt: str | None = None,
) -> GenerationResult:
    """两阶段生成主入口。返回用例 + 功能点清单 + 批次统计。"""
    from app.agents.test_point_extractor import extract_test_points

    s = get_settings()
    active_system = system_prompt or SYSTEM_PROMPT
    context = _build_user_context(
        skills=skills,
        relevant_knowledge=relevant_knowledge,
        module_relations=module_relations,
        mindmap_content=mindmap_content,
        doc_content=doc_content,
        clarification_answers=clarification_answers,
    )
    header = _user_header(module_name, case_prefix)
    base_extra = {
        "module": module_name,
        "case_prefix": case_prefix,
        "doc_chars": len(doc_content or ""),
        "mindmap_chars": len(mindmap_content or ""),
        "answers": len(clarification_answers or {}),
        "has_knowledge": bool(relevant_knowledge),
    }
    logger.info(
        "Generator start | module=%s prefix=%s doc_chars=%d mindmap_chars=%d answers=%d two_stage=%s",
        module_name, case_prefix, len(doc_content or ""), len(mindmap_content or ""),
        len(clarification_answers or {}), s.generator_two_stage,
    )

    async def _single() -> GenerationResult:
        cases = await _invoke_generator(
            system_prompt=active_system,
            user_content=header + context + _single_shot_tail(case_prefix),
            label="single",
            dump_extra=base_extra,
        )
        return GenerationResult(cases=cases, mode="single")

    if not s.generator_two_stage:
        return await _single()

    # ── 阶段 1：功能点清单 ────────────────────────────────────────────────────
    points = await extract_test_points(
        context, module_name=module_name, case_prefix=case_prefix,
        system_prompt=test_point_system_prompt,
    )
    if not points:
        logger.warning("Two-stage: no test points extracted, falling back to single-shot generation")
        return await _single()

    # ── 阶段 2：分批生成 ──────────────────────────────────────────────────────
    batches = _split_batches(points, s.generator_batch_size)
    total = len(batches)
    sem = asyncio.Semaphore(max(1, s.generator_batch_concurrency))
    logger.info(
        "Two-stage: %d test points → %d batches (size≈%d, concurrency=%d)",
        len(points), total, s.generator_batch_size, s.generator_batch_concurrency,
    )

    async def _run_batch(i: int, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        label = f"b{i}of{total}"
        async with sem:
            return await _invoke_generator(
                system_prompt=active_system,
                user_content=header + context + _batch_tail(case_prefix, batch, i, total),
                label=label,
                dump_extra={**base_extra, "batch": f"{i}/{total}",
                            "batch_subs": ",".join(p["sub"] for p in batch)},
            )

    results = await asyncio.gather(
        *(_run_batch(i, b) for i, b in enumerate(batches, start=1)),
        return_exceptions=True,
    )
    merged: list[dict[str, Any]] = []
    failed = 0
    for i, r in enumerate(results, start=1):
        if isinstance(r, BaseException):
            failed += 1
            logger.warning("Two-stage: batch %d/%d failed: %s", i, total, r)
            continue
        merged.extend(r)
    merged = _dedupe_case_numbers(merged)
    logger.info(
        "Two-stage done | points=%d batches=%d failed=%d cases=%d",
        len(points), total, failed, len(merged),
    )
    if not merged and failed == total:
        # 所有批次全挂（通常是网关/网络问题）——再给单次生成一次机会
        logger.warning("Two-stage: all batches failed, falling back to single-shot generation")
        return await _single()
    return GenerationResult(
        cases=merged, test_points=points, batches=total, failed_batches=failed, mode="two_stage",
    )


async def generate_test_cases(
    doc_content: str,
    module_name: str,
    case_prefix: str,
    clarification_answers: dict[str, str] | None = None,
    skills: str | None = None,
    module_relations: str | None = None,
    relevant_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
    test_point_system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generate structured test cases.
    Returns a list of test case dicts matching the JSON schema above.
    （兼容旧签名的薄封装；要拿功能点/批次统计用 generate_test_cases_detailed）
    """
    result = await generate_test_cases_detailed(
        doc_content=doc_content,
        module_name=module_name,
        case_prefix=case_prefix,
        clarification_answers=clarification_answers,
        skills=skills,
        module_relations=module_relations,
        relevant_knowledge=relevant_knowledge,
        mindmap_content=mindmap_content,
        system_prompt=system_prompt,
        test_point_system_prompt=test_point_system_prompt,
    )
    return result.cases


async def stream_generate_test_cases(
    doc_content: str,
    module_name: str,
    case_prefix: str,
    clarification_answers: dict[str, str] | None = None,
    skills: str | None = None,
    module_relations: str | None = None,
    relevant_knowledge: str | None = None,
    mindmap_content: str | None = None,
    system_prompt: str | None = None,
):
    """Stream-generate test cases token by token（单次生成；流式分支不走两阶段）。"""
    llm = _build_llm()
    active_system = system_prompt or SYSTEM_PROMPT
    user_content = (
        _user_header(module_name, case_prefix)
        + _build_user_context(
            skills=skills,
            relevant_knowledge=relevant_knowledge,
            module_relations=module_relations,
            mindmap_content=mindmap_content,
            doc_content=doc_content,
            clarification_answers=clarification_answers,
        )
        + _single_shot_tail(case_prefix)
    )

    messages = [
        SystemMessage(content=active_system),
        HumanMessage(content=user_content),
    ]
    dump_path = dump_prompt(
        agent="generator_stream",
        system=active_system,
        user=user_content,
        extra={
            "module": module_name,
            "case_prefix": case_prefix,
            "doc_chars": len(doc_content or ""),
            "mindmap_chars": len(mindmap_content or ""),
            "answers": len(clarification_answers or {}),
            "has_knowledge": bool(relevant_knowledge),
        },
    )

    buffer = ""
    async for chunk in llm.astream(messages):
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        buffer += text
        yield chunk.content
    dump_response(dump_path, buffer)
